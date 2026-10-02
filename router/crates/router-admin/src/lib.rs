use arc_swap::ArcSwap;
use axum::extract::State;
use axum::http::{header, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use axum::{Json, Router};
use router_core::snapshot::Health;
use router_core::sticky::truncate_hash;
use router_core::Snapshot;
use router_obs::PrometheusHandle;
use router_proxy::ProxyState;
use serde::Serialize;
use std::net::SocketAddr;
use std::sync::Arc;
use tokio::net::TcpListener;

#[derive(Clone)]
pub struct AdminState {
    pub snapshot: Arc<ArcSwap<Snapshot>>,
    pub proxy: Option<Arc<ProxyState>>,
    pub metrics: Option<PrometheusHandle>,
}

impl AdminState {
    pub fn new(snapshot: Arc<ArcSwap<Snapshot>>) -> Self {
        Self {
            snapshot,
            proxy: None,
            metrics: None,
        }
    }

    pub fn with_proxy(mut self, proxy: Arc<ProxyState>) -> Self {
        self.proxy = Some(proxy);
        self
    }

    pub fn with_metrics(mut self, metrics: PrometheusHandle) -> Self {
        self.metrics = Some(metrics);
        self
    }
}

pub fn router(state: AdminState) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/metrics", get(metrics))
        .route("/debug/endpoints", get(debug_endpoints))
        .route("/debug/sessions", get(debug_sessions))
        .with_state(state)
}

pub async fn serve(listen: SocketAddr, state: AdminState) -> std::io::Result<()> {
    let listener = TcpListener::bind(listen).await?;
    axum::serve(listener, router(state)).await
}

async fn healthz() -> &'static str {
    "ok\n"
}

pub const METRICS_CONTENT_TYPE: &str = "text/plain; version=0.0.4; charset=utf-8";

pub fn render_metrics(handle: Option<&PrometheusHandle>) -> String {
    match handle {
        Some(handle) => {
            handle.run_upkeep();
            handle.render()
        }
        None => "# metrics recorder not installed\n".to_string(),
    }
}

async fn metrics(State(state): State<AdminState>) -> Response {
    (
        StatusCode::OK,
        [(header::CONTENT_TYPE, METRICS_CONTENT_TYPE)],
        render_metrics(state.metrics.as_ref()),
    )
        .into_response()
}

#[derive(Serialize)]
pub struct EndpointView {
    pub route: String,
    pub id: String,
    pub provider: String,
    pub url: String,
    pub region: String,
    pub priority: u32,
    pub weight: u32,
    pub health: Health,
    pub ready_replicas: u32,
    pub max_concurrency: u32,
    pub circuit: Option<&'static str>,
    pub provider_circuit_open: Option<bool>,
    pub outstanding: Option<u32>,
    pub concurrency_limit: Option<u32>,
    pub ewma_ttft_seconds: Option<f64>,
}

#[derive(Serialize)]
pub struct EndpointsView {
    pub snapshot_version: u64,
    pub endpoints: Vec<EndpointView>,
}

pub fn endpoints_view(snapshot: &Snapshot, proxy: Option<&ProxyState>) -> EndpointsView {
    EndpointsView {
        snapshot_version: snapshot.version,
        endpoints: snapshot
            .endpoints()
            .map(|(route, endpoint)| {
                let status = proxy.and_then(|proxy| proxy.runtime.status(endpoint));
                EndpointView {
                    route: route.id.clone(),
                    id: endpoint.id.clone(),
                    provider: endpoint.provider.clone(),
                    url: endpoint.url.clone(),
                    region: endpoint.region.clone(),
                    priority: endpoint.priority,
                    weight: endpoint.weight,
                    health: endpoint.health,
                    ready_replicas: endpoint.ready_replicas,
                    max_concurrency: endpoint.max_concurrency,
                    circuit: status.as_ref().map(|s| s.circuit),
                    provider_circuit_open: status.as_ref().map(|s| s.provider_circuit_open),
                    outstanding: status.as_ref().map(|s| s.outstanding),
                    concurrency_limit: status.as_ref().map(|s| s.concurrency_limit),
                    ewma_ttft_seconds: status.as_ref().and_then(|s| s.ewma_ttft_secs),
                }
            })
            .collect(),
    }
}

async fn debug_endpoints(State(state): State<AdminState>) -> Json<EndpointsView> {
    let snapshot = state.snapshot.load();
    Json(endpoints_view(&snapshot, state.proxy.as_deref()))
}

#[derive(Serialize)]
pub struct SessionView {
    pub route: String,
    pub key_hash: String,
    pub owner: String,
    pub age_seconds: f64,
}

#[derive(Serialize)]
pub struct SessionsView {
    pub active: usize,
    pub sessions: Vec<SessionView>,
}

pub fn sessions_view(proxy: Option<&ProxyState>) -> SessionsView {
    let Some(proxy) = proxy else {
        return SessionsView {
            active: 0,
            sessions: Vec::new(),
        };
    };
    proxy.runtime.expire_sessions();
    let sessions: Vec<SessionView> = proxy
        .runtime
        .sessions()
        .into_iter()
        .map(|entry| SessionView {
            route: entry.route,
            key_hash: truncate_hash(&entry.pin.key_hash),
            owner: entry.pin.endpoint,
            age_seconds: entry.pin.age.as_secs_f64(),
        })
        .collect();
    SessionsView {
        active: sessions.len(),
        sessions,
    }
}

async fn debug_sessions(State(state): State<AdminState>) -> Json<SessionsView> {
    Json(sessions_view(state.proxy.as_deref()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::Body;
    use axum::http::Request;
    use http_body_util::BodyExt;
    use router_core::snapshot::{Endpoint, Route};
    use tower::ServiceExt;

    fn state() -> AdminState {
        let snapshot = Snapshot {
            version: 12,
            at: None,
            routes: vec![Route {
                id: "llama".into(),
                endpoints: vec![
                    Endpoint {
                        id: "gke".into(),
                        provider: "gke-prod".into(),
                        url: "http://10.0.0.1".into(),
                        priority: 1,
                        health: Health::Ready,
                        ..Default::default()
                    },
                    Endpoint {
                        id: "modal".into(),
                        provider: "modal-main".into(),
                        url: "https://m.modal.run".into(),
                        priority: 2,
                        health: Health::Degraded,
                        ..Default::default()
                    },
                ],
                ..Default::default()
            }],
        };
        AdminState::new(Arc::new(ArcSwap::from_pointee(snapshot)))
    }

    async fn get_body(path: &str) -> (StatusCode, String) {
        let response = router(state())
            .oneshot(Request::builder().uri(path).body(Body::empty()).unwrap())
            .await
            .unwrap();
        let status = response.status();
        let bytes = response.into_body().collect().await.unwrap().to_bytes();
        (status, String::from_utf8(bytes.to_vec()).unwrap())
    }

    #[tokio::test]
    async fn healthz_returns_ok() {
        let (status, body) = get_body("/healthz").await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(body, "ok\n");
    }

    #[tokio::test]
    async fn metrics_without_a_recorder_is_a_comment() {
        let (status, body) = get_body("/metrics").await;
        assert_eq!(status, StatusCode::OK);
        assert!(body.starts_with('#'));
    }

    #[tokio::test]
    async fn metrics_renders_prometheus_text_from_the_installed_recorder() {
        let handle = router_obs::install_prometheus().expect("first recorder in this process");
        metrics::counter!(
            router_obs::metrics::FAILOVERS_TOTAL,
            router_obs::metrics::labels::FROM => "gke-prod",
            router_obs::metrics::labels::REASON => "transient"
        )
        .increment(2);
        let response = router(state().with_metrics(handle))
            .oneshot(
                Request::builder()
                    .uri("/metrics")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        assert_eq!(
            response.headers().get(header::CONTENT_TYPE).unwrap(),
            METRICS_CONTENT_TYPE
        );
        let bytes = response.into_body().collect().await.unwrap().to_bytes();
        let body = String::from_utf8(bytes.to_vec()).unwrap();
        assert!(body.contains("# TYPE router_failovers_total counter"));
        assert!(body.contains("router_failovers_total{from=\"gke-prod\",reason=\"transient\"} 2"));
    }

    #[tokio::test]
    async fn debug_endpoints_renders_snapshot() {
        let (status, body) = get_body("/debug/endpoints").await;
        assert_eq!(status, StatusCode::OK);
        let parsed: serde_json::Value = serde_json::from_str(&body).unwrap();
        assert_eq!(parsed["snapshot_version"], 12);
        assert_eq!(parsed["endpoints"].as_array().unwrap().len(), 2);
        assert_eq!(parsed["endpoints"][1]["health"], "degraded");
        assert_eq!(parsed["endpoints"][0]["provider"], "gke-prod");
    }

    #[tokio::test]
    async fn debug_endpoints_exposes_circuit_state_from_the_proxy_runtime() {
        let (_, body) = get_body("/debug/endpoints").await;
        let parsed: serde_json::Value = serde_json::from_str(&body).unwrap();
        assert!(parsed["endpoints"][0]["circuit"].is_null());

        let base = state();
        let snapshot = base.snapshot.clone();
        let proxy = ProxyState::new(router_proxy::ProxyConfig::default(), snapshot.clone());
        let loaded = snapshot.load();
        let gke = loaded.find_endpoint("gke").unwrap();
        let modal = loaded.find_endpoint("modal").unwrap();
        let mut rng = router_core::rng::ZeroRng;
        proxy.runtime.endpoint(modal);
        for _ in 0..5 {
            proxy.runtime.record_attempt(
                gke,
                router_core::Outcome::Transient,
                Some(502),
                proxy.runtime.now(),
                &mut rng,
            );
        }
        let response = router(base.with_proxy(proxy))
            .oneshot(
                Request::builder()
                    .uri("/debug/endpoints")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        let bytes = response.into_body().collect().await.unwrap().to_bytes();
        let parsed: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(parsed["endpoints"][0]["circuit"], "open");
        assert_eq!(parsed["endpoints"][0]["provider_circuit_open"], true);
        assert_eq!(parsed["endpoints"][0]["outstanding"], 0);
        assert_eq!(parsed["endpoints"][1]["circuit"], "closed");
        assert_eq!(parsed["endpoints"][1]["provider_circuit_open"], false);
        assert!(
            parsed["endpoints"][1]["concurrency_limit"]
                .as_u64()
                .unwrap()
                >= 1
        );
    }

    #[tokio::test]
    async fn debug_sessions_lists_pins_with_truncated_hashes() {
        let (_, body) = get_body("/debug/sessions").await;
        let parsed: serde_json::Value = serde_json::from_str(&body).unwrap();
        assert_eq!(parsed["active"], 0);

        let snapshot = Arc::new(ArcSwap::from_pointee(Snapshot::default()));
        let proxy = ProxyState::new(router_proxy::ProxyConfig::default(), snapshot.clone());
        let sticky = router_core::snapshot::Sticky {
            key: "header:X-Session-Id".into(),
            ttl_seconds: 60,
            ..Default::default()
        };
        let key = b"session-1";
        proxy.runtime.pin(
            "llama",
            &sticky,
            router_core::sticky::hash_key(key),
            "gke".into(),
        );
        let state = AdminState::new(snapshot).with_proxy(proxy);
        let response = router(state)
            .oneshot(
                Request::builder()
                    .uri("/debug/sessions")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        let bytes = response.into_body().collect().await.unwrap().to_bytes();
        let parsed: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(parsed["active"], 1);
        assert_eq!(parsed["sessions"][0]["route"], "llama");
        assert_eq!(parsed["sessions"][0]["owner"], "gke");
        assert_eq!(
            parsed["sessions"][0]["key_hash"],
            router_core::sticky::truncated_key_hash(key)
        );
        assert!(parsed["sessions"][0]["age_seconds"].as_f64().unwrap() < 5.0);
        assert!(!body.contains("session-1"));
    }

    #[tokio::test]
    async fn debug_endpoints_follows_snapshot_swaps() {
        let state = state();
        state.snapshot.store(Arc::new(Snapshot {
            version: 13,
            ..Default::default()
        }));
        let response = router(state)
            .oneshot(
                Request::builder()
                    .uri("/debug/endpoints")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        let bytes = response.into_body().collect().await.unwrap().to_bytes();
        let parsed: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(parsed["snapshot_version"], 13);
        assert!(parsed["endpoints"].as_array().unwrap().is_empty());
    }
}
