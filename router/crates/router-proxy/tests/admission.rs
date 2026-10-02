mod common;

use common::{endpoint, get, single_route, start_proxy, start_proxy_configured};
use hyper::StatusCode;
use router_core::limit::AdmissionQueue;
use router_core::pressure::PressureConfig;
use router_core::snapshot::DegradedReason;
use router_proxy::ProxyConfig;
use router_testkit::{MockUpstream, MockUpstreamConfig};
use std::time::Duration;

#[tokio::test]
async fn capacity_responses_shrink_the_adaptive_limit() {
    let busy = MockUpstream::start(
        MockUpstreamConfig::default().with_status(StatusCode::TOO_MANY_REQUESTS),
    )
    .await
    .unwrap();
    let a = endpoint("a", "p1", busy.url(), 1);
    let proxy = start_proxy(vec![a.clone()]).await;
    let first = get(&proxy, "/v1/x", &[]).await;
    assert_eq!(first.status, 429);
    let after_one = proxy.state.runtime.status(&a).unwrap().concurrency_limit;
    assert!(after_one < 4, "{after_one}");
    for _ in 0..6 {
        get(&proxy, "/v1/x", &[]).await;
    }
    assert_eq!(proxy.state.runtime.status(&a).unwrap().concurrency_limit, 1);
    assert_eq!(proxy.state.runtime.status(&a).unwrap().circuit, "closed");
}

#[tokio::test]
async fn saturated_route_queues_then_returns_429_with_retry_after() {
    let slow = MockUpstream::start(
        MockUpstreamConfig::default().with_ttft_delay(Duration::from_millis(600)),
    )
    .await
    .unwrap();
    let mut a = endpoint("a", "p1", slow.url(), 1);
    a.max_concurrency = 1;
    let config = ProxyConfig {
        admission: AdmissionQueue {
            max_wait: Duration::from_millis(150),
            bound: 2,
        },
        pressure: PressureConfig {
            sustained: Duration::ZERO,
            ..PressureConfig::default()
        },
        ..ProxyConfig::default()
    };
    let proxy = start_proxy_configured(single_route(vec![a.clone()]), config).await;
    let proxy = std::sync::Arc::new(proxy);

    let mut tasks = Vec::new();
    for _ in 0..6 {
        let proxy = proxy.clone();
        tasks.push(tokio::spawn(async move { get(&proxy, "/v1/x", &[]).await }));
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    let started = std::time::Instant::now();
    let mut ok = 0;
    let mut throttled = 0;
    for task in tasks {
        let reply = task.await.unwrap();
        match reply.status {
            200 => ok += 1,
            429 => {
                throttled += 1;
                assert_eq!(reply.header("retry-after"), Some("1"));
                assert_eq!(reply.header("x-hull-attempts"), Some("0"));
                assert_eq!(reply.json()["error"]["type"], "queue_overflow");
            }
            other => panic!("unexpected status {other}"),
        }
    }
    assert_eq!(ok, 1);
    assert_eq!(throttled, 5);
    assert_eq!(slow.request_count(), 1);
    assert!(started.elapsed() < Duration::from_secs(2));

    let signals = proxy.state.runtime.poll_degraded();
    assert!(signals
        .iter()
        .any(|s| s.service == "llama" && s.reason == DegradedReason::QueueDepth));
    assert!(proxy.state.runtime.poll_degraded().is_empty());

    let later = get(&proxy, "/v1/x", &[]).await;
    assert_eq!(later.status, 200);
}
