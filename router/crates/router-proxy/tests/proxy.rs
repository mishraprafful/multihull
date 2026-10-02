mod common;

use arc_swap::ArcSwap;
use bytes::Bytes;
use common::{closed_port_url, endpoint, start_proxy, start_proxy_with};
use http_body_util::{BodyExt, Empty, Full};
use hyper::Request;
use hyper_util::client::legacy::Client;
use hyper_util::rt::TokioExecutor;
use router_core::snapshot::{Route, Snapshot};
use router_proxy::{ProxyConfig, ProxyState};
use router_testkit::{MockUpstream, MockUpstreamConfig};
use std::sync::Arc;
use std::time::Duration;
use tokio::net::TcpListener;

#[tokio::test]
async fn proxies_request_to_healthy_upstream() {
    let upstream = MockUpstream::start(MockUpstreamConfig::default())
        .await
        .unwrap();
    let proxy = start_proxy(vec![endpoint("a", "p1", upstream.url(), 1)]).await;
    let client: Client<_, Full<Bytes>> = Client::builder(TokioExecutor::new()).build_http();
    let request = Request::post(format!("http://{}/v1/chat/completions", proxy.addr))
        .header("content-type", "application/json")
        .body(Full::new(Bytes::from_static(b"{\"model\":\"x\"}")))
        .unwrap();
    let response = client.request(request).await.unwrap();
    assert_eq!(response.status(), 200);
    assert_eq!(response.headers().get("x-hull-endpoint").unwrap(), "a");
    assert_eq!(response.headers().get("x-hull-attempts").unwrap(), "1");
    let body = response.into_body().collect().await.unwrap().to_bytes();
    assert_eq!(&body[..], b"{\"ok\":true}");
    assert_eq!(upstream.request_count(), 1);
}

#[tokio::test]
async fn connect_failure_retries_on_another_provider() {
    let upstream = MockUpstream::start(MockUpstreamConfig::default())
        .await
        .unwrap();
    let dead = closed_port_url().await;
    let proxy = start_proxy(vec![
        endpoint("dead", "p1", dead, 1),
        endpoint("alive", "p2", upstream.url(), 2),
    ])
    .await;
    let client: Client<_, Full<Bytes>> = Client::builder(TokioExecutor::new()).build_http();
    let request = Request::post(format!("http://{}/v1/chat/completions", proxy.addr))
        .body(Full::new(Bytes::from_static(b"{}")))
        .unwrap();
    let response = client.request(request).await.unwrap();
    assert_eq!(response.status(), 200);
    assert_eq!(response.headers().get("x-hull-endpoint").unwrap(), "alive");
    assert_eq!(response.headers().get("x-hull-attempts").unwrap(), "2");
    assert_eq!(upstream.request_count(), 1);
}

#[tokio::test]
async fn capacity_status_retries_and_non_idempotent_transient_does_not() {
    let busy = MockUpstream::start(
        MockUpstreamConfig::default().with_status(hyper::StatusCode::TOO_MANY_REQUESTS),
    )
    .await
    .unwrap();
    let healthy = MockUpstream::start(MockUpstreamConfig::default())
        .await
        .unwrap();
    let proxy = start_proxy(vec![
        endpoint("busy", "p1", busy.url(), 1),
        endpoint("healthy", "p2", healthy.url(), 2),
    ])
    .await;
    let client: Client<_, Full<Bytes>> = Client::builder(TokioExecutor::new()).build_http();
    let request = Request::post(format!("http://{}/v1/x", proxy.addr))
        .body(Full::new(Bytes::from_static(b"{}")))
        .unwrap();
    let response = client.request(request).await.unwrap();
    assert_eq!(response.status(), 200);
    assert_eq!(
        response.headers().get("x-hull-endpoint").unwrap(),
        "healthy"
    );

    busy.reconfigure(MockUpstreamConfig::default().with_status(hyper::StatusCode::BAD_GATEWAY));
    let request = Request::post(format!("http://{}/v1/x", proxy.addr))
        .body(Full::new(Bytes::from_static(b"{}")))
        .unwrap();
    let response = client.request(request).await.unwrap();
    assert_eq!(response.status(), 502);
    assert_eq!(response.headers().get("x-hull-endpoint").unwrap(), "busy");

    let request = Request::get(format!("http://{}/v1/x", proxy.addr))
        .body(Full::new(Bytes::new()))
        .unwrap();
    let response = client.request(request).await.unwrap();
    assert_eq!(response.status(), 200);
    assert_eq!(
        response.headers().get("x-hull-endpoint").unwrap(),
        "healthy"
    );
}

#[tokio::test]
async fn server_error_with_body_retries_only_for_idempotent_requests() {
    let broken = MockUpstream::start(
        MockUpstreamConfig::default()
            .with_status(hyper::StatusCode::INTERNAL_SERVER_ERROR)
            .with_body("{\"error\":{\"type\":\"internal\"}}"),
    )
    .await
    .unwrap();
    let healthy = MockUpstream::start(MockUpstreamConfig::default())
        .await
        .unwrap();
    let proxy = start_proxy(vec![
        endpoint("broken", "p1", broken.url(), 1),
        endpoint("healthy", "p2", healthy.url(), 2),
    ])
    .await;
    let client: Client<_, Full<Bytes>> = Client::builder(TokioExecutor::new()).build_http();

    let request = Request::post(format!("http://{}/v1/x", proxy.addr))
        .body(Full::new(Bytes::from_static(b"{}")))
        .unwrap();
    let response = client.request(request).await.unwrap();
    assert_eq!(response.status(), 500);
    assert_eq!(response.headers().get("x-hull-endpoint").unwrap(), "broken");
    assert_eq!(response.headers().get("x-hull-attempts").unwrap(), "1");
    let body = response.into_body().collect().await.unwrap().to_bytes();
    assert_eq!(&body[..], b"{\"error\":{\"type\":\"internal\"}}");

    let request = Request::post(format!("http://{}/v1/x", proxy.addr))
        .header("idempotency-key", "req-1")
        .body(Full::new(Bytes::from_static(b"{}")))
        .unwrap();
    let response = client.request(request).await.unwrap();
    assert_eq!(response.status(), 200);
    assert_eq!(
        response.headers().get("x-hull-endpoint").unwrap(),
        "healthy"
    );
    assert_eq!(response.headers().get("x-hull-attempts").unwrap(), "2");

    let request = Request::get(format!("http://{}/v1/x", proxy.addr))
        .body(Full::new(Bytes::new()))
        .unwrap();
    let response = client.request(request).await.unwrap();
    assert_eq!(response.status(), 200);
    assert_eq!(
        response.headers().get("x-hull-endpoint").unwrap(),
        "healthy"
    );
    assert_eq!(broken.request_count(), 3);
    assert_eq!(healthy.request_count(), 2);
}

#[tokio::test]
async fn streams_sse_chunks_through() {
    let upstream = MockUpstream::start(MockUpstreamConfig::default().with_sse_chunks(vec![
        "one".into(),
        "two".into(),
        "three".into(),
    ]))
    .await
    .unwrap();
    let proxy = start_proxy(vec![endpoint("a", "p1", upstream.url(), 1)]).await;
    let client: Client<_, Empty<Bytes>> = Client::builder(TokioExecutor::new()).build_http();
    let request = Request::get(format!("http://{}/v1/stream", proxy.addr))
        .body(Empty::new())
        .unwrap();
    let response = client.request(request).await.unwrap();
    assert_eq!(response.status(), 200);
    assert_eq!(
        response.headers().get("content-type").unwrap(),
        "text/event-stream"
    );
    let body = tokio::time::timeout(Duration::from_secs(5), response.into_body().collect())
        .await
        .unwrap()
        .unwrap()
        .to_bytes();
    let text = String::from_utf8(body.to_vec()).unwrap();
    assert!(text.contains("data: one\n\n"));
    assert!(text.contains("data: three\n\n"));
    assert!(text.ends_with("data: [DONE]\n\n"));
}

#[tokio::test]
async fn missing_route_empty_route_and_dead_upstream_map_to_404_503_502() {
    let proxy = start_proxy_with(Snapshot::default()).await;
    let client: Client<_, Empty<Bytes>> = Client::builder(TokioExecutor::new()).build_http();
    let response = client
        .request(
            Request::get(format!("http://{}/v1/x", proxy.addr))
                .body(Empty::new())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), 404);

    let proxy = start_proxy(vec![]).await;
    let response = client
        .request(
            Request::get(format!("http://{}/v1/x", proxy.addr))
                .body(Empty::new())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), 503);

    let dead = closed_port_url().await;
    let proxy = start_proxy(vec![endpoint("dead", "p1", dead, 1)]).await;
    let response = client
        .request(
            Request::get(format!("http://{}/v1/x", proxy.addr))
                .body(Empty::new())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), 502);
    assert!(response.headers().contains_key("retry-after"));
}

#[tokio::test]
async fn api_key_is_required_when_route_has_hashes() {
    let upstream = MockUpstream::start(MockUpstreamConfig::default())
        .await
        .unwrap();
    let key = router_auth::ApiKey::parse("hull_team1_notasecretjustatest").unwrap();
    let snapshot = Snapshot {
        version: 1,
        at: None,
        routes: vec![Route {
            id: "secure".into(),
            auth: router_core::snapshot::Auth {
                api_key_hashes: vec![key.hash()],
            },
            endpoints: vec![endpoint("a", "p1", upstream.url(), 1)],
            ..Default::default()
        }],
    };
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let state = ProxyState::new(
        ProxyConfig::default(),
        Arc::new(ArcSwap::from_pointee(snapshot)),
    );
    tokio::spawn(router_proxy::serve(state, listener, std::future::pending()));
    let client: Client<_, Empty<Bytes>> = Client::builder(TokioExecutor::new()).build_http();
    let denied = client
        .request(
            Request::get(format!("http://{addr}/v1/x"))
                .body(Empty::new())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(denied.status(), 401);
    let allowed = client
        .request(
            Request::get(format!("http://{addr}/v1/x"))
                .header("authorization", "Bearer hull_team1_notasecretjustatest")
                .body(Empty::new())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(allowed.status(), 200);
    assert_eq!(upstream.request_count(), 1);
}
