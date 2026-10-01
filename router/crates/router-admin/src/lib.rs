use arc_swap::ArcSwap;
use axum::extract::State;
use axum::http::{header, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use axum::{Json, Router};
use router_core::snapshot::Health;
use router_core::Snapshot;
use serde::Serialize;
use std::net::SocketAddr;
use std::sync::Arc;
use tokio::net::TcpListener;

#[derive(Clone)]
pub struct AdminState {
    pub snapshot: Arc<ArcSwap<Snapshot>>,
}

impl AdminState {
    pub fn new(snapshot: Arc<ArcSwap<Snapshot>>) -> Self {
        Self { snapshot }
    }
}

pub fn router(state: AdminState) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/metrics", get(metrics))
        .route("/debug/endpoints", get(debug_endpoints))
        .with_state(state)
}

pub async fn serve(listen: SocketAddr, state: AdminState) -> std::io::Result<()> {
    let listener = TcpListener::bind(listen).await?;
    axum::serve(listener, router(state)).await
}

async fn healthz() -> &'static str {
    "ok\n"
}

async fn metrics() -> Response {
    (
        StatusCode::OK,
        [(header::CONTENT_TYPE, "text/plain; version=0.0.4")],
        "# metrics exporter not wired yet\n",
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
}

#[derive(Serialize)]
pub struct EndpointsView {
    pub snapshot_version: u64,
    pub endpoints: Vec<EndpointView>,
}

pub fn endpoints_view(snapshot: &Snapshot) -> EndpointsView {
    EndpointsView {
        snapshot_version: snapshot.version,
        endpoints: snapshot
            .endpoints()
            .map(|(route, endpoint)| EndpointView {
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
            })
            .collect(),
    }
}

async fn debug_endpoints(State(state): State<AdminState>) -> Json<EndpointsView> {
    let snapshot = state.snapshot.load();
    Json(endpoints_view(&snapshot))
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
    async fn metrics_placeholder_is_text() {
        let (status, body) = get_body("/metrics").await;
        assert_eq!(status, StatusCode::OK);
        assert!(body.starts_with('#'));
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
