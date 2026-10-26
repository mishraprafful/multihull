mod common;

use bytes::Bytes;
use common::{endpoint, get, post, send, single_route, start_proxy, start_proxy_configured};
use http::Request;
use http_body_util::Full;
use multihull::core::snapshot::EndpointType;
use multihull::proxy::{PhaseTimeouts, ProxyConfig};
use router_testkit::{MockUpstream, MockUpstreamConfig};
use std::time::Duration;

const PREDICTION: &[u8] = br#"{"input":{"text":"hi"}}"#;

async fn replicate_upstream() -> (MockUpstream, multihull::core::snapshot::Endpoint) {
    let upstream = MockUpstream::start(MockUpstreamConfig::default())
        .await
        .unwrap();
    let deployment = format!("{}/v1/deployments/acme/echo", upstream.url());
    let mut replicate = endpoint("a", "replicate", deployment, 1);
    replicate.kind = EndpointType::Replicate;
    (upstream, replicate)
}

#[tokio::test]
async fn a_replicate_only_route_answers_anything_but_prediction_posts_locally() {
    let (upstream, replicate) = replicate_upstream().await;
    let proxy = start_proxy(vec![replicate]).await;

    for method in ["PATCH", "DELETE", "PUT"] {
        let request = Request::builder()
            .method(method)
            .uri(proxy.url("/"))
            .body(Full::new(Bytes::from_static(PREDICTION)))
            .unwrap();
        let reply = send(request).await;
        assert_eq!(reply.status, 404, "{method}");
        assert_eq!(reply.json()["error"]["type"], "not_forwarded");
    }
    assert_eq!(get(&proxy, "/health-check", &[]).await.status, 404);
    assert_eq!(get(&proxy, "/predictions", &[]).await.status, 404);
    assert_eq!(
        post(&proxy, "/predictions/abc/cancel", &[], PREDICTION)
            .await
            .status,
        404
    );
    assert_eq!(upstream.request_count(), 0);

    let reply = post(&proxy, "/predictions", &[], PREDICTION).await;
    assert_eq!(reply.status, 200);
    assert_eq!(upstream.request_count(), 1);
}

#[tokio::test]
async fn other_requests_on_a_mixed_route_skip_the_replicate_endpoint() {
    let (replicate_upstream, replicate) = replicate_upstream().await;
    let cog = MockUpstream::start(MockUpstreamConfig::default())
        .await
        .unwrap();
    let proxy = start_proxy(vec![replicate, endpoint("b", "local", cog.url(), 2)]).await;

    let health = get(&proxy, "/health-check", &[]).await;
    assert_eq!((health.status, health.endpoint()), (200, "b"));
    assert_eq!(replicate_upstream.request_count(), 0);

    let prediction = post(&proxy, "/predictions", &[], PREDICTION).await;
    assert_eq!((prediction.status, prediction.endpoint()), (200, "a"));
    assert_eq!(replicate_upstream.request_count(), 1);
}

#[tokio::test]
async fn predictions_wait_less_than_the_first_byte_timeout() {
    let upstream =
        MockUpstream::start(MockUpstreamConfig::default().with_required_header("prefer", "wait=5"))
            .await
            .unwrap();
    let deployment = format!("{}/v1/deployments/acme/echo", upstream.url());
    let mut replicate = endpoint("a", "replicate", deployment, 1);
    replicate.kind = EndpointType::Replicate;
    replicate
        .inject_headers
        .insert("Prefer".into(), "wait=60".into());
    let config = ProxyConfig {
        timeouts: PhaseTimeouts {
            first_byte: Duration::from_secs(10),
            ..PhaseTimeouts::default()
        },
        ..ProxyConfig::default()
    };
    let proxy = start_proxy_configured(single_route(vec![replicate]), config).await;
    let reply = post(
        &proxy,
        "/predictions",
        &[("prefer", "respond-async")],
        PREDICTION,
    )
    .await;
    assert_eq!(reply.status, 200);
}

#[tokio::test]
async fn other_endpoints_keep_the_client_preference() {
    let cog = MockUpstream::start(
        MockUpstreamConfig::default().with_required_header("prefer", "respond-async"),
    )
    .await
    .unwrap();
    let proxy = start_proxy(vec![endpoint("b", "local", cog.url(), 1)]).await;
    let reply = post(
        &proxy,
        "/predictions",
        &[("prefer", "respond-async")],
        PREDICTION,
    )
    .await;
    assert_eq!(reply.status, 200);
}
