mod common;

use common::{endpoint, start_proxy};
use http::StatusCode;
use router_core::probe::ProbeOutcome;
use router_core::snapshot::EndpointType;
use router_proxy::probe::{probe_all, probe_once};
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
async fn three_failed_rounds_open_the_circuit_and_three_good_rounds_close_it() {
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
    assert!(status.probe.state == router_core::probe::ProbeState::Down);
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
    assert_eq!(status.circuit, "closed");
    assert_eq!(status.probe.probes, 6);
    assert!(proxy
        .state
        .runtime
        .endpoint_healthy(&target, proxy.state.runtime.now()));
}
