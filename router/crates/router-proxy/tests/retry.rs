mod common;

use common::{closed_port_url, endpoint, post, start_proxy};
use hyper::StatusCode;
use router_testkit::{MockUpstream, MockUpstreamConfig};

const COMPLETIONS: &str = "/v1/chat/completions";

#[tokio::test]
async fn request_timeout_fails_over_to_an_untried_healthy_provider_first() {
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

    let reply = post(&proxy, COMPLETIONS, &[], b"{}").await;

    assert_eq!(reply.status, 200, "{:?}", reply.body);
    assert_eq!(reply.header("x-hull-provider"), Some("modal"));
    assert_eq!(reply.header("x-hull-attempts"), Some("2"));
    assert_eq!(primary.request_count(), 1);
    assert_eq!(secondary.request_count(), 1);
}

#[tokio::test]
async fn connect_failure_is_not_retried_on_the_same_endpoint() {
    let proxy = start_proxy(vec![endpoint("dead", "p1", closed_port_url().await, 1)]).await;

    let reply = post(&proxy, COMPLETIONS, &[("idempotency-key", "k-1")], b"{}").await;

    assert_eq!(reply.status, 502);
    assert_eq!(reply.header("x-hull-attempts"), Some("1"));
}
