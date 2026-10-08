use multihull::core::Snapshot;
use multihull::cp::grpc::stream_once;
use multihull::cp::http::{client, Poller};
use multihull::cp::proto::discovery_server::{Discovery, DiscoveryServer};
use multihull::cp::proto::{control_message, router_message, ControlMessage, RouterMessage};
use multihull::cp::{proto, BearerToken, SnapshotError, SourceAuth};
use multihull::tls::{authenticated_client_config, ClientIdentity};
use rcgen::{
    BasicConstraints, CertificateParams, DnType, ExtendedKeyUsagePurpose, IsCa, Issuer, KeyPair,
    KeyUsagePurpose,
};
use router_testkit::{MockUpstream, MockUpstreamConfig};
use rustls::server::WebPkiClientVerifier;
use rustls::{RootCertStore, ServerConfig};
use std::net::SocketAddr;
use std::path::{Path, PathBuf};
use std::pin::Pin;
use std::sync::{Arc, Mutex};
use std::task::{Context, Poll};
use std::time::Duration;
use tempfile::TempDir;
use tokio::io::{AsyncRead, AsyncWrite, ReadBuf};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::{mpsc, watch};
use tokio_rustls::TlsAcceptor;
use tokio_stream::wrappers::{ReceiverStream, TcpListenerStream};
use tokio_stream::Stream;
use tonic::transport::server::Connected;
use tonic::transport::Server;
use tonic::{Request, Response, Status, Streaming};

const TOKEN: &str = "test-bootstrap-token";
const SNAPSHOT_VERSION: u64 = 7;
const WAIT: Duration = Duration::from_secs(10);

struct Pki {
    dir: TempDir,
    ca: PathBuf,
    server_cert: String,
    server_key: String,
    client_cert: PathBuf,
    client_key: PathBuf,
    foreign_ca: PathBuf,
}

fn ca(name: &str) -> (CertificateParams, KeyPair, String) {
    let key = KeyPair::generate().unwrap();
    let mut params = CertificateParams::new(Vec::<String>::new()).unwrap();
    params.is_ca = IsCa::Ca(BasicConstraints::Unconstrained);
    params.distinguished_name.push(DnType::CommonName, name);
    params.key_usages = vec![KeyUsagePurpose::KeyCertSign, KeyUsagePurpose::CrlSign];
    let pem = params.self_signed(&key).unwrap().pem();
    (params, key, pem)
}

fn leaf(
    names: Vec<String>,
    purpose: ExtendedKeyUsagePurpose,
    issuer: &Issuer<'_, KeyPair>,
) -> (String, String) {
    let key = KeyPair::generate().unwrap();
    let mut params = CertificateParams::new(names).unwrap();
    params.distinguished_name.push(DnType::CommonName, "leaf");
    params.extended_key_usages = vec![purpose];
    let cert = params.signed_by(&key, issuer).unwrap();
    (cert.pem(), key.serialize_pem())
}

fn pki() -> Pki {
    let dir = tempfile::tempdir().unwrap();
    let (ca_params, ca_key, ca_pem) = ca("discovery test ca");
    let issuer = Issuer::new(ca_params, ca_key);
    let (server_cert, server_key) = leaf(
        vec!["localhost".into(), "127.0.0.1".into()],
        ExtendedKeyUsagePurpose::ServerAuth,
        &issuer,
    );
    let (client_cert, client_key) = leaf(vec![], ExtendedKeyUsagePurpose::ClientAuth, &issuer);
    let (_, _, foreign_pem) = ca("someone else");
    let write = |name: &str, contents: &str| {
        let path = dir.path().join(name);
        std::fs::write(&path, contents).unwrap();
        path
    };
    Pki {
        ca: write("ca.crt", &ca_pem),
        client_cert: write("client.crt", &client_cert),
        client_key: write("client.key", &client_key),
        foreign_ca: write("foreign-ca.crt", &foreign_pem),
        server_cert,
        server_key,
        dir,
    }
}

#[derive(Default)]
struct Seen {
    authorization: Vec<Option<String>>,
    acks: Vec<u64>,
}

struct TestDiscovery {
    seen: Arc<Mutex<Seen>>,
}

type ControlStream = Pin<Box<dyn Stream<Item = Result<ControlMessage, Status>> + Send>>;

#[tonic::async_trait]
impl Discovery for TestDiscovery {
    type StreamStream = ControlStream;

    async fn stream(
        &self,
        request: Request<Streaming<RouterMessage>>,
    ) -> Result<Response<ControlStream>, Status> {
        let header = request
            .metadata()
            .get("authorization")
            .and_then(|value| value.to_str().ok())
            .map(str::to_string);
        self.seen.lock().unwrap().authorization.push(header.clone());
        if header.as_deref() != Some(format!("Bearer {TOKEN}").as_str()) {
            return Err(Status::unauthenticated("invalid bearer token"));
        }
        let (tx, rx) = mpsc::channel(4);
        tx.send(Ok(ControlMessage {
            message: Some(control_message::Message::Snapshot(proto::Snapshot {
                version: SNAPSHOT_VERSION,
                ..Default::default()
            })),
        }))
        .await
        .unwrap();
        let seen = self.seen.clone();
        let mut inbound = request.into_inner();
        tokio::spawn(async move {
            while let Ok(Some(message)) = inbound.message().await {
                if let Some(router_message::Message::Ack(ack)) = message.message {
                    seen.lock().unwrap().acks.push(ack.version);
                }
            }
            drop(tx);
        });
        Ok(Response::new(Box::pin(ReceiverStream::new(rx))))
    }
}

struct TlsConn(tokio_rustls::server::TlsStream<TcpStream>);

impl Connected for TlsConn {
    type ConnectInfo = ();

    fn connect_info(&self) -> Self::ConnectInfo {}
}

impl AsyncRead for TlsConn {
    fn poll_read(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &mut ReadBuf<'_>,
    ) -> Poll<std::io::Result<()>> {
        Pin::new(&mut self.get_mut().0).poll_read(cx, buf)
    }
}

impl AsyncWrite for TlsConn {
    fn poll_write(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &[u8],
    ) -> Poll<std::io::Result<usize>> {
        Pin::new(&mut self.get_mut().0).poll_write(cx, buf)
    }

    fn poll_flush(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<std::io::Result<()>> {
        Pin::new(&mut self.get_mut().0).poll_flush(cx)
    }

    fn poll_shutdown(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<std::io::Result<()>> {
        Pin::new(&mut self.get_mut().0).poll_shutdown(cx)
    }
}

fn server_tls(pki: &Pki, require_client_cert: bool) -> ServerConfig {
    let provider = multihull::tls::crypto_provider();
    let certs = multihull::tls::certificates_from_pem(pki.server_cert.as_bytes()).unwrap();
    let key = multihull::tls::private_key_from_pem(pki.server_key.as_bytes()).unwrap();
    let builder = ServerConfig::builder_with_provider(provider.clone())
        .with_safe_default_protocol_versions()
        .unwrap();
    let builder = if require_client_cert {
        let mut roots = RootCertStore::empty();
        for cert in multihull::tls::certificates_from_file(&pki.ca).unwrap() {
            roots.add(cert).unwrap();
        }
        let verifier = WebPkiClientVerifier::builder_with_provider(Arc::new(roots), provider)
            .build()
            .unwrap();
        builder.with_client_cert_verifier(verifier)
    } else {
        builder.with_no_client_auth()
    };
    let mut config = builder.with_single_cert(certs, key).unwrap();
    config.alpn_protocols = vec![b"h2".to_vec()];
    config
}

async fn serve(tls: Option<ServerConfig>) -> (SocketAddr, Arc<Mutex<Seen>>) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let seen = Arc::new(Mutex::new(Seen::default()));
    let service = DiscoveryServer::new(TestDiscovery { seen: seen.clone() });
    match tls {
        None => {
            tokio::spawn(
                Server::builder()
                    .add_service(service)
                    .serve_with_incoming(TcpListenerStream::new(listener)),
            );
        }
        Some(config) => {
            let acceptor = TlsAcceptor::from(Arc::new(config));
            let (conn_tx, conn_rx) = mpsc::channel::<std::io::Result<TlsConn>>(8);
            tokio::spawn(async move {
                while let Ok((stream, _)) = listener.accept().await {
                    let acceptor = acceptor.clone();
                    let conn_tx = conn_tx.clone();
                    tokio::spawn(async move {
                        if let Ok(stream) = acceptor.accept(stream).await {
                            let _ = conn_tx.send(Ok(TlsConn(stream))).await;
                        }
                    });
                }
            });
            tokio::spawn(
                Server::builder()
                    .add_service(service)
                    .serve_with_incoming(ReceiverStream::new(conn_rx)),
            );
        }
    }
    (addr, seen)
}

fn auth(ca: Option<&Path>, identity: Option<(&Path, &Path)>, token: Option<&str>) -> SourceAuth {
    let identity = identity.map(|(cert, key)| ClientIdentity { cert, key });
    SourceAuth {
        tls: authenticated_client_config(ca, identity).unwrap(),
        token: token.map(|token| BearerToken::new(token).unwrap()),
    }
}

async fn session(url: String, auth: SourceAuth) -> (Result<(), SnapshotError>, Arc<Snapshot>) {
    let (tx, mut rx) = watch::channel(Arc::new(Snapshot::default()));
    let mut task = tokio::spawn(async move {
        let mut degraded = None;
        stream_once(&url, "router-test", &tx, &mut degraded, &auth).await
    });
    let ended = tokio::time::timeout(WAIT, async {
        tokio::select! {
            changed = rx.changed() => match changed {
                Ok(()) => None,
                Err(_) => Some((&mut task).await.unwrap()),
            },
            result = &mut task => Some(result.unwrap()),
        }
    })
    .await
    .expect("session applies a snapshot or ends");
    task.abort();
    let snapshot = rx.borrow().clone();
    (ended.unwrap_or(Ok(())), snapshot)
}

async fn wait_for_ack(seen: &Mutex<Seen>) {
    tokio::time::timeout(WAIT, async {
        while seen.lock().unwrap().acks.is_empty() {
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .expect("router acked the snapshot");
}

#[tokio::test]
async fn mtls_and_token_stream_snapshots() {
    let pki = pki();
    let (addr, seen) = serve(Some(server_tls(&pki, true))).await;
    let identity = Some((pki.client_cert.as_path(), pki.client_key.as_path()));
    let (result, snapshot) = session(
        format!("https://localhost:{}", addr.port()),
        auth(Some(&pki.ca), identity, Some(TOKEN)),
    )
    .await;
    assert!(result.is_ok(), "{result:?}");
    assert_eq!(snapshot.version, SNAPSHOT_VERSION);
    wait_for_ack(&seen).await;
    let seen = seen.lock().unwrap();
    assert_eq!(seen.acks, vec![SNAPSHOT_VERSION]);
    assert_eq!(seen.authorization, vec![Some(format!("Bearer {TOKEN}"))]);
}

#[tokio::test]
async fn ip_address_hosts_verify_against_ip_sans() {
    let pki = pki();
    let (addr, seen) = serve(Some(server_tls(&pki, false))).await;
    let (result, snapshot) = session(
        format!("https://{addr}"),
        auth(Some(&pki.ca), None, Some(TOKEN)),
    )
    .await;
    assert!(result.is_ok(), "{result:?}");
    assert_eq!(snapshot.version, SNAPSHOT_VERSION);
    wait_for_ack(&seen).await;
}

#[tokio::test]
async fn wrong_or_missing_token_is_unauthenticated() {
    let pki = pki();
    let (addr, seen) = serve(Some(server_tls(&pki, false))).await;
    let url = format!("https://localhost:{}", addr.port());
    for token in [Some("wrong-token"), None] {
        let (result, snapshot) = session(url.clone(), auth(Some(&pki.ca), None, token)).await;
        match result {
            Err(SnapshotError::Status(status)) => {
                assert_eq!(status.code(), tonic::Code::Unauthenticated, "{status:?}")
            }
            other => panic!("expected unauthenticated, got {other:?}"),
        }
        assert_eq!(snapshot.version, 0);
    }
    let seen = seen.lock().unwrap();
    assert_eq!(
        seen.authorization,
        vec![Some("Bearer wrong-token".to_string()), None]
    );
    assert!(seen.acks.is_empty());
}

#[tokio::test]
async fn server_requiring_client_certificates_rejects_a_router_without_one() {
    let pki = pki();
    let (addr, seen) = serve(Some(server_tls(&pki, true))).await;
    let (result, snapshot) = session(
        format!("https://localhost:{}", addr.port()),
        auth(Some(&pki.ca), None, Some(TOKEN)),
    )
    .await;
    assert!(result.is_err());
    assert_eq!(snapshot.version, 0);
    assert!(seen.lock().unwrap().authorization.is_empty());
}

#[tokio::test]
async fn controller_certificate_from_another_ca_is_rejected() {
    let pki = pki();
    let (addr, seen) = serve(Some(server_tls(&pki, false))).await;
    let (result, snapshot) = session(
        format!("https://localhost:{}", addr.port()),
        auth(Some(&pki.foreign_ca), None, Some(TOKEN)),
    )
    .await;
    let error = multihull::cp::grpc::error_chain(&result.unwrap_err());
    assert!(error.contains("certificate"), "{error}");
    assert_eq!(snapshot.version, 0);
    assert!(seen.lock().unwrap().authorization.is_empty());
}

#[tokio::test]
async fn plaintext_grpc_still_streams_when_allowed() {
    let (addr, seen) = serve(None).await;
    let (result, snapshot) = session(format!("http://{addr}"), auth(None, None, Some(TOKEN))).await;
    assert!(result.is_ok(), "{result:?}");
    assert_eq!(snapshot.version, SNAPSHOT_VERSION);
    wait_for_ack(&seen).await;
}

fn self_signed_localhost() -> (String, String) {
    let certified = rcgen::generate_simple_self_signed(vec!["localhost".to_string()]).unwrap();
    (certified.cert.pem(), certified.signing_key.serialize_pem())
}

#[tokio::test]
async fn https_source_sends_the_bearer_token_to_a_pinned_ca() {
    let (cert, key) = self_signed_localhost();
    let dir = tempfile::tempdir().unwrap();
    let ca = dir.path().join("ca.crt");
    std::fs::write(&ca, &cert).unwrap();
    let body = format!(r#"{{"version":{SNAPSHOT_VERSION},"routes":[]}}"#);
    let upstream = MockUpstream::start_tls(
        MockUpstreamConfig::default()
            .with_body(body)
            .with_required_authorization(format!("Bearer {TOKEN}")),
        cert.as_bytes(),
        key.as_bytes(),
    )
    .await
    .unwrap();
    let url = format!("{}/snapshot.json", upstream.url());
    let tls = || authenticated_client_config(Some(&ca), None).unwrap();

    let mut poller =
        Poller::new(url.clone(), client(tls())).with_token(Some(BearerToken::new(TOKEN).unwrap()));
    let snapshot = poller.fetch().await.unwrap().unwrap();
    assert_eq!(snapshot.version, SNAPSHOT_VERSION);

    let mut anonymous = Poller::new(url.clone(), client(tls()));
    match anonymous.fetch().await {
        Err(SnapshotError::Http(message)) => assert!(message.contains("401"), "{message}"),
        other => panic!("expected 401, got {other:?}"),
    }

    let foreign = pki();
    let mut untrusted = Poller::new(
        url,
        client(authenticated_client_config(Some(&foreign.foreign_ca), None).unwrap()),
    )
    .with_token(Some(BearerToken::new(TOKEN).unwrap()));
    assert!(untrusted.fetch().await.is_err());
    drop(foreign.dir);
}
