mod common;

use common::{endpoint, get, single_route, start_proxy, start_proxy_configured};
use hyper::StatusCode;
use multihull::core::circuit::CircuitConfig;
use multihull::proxy::ProxyConfig;
use router_testkit::{MockUpstream, MockUpstreamConfig};
use std::time::Duration;

fn fast_circuits() -> ProxyConfig {
    ProxyConfig {
        circuit: CircuitConfig {
            base_backoff: Duration::from_millis(300),
            jitter_fraction: 0.0,
            half_open_ramp: Duration::from_millis(30),
            ..CircuitConfig::default()
        },
        ..ProxyConfig::default()
    }
}

#[tokio::test]
async fn provider_circuit_opens_after_endpoint_failures_and_recovers() {
    let failing =
        MockUpstream::start(MockUpstreamConfig::default().with_status(StatusCode::BAD_GATEWAY))
            .await
            .unwrap();
    let sibling = MockUpstream::start(MockUpstreamConfig::default())
        .await
        .unwrap();
    let fallback = MockUpstream::start(MockUpstreamConfig::default())
        .await
        .unwrap();
    let a = endpoint("a", "p1", failing.url(), 1);
    let b = endpoint("b", "p1", sibling.url(), 1);
    let c = endpoint("c", "p2", fallback.url(), 2);
    let proxy = start_proxy_configured(
        single_route(vec![a.clone(), b.clone(), c.clone()]),
        fast_circuits(),
    )
    .await;
    proxy.state.runtime.endpoint(&b);

    for _ in 0..40 {
        let reply = get(&proxy, "/v1/x", &[]).await;
        assert!(
            reply.status == 200 || reply.status == 502,
            "{}",
            reply.status
        );
    }
    let now = proxy.state.runtime.now();
    assert!(proxy.state.runtime.provider_open("p1", now));
    assert_eq!(proxy.state.runtime.status(&a).unwrap().circuit, "open");
    assert_eq!(proxy.state.runtime.status(&b).unwrap().circuit, "closed");
    assert!(
        proxy
            .state
            .runtime
            .status(&b)
            .unwrap()
            .provider_circuit_open
    );

    let sibling_before = sibling.request_count();
    for _ in 0..10 {
        let reply = get(&proxy, "/v1/x", &[]).await;
        assert_eq!(reply.status, 200);
        assert_eq!(reply.endpoint(), "c");
        assert_eq!(reply.header("x-hull-attempts"), Some("1"));
    }
    assert_eq!(sibling.request_count(), sibling_before);

    failing.reconfigure(MockUpstreamConfig::default());
    tokio::time::sleep(Duration::from_millis(400)).await;
    let mut closed = false;
    for _ in 0..60 {
        let reply = get(&proxy, "/v1/x", &[]).await;
        assert_eq!(reply.status, 200);
        if proxy.state.runtime.status(&a).unwrap().circuit == "closed" {
            closed = true;
            break;
        }
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    assert!(closed, "endpoint a never closed its circuit");
    let now = proxy.state.runtime.now();
    assert!(!proxy.state.runtime.provider_open("p1", now));
    assert!(
        !proxy
            .state
            .runtime
            .status(&b)
            .unwrap()
            .provider_circuit_open
    );
    let sibling_before = sibling.request_count();
    for _ in 0..30 {
        get(&proxy, "/v1/x", &[]).await;
    }
    assert!(sibling.request_count() > sibling_before);
}

#[tokio::test]
async fn panic_threshold_keeps_routing_when_every_circuit_is_open() {
    let a_up =
        MockUpstream::start(MockUpstreamConfig::default().with_status(StatusCode::BAD_GATEWAY))
            .await
            .unwrap();
    let b_up =
        MockUpstream::start(MockUpstreamConfig::default().with_status(StatusCode::BAD_GATEWAY))
            .await
            .unwrap();
    let a = endpoint("a", "p1", a_up.url(), 1);
    let b = endpoint("b", "p2", b_up.url(), 1);
    let proxy = start_proxy(vec![a.clone(), b.clone()]).await;

    for _ in 0..12 {
        let reply = get(&proxy, "/v1/x", &[]).await;
        assert_eq!(reply.status, 502);
    }
    let now = proxy.state.runtime.now();
    assert!(proxy.state.runtime.endpoint_open(&a, now));
    assert!(proxy.state.runtime.endpoint_open(&b, now));

    let reply = get(&proxy, "/v1/x", &[]).await;
    assert_eq!(reply.status, 502);
    assert!(reply.header("x-hull-endpoint").is_some());
    assert_ne!(reply.json()["error"]["type"], "no_healthy_upstream");
}
