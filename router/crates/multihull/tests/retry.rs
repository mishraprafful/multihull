mod common;

use bytes::Bytes;
use common::{
    closed_port_url, endpoint, post, send, single_route, start_proxy, start_proxy_configured,
    RunningProxy,
};
use http_body_util::Full;
use hyper::{Request, StatusCode};
use multihull::core::snapshot::{EdgeError, Endpoint};
use multihull::proxy::{PhaseTimeouts, ProxyConfig};
use router_testkit::{MockUpstream, MockUpstreamConfig};
use std::time::{Duration, Instant};

const COMPLETIONS: &str = "/v1/chat/completions";
const MODAL_EDGE_BODY: &[u8] = b"modal-http: invalid function call";

fn modal_edge_error() -> EdgeError {
    EdgeError {
        statuses: vec![404],
        body_prefix: "modal-http:".into(),
    }
}

async fn stopped_modal_app(body: &'static [u8]) -> MockUpstream {
    MockUpstream::start(
        MockUpstreamConfig::default()
            .with_status(StatusCode::NOT_FOUND)
            .with_body(body),
    )
    .await
    .unwrap()
}

async fn modal_pair(
    stopped: &MockUpstream,
    healthy: &MockUpstream,
    edge_error: Option<EdgeError>,
) -> (RunningProxy, Endpoint) {
    let mut primary = endpoint("modal-a", "modal-a", stopped.url(), 1);
    primary.edge_error = edge_error;
    let proxy = start_proxy(vec![
        primary.clone(),
        endpoint("modal-b", "modal-b", healthy.url(), 2),
    ])
    .await;
    (proxy, primary)
}

async fn open_circuit_of(proxy: &RunningProxy, primary: &Endpoint) {
    for _ in 0..20 {
        let reply = post(proxy, COMPLETIONS, &[], b"{}").await;
        assert_eq!(reply.status, 200, "{:?}", reply.body);
        let now = proxy.state.runtime.now();
        if proxy.state.runtime.endpoint_open(primary, now) {
            return;
        }
    }
    panic!("circuit of {} never opened", primary.id);
}

async fn primary_down_and_fallback(config: ProxyConfig) -> (RunningProxy, MockUpstream) {
    let fallback = MockUpstream::start(MockUpstreamConfig::default())
        .await
        .unwrap();
    let primary = endpoint("kind", "kind", closed_port_url().await, 1);
    let proxy = start_proxy_configured(
        single_route(vec![
            primary.clone(),
            endpoint("modal", "modal", fallback.url(), 2),
        ]),
        config,
    )
    .await;
    open_circuit_of(&proxy, &primary).await;
    (proxy, fallback)
}

#[tokio::test]
async fn first_byte_timeout_on_the_last_healthy_provider_is_retried_there() {
    let config = ProxyConfig {
        timeouts: PhaseTimeouts {
            first_byte: Duration::from_millis(300),
            ..Default::default()
        },
        ..Default::default()
    };
    let (proxy, fallback) = primary_down_and_fallback(config).await;
    fallback.reconfigure(MockUpstreamConfig::default().stalling_first(1, Duration::from_secs(3)));
    let before = fallback.request_count();

    let started = Instant::now();
    let reply = post(&proxy, COMPLETIONS, &[], b"{}").await;

    assert_eq!(reply.status, 200, "{:?}", reply.body);
    assert_eq!(reply.header("x-hull-provider"), Some("modal"));
    assert_eq!(reply.header("x-hull-attempts"), Some("2"));
    assert_eq!(fallback.request_count() - before, 2);
    assert!(started.elapsed() < Duration::from_secs(2));
}

#[tokio::test]
async fn keyed_request_timeout_from_the_last_healthy_provider_is_retried_there() {
    let (proxy, fallback) = primary_down_and_fallback(ProxyConfig::default()).await;
    fallback
        .reconfigure(MockUpstreamConfig::default().failing_first(2, StatusCode::REQUEST_TIMEOUT));
    let before = fallback.request_count();

    let reply = post(&proxy, COMPLETIONS, &[("idempotency-key", "k-408")], b"{}").await;

    assert_eq!(reply.status, 200, "{:?}", reply.body);
    assert_eq!(reply.header("x-hull-provider"), Some("modal"));
    assert_eq!(reply.header("x-hull-attempts"), Some("3"));
    assert_eq!(fallback.request_count() - before, 3);
}

async fn request_timeout_then_healthy() -> (RunningProxy, MockUpstream, MockUpstream) {
    let primary = MockUpstream::start(
        MockUpstreamConfig::default().failing_first(1, StatusCode::REQUEST_TIMEOUT),
    )
    .await
    .unwrap();
    let secondary = MockUpstream::start(MockUpstreamConfig::default())
        .await
        .unwrap();
    let proxy = start_proxy(vec![
        endpoint("kind", "kind", primary.url(), 1),
        endpoint("modal", "modal", secondary.url(), 2),
    ])
    .await;
    (proxy, primary, secondary)
}

#[tokio::test]
async fn keyed_request_timeout_is_retried_on_an_untried_healthy_provider_first() {
    let (proxy, primary, secondary) = request_timeout_then_healthy().await;

    let reply = post(&proxy, COMPLETIONS, &[("idempotency-key", "k-408")], b"{}").await;

    assert_eq!(reply.status, 200, "{:?}", reply.body);
    assert_eq!(reply.header("x-hull-provider"), Some("modal"));
    assert_eq!(reply.header("x-hull-attempts"), Some("2"));
    assert_eq!(primary.request_count(), 1);
    assert_eq!(secondary.request_count(), 1);
}

#[tokio::test]
async fn keyed_request_5xx_is_retried_on_the_same_provider_when_the_untried_one_cannot_take_it() {
    let primary = MockUpstream::start(
        MockUpstreamConfig::default().stalling_first(1, Duration::from_secs(5)),
    )
    .await
    .unwrap();
    let secondary = MockUpstream::start(
        MockUpstreamConfig::default().failing_first(1, StatusCode::INTERNAL_SERVER_ERROR),
    )
    .await
    .unwrap();
    let mut busy = endpoint("kind", "kind", primary.url(), 1);
    busy.max_concurrency = 1;
    let proxy = start_proxy(vec![busy, endpoint("modal", "modal", secondary.url(), 2)]).await;
    let occupying = tokio::spawn({
        let url = proxy.url(COMPLETIONS);
        async move {
            let request = Request::post(url)
                .body(Full::new(Bytes::from_static(b"{}")))
                .unwrap();
            send(request).await
        }
    });
    tokio::time::sleep(Duration::from_millis(200)).await;
    assert_eq!(primary.request_count(), 1);

    let reply = post(&proxy, COMPLETIONS, &[("idempotency-key", "k-500")], b"{}").await;

    assert_eq!(reply.status, 200, "{:?}", reply.body);
    assert_eq!(reply.header("x-hull-provider"), Some("modal"));
    assert_eq!(reply.header("x-hull-attempts"), Some("2"));
    assert_eq!(primary.request_count(), 1);
    assert_eq!(secondary.request_count(), 2);
    assert_eq!(occupying.await.unwrap().status, 200);
}

#[tokio::test]
async fn keyless_post_request_timeout_is_not_retried() {
    let (proxy, primary, secondary) = request_timeout_then_healthy().await;

    let reply = post(&proxy, COMPLETIONS, &[], b"{}").await;

    assert_eq!(reply.status, 408, "{:?}", reply.body);
    assert_eq!(reply.header("x-hull-provider"), Some("kind"));
    assert_eq!(reply.header("x-hull-attempts"), Some("1"));
    assert_eq!(primary.request_count(), 1);
    assert_eq!(secondary.request_count(), 0);
}

#[tokio::test]
async fn connect_failure_is_not_retried_on_the_same_endpoint() {
    let proxy = start_proxy(vec![endpoint("dead", "p1", closed_port_url().await, 1)]).await;

    let reply = post(&proxy, COMPLETIONS, &[("idempotency-key", "k-1")], b"{}").await;

    assert_eq!(reply.status, 502);
    assert_eq!(reply.header("x-hull-attempts"), Some("1"));
}

#[tokio::test]
async fn keyless_post_fails_over_on_a_provider_edge_404() {
    let stopped = stopped_modal_app(MODAL_EDGE_BODY).await;
    let healthy = MockUpstream::start(MockUpstreamConfig::default())
        .await
        .unwrap();
    let (proxy, _) = modal_pair(&stopped, &healthy, Some(modal_edge_error())).await;

    let reply = post(&proxy, COMPLETIONS, &[], b"{}").await;

    assert_eq!(reply.status, 200, "{:?}", reply.body);
    assert_eq!(reply.header("x-hull-provider"), Some("modal-b"));
    assert_eq!(reply.header("x-hull-attempts"), Some("2"));
    assert_eq!(stopped.request_count(), 1);
    assert_eq!(healthy.request_count(), 1);
}

#[tokio::test]
async fn provider_edge_404s_open_the_endpoint_circuit() {
    let stopped = stopped_modal_app(MODAL_EDGE_BODY).await;
    let healthy = MockUpstream::start(MockUpstreamConfig::default())
        .await
        .unwrap();
    let (proxy, primary) = modal_pair(&stopped, &healthy, Some(modal_edge_error())).await;

    for _ in 0..5 {
        let reply = post(&proxy, COMPLETIONS, &[], b"{}").await;
        assert_eq!(reply.status, 200, "{:?}", reply.body);
    }

    let now = proxy.state.runtime.now();
    assert!(proxy.state.runtime.endpoint_open(&primary, now));
    assert_eq!(stopped.request_count(), 5);
}

#[tokio::test]
async fn a_404_from_the_model_is_returned_even_with_the_edge_signature_configured() {
    let model = stopped_modal_app(b"{\"detail\":\"Not Found\"}").await;
    let healthy = MockUpstream::start(MockUpstreamConfig::default())
        .await
        .unwrap();
    let (proxy, _) = modal_pair(&model, &healthy, Some(modal_edge_error())).await;

    let reply = post(&proxy, COMPLETIONS, &[("idempotency-key", "k-404")], b"{}").await;

    assert_eq!(reply.status, 404);
    assert_eq!(reply.header("x-hull-provider"), Some("modal-a"));
    assert_eq!(reply.header("x-hull-attempts"), Some("1"));
    assert_eq!(healthy.request_count(), 0);
}

#[tokio::test]
async fn an_edge_body_without_a_signature_on_the_endpoint_stays_fatal() {
    let stopped = stopped_modal_app(MODAL_EDGE_BODY).await;
    let healthy = MockUpstream::start(MockUpstreamConfig::default())
        .await
        .unwrap();
    let (proxy, _) = modal_pair(&stopped, &healthy, None).await;

    let reply = post(&proxy, COMPLETIONS, &[("idempotency-key", "k-edge")], b"{}").await;

    assert_eq!(reply.status, 404);
    assert_eq!(reply.body.as_ref(), MODAL_EDGE_BODY);
    assert_eq!(reply.header("x-hull-attempts"), Some("1"));
    assert_eq!(healthy.request_count(), 0);
}
