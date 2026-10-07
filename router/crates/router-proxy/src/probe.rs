use bytes::Bytes;
use http::{Method, Request};
use http_body_util::Full;
use router_core::probe::{classify_probe, ProbeOutcome};
use router_core::snapshot::Endpoint;
use std::sync::Arc;
use std::time::Duration;

use crate::attempt::{upstream_uri, UpstreamClient};
use crate::runtime::ThreadRng;
use crate::state::ProxyState;

pub struct ProbeResult {
    pub outcome: ProbeOutcome,
    pub status: Option<u16>,
}

pub async fn probe_once(
    client: &UpstreamClient,
    endpoint: &Endpoint,
    timeout: Duration,
) -> ProbeResult {
    let status = fetch_status(client, endpoint, timeout).await;
    ProbeResult {
        outcome: classify_probe(endpoint.kind, status),
        status,
    }
}

async fn fetch_status(
    client: &UpstreamClient,
    endpoint: &Endpoint,
    timeout: Duration,
) -> Option<u16> {
    let uri = upstream_uri(endpoint, &endpoint.health_path()).ok()?;
    let mut builder = Request::builder().method(Method::GET).uri(&uri);
    if let Some(headers) = builder.headers_mut() {
        if let Some(authority) = uri.authority() {
            if let Ok(host) = authority.as_str().parse() {
                headers.insert(http::header::HOST, host);
            }
        }
        for (name, value) in &endpoint.inject_headers {
            if let (Ok(name), Ok(value)) = (
                http::header::HeaderName::from_bytes(name.as_bytes()),
                http::header::HeaderValue::from_str(value),
            ) {
                headers.insert(name, value);
            }
        }
    }
    let request = builder.body(Full::new(Bytes::new())).ok()?;
    match tokio::time::timeout(timeout, client.request(request)).await {
        Ok(Ok(response)) => Some(response.status().as_u16()),
        Ok(Err(_)) | Err(_) => None,
    }
}

pub async fn probe_all(state: &Arc<ProxyState>) {
    let snapshot = state.snapshot.load_full();
    let timeout = state.config.probe.timeout;
    let mut probes = tokio::task::JoinSet::new();
    for (_, endpoint) in snapshot.endpoints() {
        let state = state.clone();
        let endpoint = endpoint.clone();
        probes.spawn(async move {
            let result = probe_once(&state.client, &endpoint, timeout).await;
            state
                .runtime
                .record_probe(&endpoint, result.outcome, result.status);
        });
    }
    while probes.join_next().await.is_some() {}
}

pub async fn run(state: Arc<ProxyState>) {
    if !state.config.probe.enabled {
        tracing::info!("active health prober disabled");
        std::future::pending::<()>().await;
    }
    loop {
        let delay = state.config.probe.next_delay(&mut ThreadRng);
        tokio::time::sleep(delay).await;
        probe_all(&state).await;
    }
}
