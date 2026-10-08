mod common;

use common::{endpoint, get, single_route, start_proxy, start_proxy_configured};
use http::StatusCode;
use multihull::core::circuit::CircuitConfig;
use multihull::core::probe::ProbeOutcome;
use multihull::core::snapshot::EndpointType;
use multihull::proxy::probe::{probe_all, probe_once};
use multihull::proxy::ProxyConfig;
use router_testkit::{MockUpstream, MockUpstreamConfig};
use std::time::Duration;

#[tokio::test]
async fn probe_classifies_status_connect_failure_and_runpod_warming() {
    let upstream = MockUpstream::start(MockUpstreamConfig::default())
        .await
        .unwrap();
    let mut target = endpoint("a", "pa", upstream.url(), 1);
    target.health_path = "/healthz".into();
    let proxy = start_proxy(vec![target.clone()]).await;
    let timeout = Duration::from_secs(1);

    let healthy = probe_once(&proxy.state.client, &target, timeout).await;
    assert_eq!(healthy.outcome, ProbeOutcome::Success);
    assert_eq!(healthy.status, Some(200));
    assert_eq!(upstream.request_count(), 1);

    upstream
        .reconfigure(MockUpstreamConfig::default().with_status(StatusCode::SERVICE_UNAVAILABLE));
    let failing = probe_once(&proxy.state.client, &target, timeout).await;
    assert_eq!(failing.outcome, ProbeOutcome::Failure);
    assert_eq!(failing.status, Some(503));

    upstream.reconfigure(MockUpstreamConfig::default().with_status(StatusCode::NO_CONTENT));
    let warm_other = probe_once(&proxy.state.client, &target, timeout).await;
    assert_eq!(warm_other.outcome, ProbeOutcome::Success);
    let mut runpod = target.clone();
    runpod.kind = EndpointType::Runpod;
    let warming = probe_once(&proxy.state.client, &runpod, timeout).await;
    assert_eq!(warming.outcome, ProbeOutcome::Warming);
    assert_eq!(warming.status, Some(204));

    let dropping = MockUpstream::start(MockUpstreamConfig::default().dropping_connections())
        .await
        .unwrap();
    let unreachable = endpoint("b", "pb", dropping.url(), 1);
    let dropped = probe_once(&proxy.state.client, &unreachable, timeout).await;
    assert_eq!(dropped.outcome, ProbeOutcome::Failure);
    assert_eq!(dropped.status, None);

    upstream.reconfigure(MockUpstreamConfig::default().with_ttft_delay(Duration::from_millis(300)));
    let slow = probe_once(&proxy.state.client, &target, Duration::from_millis(50)).await;
    assert_eq!(slow.outcome, ProbeOutcome::Failure);
    assert_eq!(slow.status, None);
}

#[tokio::test]
async fn health_path_without_a_leading_slash_probes_the_endpoint_host() {
    let upstream = MockUpstream::start(MockUpstreamConfig::default())
        .await
        .unwrap();
    let mut target = endpoint("a", "pa", upstream.url(), 1);
    target.health_path = "healthz".into();
    let proxy = start_proxy(vec![target.clone()]).await;

    let result = probe_once(&proxy.state.client, &target, Duration::from_secs(1)).await;
    assert_eq!(result.outcome, ProbeOutcome::Success);
    assert_eq!(result.status, Some(200));
    assert_eq!(upstream.request_count(), 1);
}

#[tokio::test]
async fn three_failed_rounds_open_the_circuit_and_three_good_rounds_allow_a_half_open_trial() {
    let upstream = MockUpstream::start(
        MockUpstreamConfig::default().with_status(StatusCode::SERVICE_UNAVAILABLE),
    )
    .await
    .unwrap();
    let target = endpoint("a", "pa", upstream.url(), 1);
    let proxy = start_proxy(vec![target.clone()]).await;

    for _ in 0..2 {
        probe_all(&proxy.state).await;
        assert_eq!(
            proxy.state.runtime.status(&target).unwrap().circuit,
            "closed"
        );
    }
    probe_all(&proxy.state).await;
    let status = proxy.state.runtime.status(&target).unwrap();
    assert_eq!(status.circuit, "open");
    assert!(status.probe.state == multihull::core::probe::ProbeState::Down);
    assert!(!proxy
        .state
        .runtime
        .endpoint_healthy(&target, proxy.state.runtime.now()));

    upstream.reconfigure(MockUpstreamConfig::default());
    for _ in 0..2 {
        probe_all(&proxy.state).await;
        assert_ne!(
            proxy.state.runtime.status(&target).unwrap().circuit,
            "closed"
        );
    }
    probe_all(&proxy.state).await;
    let status = proxy.state.runtime.status(&target).unwrap();
    assert_eq!(status.circuit, "half_open");
    assert_eq!(status.probe.probes, 6);
    assert!(proxy
        .state
        .runtime
        .endpoint_healthy(&target, proxy.state.runtime.now()));

    for _ in 0..3 {
        assert_eq!(get(&proxy, "/v1/x", &[]).await.status, 200);
    }
    assert_eq!(
        proxy.state.runtime.status(&target).unwrap().circuit,
        "closed"
    );
}

#[tokio::test]
async fn a_probe_down_endpoint_gets_no_traffic_after_its_backoff_until_the_probe_recovers() {
    let primary = MockUpstream::start(MockUpstreamConfig::default())
        .await
        .unwrap();
    let secondary = MockUpstream::start(MockUpstreamConfig::default())
        .await
        .unwrap();
    let a = endpoint("a", "pa", primary.url(), 1);
    let b = endpoint("b", "pb", secondary.url(), 2);
    let config = ProxyConfig {
        circuit: CircuitConfig {
            base_backoff: Duration::from_millis(50),
            jitter_fraction: 0.0,
            half_open_ramp: Duration::from_millis(30),
            ..CircuitConfig::default()
        },
        ..ProxyConfig::default()
    };
    let proxy = start_proxy_configured(single_route(vec![a.clone(), b.clone()]), config).await;
    for _ in 0..3 {
        proxy
            .state
            .runtime
            .record_probe(&a, ProbeOutcome::Failure, Some(503));
    }
    tokio::time::sleep(Duration::from_millis(200)).await;

    for _ in 0..20 {
        let reply = get(&proxy, "/v1/x", &[]).await;
        assert_eq!(reply.status, 200);
        assert_eq!(reply.endpoint(), "b");
    }
    assert_eq!(primary.request_count(), 0);
    assert_eq!(proxy.state.runtime.status(&a).unwrap().circuit, "open");

    for _ in 0..3 {
        proxy
            .state
            .runtime
            .record_probe(&a, ProbeOutcome::Success, Some(200));
    }
    assert_eq!(proxy.state.runtime.status(&a).unwrap().circuit, "half_open");
    tokio::time::sleep(Duration::from_millis(50)).await;
    for _ in 0..10 {
        assert_eq!(get(&proxy, "/v1/x", &[]).await.status, 200);
    }
    assert_eq!(proxy.state.runtime.status(&a).unwrap().circuit, "closed");
    assert!(primary.request_count() >= 3);
}
