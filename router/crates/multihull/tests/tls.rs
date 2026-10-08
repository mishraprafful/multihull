mod common;

use arc_swap::ArcSwap;
use bytes::Bytes;
use common::{endpoint, get, single_route, start_proxy_configured};
use http_body_util::{BodyExt, Full};
use hyper::Request;
use multihull::proxy::{ProxyConfig, ProxyState};
use multihull::tls::TlsReloader;
use router_testkit::{MockUpstream, MockUpstreamConfig};
use std::path::Path;
use std::sync::Arc;
use std::time::Duration;
use tokio::net::TcpListener;

fn self_signed() -> (String, String) {
    let certified =
        rcgen::generate_simple_self_signed(vec!["localhost".to_string(), "127.0.0.1".to_string()])
            .unwrap();
    (certified.cert.pem(), certified.signing_key.serialize_pem())
}

fn write(dir: &Path, name: &str, contents: &str) -> std::path::PathBuf {
    let path = dir.join(name);
    std::fs::write(&path, contents).unwrap();
    path
}

#[tokio::test]
async fn https_upstream_with_a_private_ca_is_proxied() {
    let dir = tempfile::tempdir().unwrap();
    let (cert, key) = self_signed();
    let ca = write(dir.path(), "ca.pem", &cert);
    let upstream = MockUpstream::start_tls(
        MockUpstreamConfig::default().with_body("{\"secure\":true}"),
        cert.as_bytes(),
        key.as_bytes(),
    )
    .await
    .unwrap();
    assert!(upstream.url().starts_with("https://"));

    let trusting = ProxyConfig {
        upstream_ca: Some(ca),
        ..ProxyConfig::default()
    };
    let proxy = start_proxy_configured(
        single_route(vec![endpoint("secure", "modal", upstream.url(), 1)]),
        trusting,
    )
    .await;
    let reply = get(&proxy, "/v1/x", &[]).await;
    assert_eq!(reply.status, 200);
    assert_eq!(reply.endpoint(), "secure");
    assert_eq!(&reply.body[..], b"{\"secure\":true}");
    assert_eq!(upstream.request_count(), 1);

    let untrusting = start_proxy_configured(
        single_route(vec![endpoint("secure", "modal", upstream.url(), 1)]),
        ProxyConfig::default(),
    )
    .await;
    let rejected = get(&untrusting, "/v1/x", &[]).await;
    assert_eq!(rejected.status, 502);
    assert_eq!(upstream.request_count(), 1);
}

async fn https_get(ca: &Path, url: &str) -> Result<(u16, Bytes), String> {
    let client = multihull::tls::https_client(Duration::from_secs(2), Some(ca)).unwrap();
    let request = Request::get(url).body(Full::new(Bytes::new())).unwrap();
    let response = client
        .request(request)
        .await
        .map_err(|error| error.to_string())?;
    let status = response.status().as_u16();
    let body = response.into_body().collect().await.unwrap().to_bytes();
    Ok((status, body))
}

#[tokio::test]
async fn listener_terminates_tls_and_reloads_certificates() {
    let dir = tempfile::tempdir().unwrap();
    let (first_cert, first_key) = self_signed();
    let cert_path = write(dir.path(), "tls.crt", &first_cert);
    let key_path = write(dir.path(), "tls.key", &first_key);
    let first_ca = write(dir.path(), "first-ca.pem", &first_cert);

    let upstream = MockUpstream::start(MockUpstreamConfig::default())
        .await
        .unwrap();
    let snapshot = single_route(vec![endpoint("a", "p1", upstream.url(), 1)]);
    let state = ProxyState::new(
        ProxyConfig::default(),
        Arc::new(ArcSwap::from_pointee(snapshot)),
    );
    let reloader = Arc::new(TlsReloader::new(cert_path.clone(), key_path.clone()).unwrap());
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(multihull::proxy::serve_tls(
        state,
        listener,
        reloader.clone(),
        std::future::pending(),
    ));
    let url = format!("https://localhost:{}/v1/x", addr.port());

    let (status, body) = https_get(&first_ca, &url).await.unwrap();
    assert_eq!(status, 200);
    assert_eq!(&body[..], b"{\"ok\":true}");

    let (second_cert, second_key) = self_signed();
    let second_ca = write(dir.path(), "second-ca.pem", &second_cert);
    assert!(https_get(&second_ca, &url).await.is_err());

    std::fs::write(&cert_path, &second_cert).unwrap();
    std::fs::write(&key_path, &second_key).unwrap();
    reloader.reload().unwrap();

    let (status, _) = https_get(&second_ca, &url).await.unwrap();
    assert_eq!(status, 200);
    assert!(https_get(&first_ca, &url).await.is_err());
    assert_eq!(upstream.request_count(), 2);
}
