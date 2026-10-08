use bytes::Bytes;
use http_body_util::{combinators::BoxBody, BodyExt, Full, StreamBody};
use hyper::body::{Frame, Incoming};
use hyper::service::service_fn;
use hyper::{Request, Response, StatusCode};
use hyper_util::rt::{TokioExecutor, TokioIo};
use hyper_util::server::conn::auto;
use rustls::pki_types::pem::PemObject;
use rustls::pki_types::{CertificateDer, PrivateKeyDer};
use rustls::ServerConfig;
use std::convert::Infallible;
use std::net::SocketAddr;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::Duration;
use tokio::net::TcpListener;
use tokio::sync::{mpsc, watch};
use tokio_rustls::TlsAcceptor;
use tokio_stream::wrappers::ReceiverStream;

#[derive(Clone, Debug)]
pub struct MockUpstreamConfig {
    pub status: StatusCode,
    pub ttft_delay: Duration,
    pub body: Bytes,
    pub sse_chunks: Vec<String>,
    pub sse_chunk_interval: Duration,
    pub sse_first_chunk_delay: Duration,
    pub sse_drop_after_chunks: usize,
    pub sse_raw: bool,
    pub stream_content_type: String,
    pub drop_connection: bool,
    pub etag: Option<String>,
    pub failing_first: usize,
    pub failing_status: StatusCode,
    pub stalling_first: usize,
    pub stall: Duration,
    pub required_authorization: Option<String>,
}

impl Default for MockUpstreamConfig {
    fn default() -> Self {
        Self {
            status: StatusCode::OK,
            ttft_delay: Duration::ZERO,
            body: Bytes::from_static(b"{\"ok\":true}"),
            sse_chunks: Vec::new(),
            sse_chunk_interval: Duration::from_millis(5),
            sse_first_chunk_delay: Duration::ZERO,
            sse_drop_after_chunks: 0,
            sse_raw: false,
            stream_content_type: "text/event-stream".to_string(),
            drop_connection: false,
            etag: None,
            failing_first: 0,
            failing_status: StatusCode::OK,
            stalling_first: 0,
            stall: Duration::ZERO,
            required_authorization: None,
        }
    }
}

impl MockUpstreamConfig {
    pub fn with_status(mut self, status: StatusCode) -> Self {
        self.status = status;
        self
    }

    pub fn with_ttft_delay(mut self, delay: Duration) -> Self {
        self.ttft_delay = delay;
        self
    }

    pub fn with_body(mut self, body: impl Into<Bytes>) -> Self {
        self.body = body.into();
        self
    }

    pub fn with_sse_chunks(mut self, chunks: Vec<String>) -> Self {
        self.sse_chunks = chunks;
        self
    }

    pub fn with_sse_first_chunk_delay(mut self, delay: Duration) -> Self {
        self.sse_first_chunk_delay = delay;
        self
    }

    pub fn dropping_connections(mut self) -> Self {
        self.drop_connection = true;
        self
    }

    pub fn dropping_sse_after(mut self, chunks: usize) -> Self {
        self.sse_drop_after_chunks = chunks;
        self
    }

    pub fn sending_raw_chunks(mut self) -> Self {
        self.sse_raw = true;
        self
    }

    pub fn with_stream_content_type(mut self, content_type: impl Into<String>) -> Self {
        self.stream_content_type = content_type.into();
        self
    }

    pub fn with_etag(mut self, etag: impl Into<String>) -> Self {
        self.etag = Some(etag.into());
        self
    }

    pub fn failing_first(mut self, requests: usize, status: StatusCode) -> Self {
        self.failing_first = requests;
        self.failing_status = status;
        self
    }

    pub fn with_required_authorization(mut self, value: impl Into<String>) -> Self {
        self.required_authorization = Some(value.into());
        self
    }

    pub fn stalling_first(mut self, requests: usize, stall: Duration) -> Self {
        self.stalling_first = requests;
        self.stall = stall;
        self
    }
}

pub struct MockUpstream {
    addr: SocketAddr,
    tls: bool,
    requests: Arc<AtomicUsize>,
    not_modified: Arc<AtomicUsize>,
    since_reconfigure: Arc<AtomicUsize>,
    config_tx: watch::Sender<MockUpstreamConfig>,
    shutdown_tx: Option<watch::Sender<bool>>,
}

impl MockUpstream {
    pub async fn start(config: MockUpstreamConfig) -> std::io::Result<Self> {
        Self::start_with_tls(config, None).await
    }

    pub async fn start_tls(
        config: MockUpstreamConfig,
        cert_pem: &[u8],
        key_pem: &[u8],
    ) -> std::io::Result<Self> {
        let server_config = server_config_from_pem(cert_pem, key_pem)
            .map_err(|error| std::io::Error::new(std::io::ErrorKind::InvalidInput, error))?;
        Self::start_with_tls(config, Some(TlsAcceptor::from(Arc::new(server_config)))).await
    }

    async fn start_with_tls(
        config: MockUpstreamConfig,
        acceptor: Option<TlsAcceptor>,
    ) -> std::io::Result<Self> {
        let listener = TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, 0)).await?;
        let addr = listener.local_addr()?;
        let requests = Arc::new(AtomicUsize::new(0));
        let not_modified = Arc::new(AtomicUsize::new(0));
        let since_reconfigure = Arc::new(AtomicUsize::new(0));
        let (config_tx, config_rx) = watch::channel(config);
        let (shutdown_tx, mut shutdown_rx) = watch::channel(false);
        let counter = requests.clone();
        let not_modified_counter = not_modified.clone();
        let since_counter = since_reconfigure.clone();
        let tls = acceptor.is_some();
        tokio::spawn(async move {
            loop {
                tokio::select! {
                    accepted = listener.accept() => {
                        let Ok((stream, _)) = accepted else { break };
                        if config_rx.borrow().drop_connection {
                            counter.fetch_add(1, Ordering::SeqCst);
                            drop(stream);
                            continue;
                        }
                        let config_rx = config_rx.clone();
                        let counter = counter.clone();
                        let not_modified_counter = not_modified_counter.clone();
                        let since_counter = since_counter.clone();
                        let acceptor = acceptor.clone();
                        tokio::spawn(async move {
                            let service = service_fn(move |req| {
                                let config = config_rx.borrow().clone();
                                let counters = Counters {
                                    requests: counter.clone(),
                                    not_modified: not_modified_counter.clone(),
                                    since_reconfigure: since_counter.clone(),
                                };
                                async move { respond(req, config, counters).await }
                            });
                            match acceptor {
                                Some(acceptor) => {
                                    let Ok(stream) = acceptor.accept(stream).await else { return };
                                    let _ = auto::Builder::new(TokioExecutor::new())
                                        .serve_connection(TokioIo::new(stream), service)
                                        .await;
                                }
                                None => {
                                    let _ = auto::Builder::new(TokioExecutor::new())
                                        .serve_connection(TokioIo::new(stream), service)
                                        .await;
                                }
                            }
                        });
                    }
                    _ = shutdown_rx.changed() => break,
                }
            }
        });
        Ok(Self {
            addr,
            tls,
            requests,
            not_modified,
            since_reconfigure,
            config_tx,
            shutdown_tx: Some(shutdown_tx),
        })
    }

    pub fn not_modified_count(&self) -> usize {
        self.not_modified.load(Ordering::SeqCst)
    }

    pub fn addr(&self) -> SocketAddr {
        self.addr
    }

    pub fn url(&self) -> String {
        let scheme = if self.tls { "https" } else { "http" };
        format!("{scheme}://localhost:{}", self.addr.port())
    }

    pub fn request_count(&self) -> usize {
        self.requests.load(Ordering::SeqCst)
    }

    pub fn reconfigure(&self, config: MockUpstreamConfig) {
        self.config_tx.send_modify(|current| {
            self.since_reconfigure.store(0, Ordering::SeqCst);
            *current = config;
        });
    }
}

impl Drop for MockUpstream {
    fn drop(&mut self) {
        if let Some(tx) = self.shutdown_tx.take() {
            let _ = tx.send(true);
        }
    }
}

fn server_config_from_pem(
    cert_pem: &[u8],
    key_pem: &[u8],
) -> Result<ServerConfig, Box<dyn std::error::Error + Send + Sync>> {
    let certs = CertificateDer::pem_slice_iter(cert_pem).collect::<Result<Vec<_>, _>>()?;
    let key = PrivateKeyDer::from_pem_slice(key_pem)?;
    let provider = Arc::new(rustls::crypto::ring::default_provider());
    let mut config = ServerConfig::builder_with_provider(provider)
        .with_safe_default_protocol_versions()?
        .with_no_client_auth()
        .with_single_cert(certs, key)?;
    config.alpn_protocols = vec![b"h2".to_vec(), b"http/1.1".to_vec()];
    Ok(config)
}

#[derive(Clone)]
struct Counters {
    requests: Arc<AtomicUsize>,
    not_modified: Arc<AtomicUsize>,
    since_reconfigure: Arc<AtomicUsize>,
}

async fn respond(
    req: Request<Incoming>,
    config: MockUpstreamConfig,
    counters: Counters,
) -> Result<Response<BoxBody<Bytes, std::io::Error>>, Infallible> {
    counters.requests.fetch_add(1, Ordering::SeqCst);
    if let Some(required) = &config.required_authorization {
        let presented = req
            .headers()
            .get("authorization")
            .is_some_and(|value| value.as_bytes() == required.as_bytes());
        if !presented {
            let response = Response::builder()
                .status(StatusCode::UNAUTHORIZED)
                .body(
                    Full::new(Bytes::new())
                        .map_err(|never| match never {})
                        .boxed(),
                )
                .expect("valid response");
            return Ok(response);
        }
    }
    let index = counters.since_reconfigure.fetch_add(1, Ordering::SeqCst);
    if index < config.stalling_first {
        tokio::time::sleep(config.stall).await;
    }
    if config.ttft_delay > Duration::ZERO {
        tokio::time::sleep(config.ttft_delay).await;
    }
    if index < config.failing_first {
        let response = Response::builder()
            .status(config.failing_status)
            .header("content-type", "application/json")
            .body(
                Full::new(Bytes::from_static(b"{\"error\":\"mock failure\"}"))
                    .map_err(|never| match never {})
                    .boxed(),
            )
            .expect("valid response");
        return Ok(response);
    }
    if let Some(etag) = &config.etag {
        let matches = req
            .headers()
            .get("if-none-match")
            .and_then(|value| value.to_str().ok())
            .is_some_and(|value| value == etag);
        if matches {
            counters.not_modified.fetch_add(1, Ordering::SeqCst);
            let response = Response::builder()
                .status(StatusCode::NOT_MODIFIED)
                .header("etag", etag)
                .body(
                    Full::new(Bytes::new())
                        .map_err(|never| match never {})
                        .boxed(),
                )
                .expect("valid response");
            return Ok(response);
        }
    }
    if config.sse_chunks.is_empty() {
        let body = Full::new(config.body)
            .map_err(|never| match never {})
            .boxed();
        let mut builder = Response::builder()
            .status(config.status)
            .header("content-type", "application/json");
        if let Some(etag) = &config.etag {
            builder = builder.header("etag", etag);
        }
        let response = builder.body(body).expect("valid response");
        return Ok(response);
    }
    let (tx, rx) = mpsc::channel::<Result<Frame<Bytes>, std::io::Error>>(16);
    let interval = config.sse_chunk_interval;
    let first_chunk_delay = config.sse_first_chunk_delay;
    let drop_after = config.sse_drop_after_chunks;
    let raw = config.sse_raw;
    let chunks = config.sse_chunks.clone();
    tokio::spawn(async move {
        tokio::time::sleep(first_chunk_delay).await;
        for (sent, chunk) in chunks.into_iter().enumerate() {
            if drop_after > 0 && sent == drop_after {
                let _ = tx
                    .send(Err(std::io::Error::new(
                        std::io::ErrorKind::ConnectionAborted,
                        "mock upstream dropped the stream",
                    )))
                    .await;
                return;
            }
            let bytes = if raw {
                Bytes::from(chunk)
            } else {
                Bytes::from(format!("data: {chunk}\n\n"))
            };
            let frame = Frame::data(bytes);
            if tx.send(Ok(frame)).await.is_err() {
                return;
            }
            tokio::time::sleep(interval).await;
        }
        if raw {
            return;
        }
        let _ = tx
            .send(Ok(Frame::data(Bytes::from_static(b"data: [DONE]\n\n"))))
            .await;
    });
    let body = StreamBody::new(ReceiverStream::new(rx)).boxed();
    let response = Response::builder()
        .status(config.status)
        .header("content-type", config.stream_content_type.as_str())
        .body(body)
        .expect("valid response");
    Ok(response)
}
