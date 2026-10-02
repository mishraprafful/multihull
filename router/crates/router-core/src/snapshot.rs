use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

pub type EndpointId = String;

#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Timestamp {
    pub seconds: i64,
    pub nanos: i32,
}

#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct Snapshot {
    pub version: u64,
    #[serde(default)]
    pub at: Option<Timestamp>,
    #[serde(default)]
    pub routes: Vec<Route>,
}

impl Snapshot {
    pub fn endpoints(&self) -> impl Iterator<Item = (&Route, &Endpoint)> {
        self.routes.iter().flat_map(|route| {
            route
                .endpoints
                .iter()
                .map(move |endpoint| (route, endpoint))
        })
    }

    pub fn find_endpoint(&self, id: &str) -> Option<&Endpoint> {
        self.endpoints()
            .map(|(_, endpoint)| endpoint)
            .find(|endpoint| endpoint.id == id)
    }
}

#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct Route {
    pub id: String,
    #[serde(default)]
    pub hostname: String,
    #[serde(default = "default_path_prefix")]
    pub path_prefix: String,
    #[serde(default)]
    pub protocol: Protocol,
    #[serde(default)]
    pub failover: Failover,
    #[serde(default)]
    pub auth: Auth,
    #[serde(default)]
    pub sticky: Option<Sticky>,
    #[serde(default)]
    pub endpoints: Vec<Endpoint>,
}

fn default_path_prefix() -> String {
    "/".to_string()
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Protocol {
    #[default]
    Unspecified,
    Http,
    Sse,
    Websocket,
    Grpc,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Failover {
    #[serde(default)]
    pub policy: FailoverPolicy,
    #[serde(default)]
    pub retry_on: Vec<String>,
    #[serde(default = "default_max_retries")]
    pub max_retries: u32,
}

fn default_max_retries() -> u32 {
    2
}

impl Default for Failover {
    fn default() -> Self {
        Self {
            policy: FailoverPolicy::default(),
            retry_on: Vec::new(),
            max_retries: default_max_retries(),
        }
    }
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FailoverPolicy {
    #[default]
    Unspecified,
    Priority,
    Weighted,
    Latency,
}

#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Auth {
    #[serde(default)]
    pub api_key_hashes: Vec<String>,
}

#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Sticky {
    pub key: String,
    #[serde(default)]
    pub ttl_seconds: u64,
    #[serde(default)]
    pub mode: StickyMode,
    #[serde(default)]
    pub on_unhealthy: StickyOnUnhealthy,
    #[serde(default)]
    pub fallback_key: String,
}

impl Sticky {
    pub const DEFAULT_TTL_SECONDS: u64 = 30 * 60;

    pub fn ttl(&self) -> std::time::Duration {
        let seconds = if self.ttl_seconds == 0 {
            Self::DEFAULT_TTL_SECONDS
        } else {
            self.ttl_seconds
        };
        std::time::Duration::from_secs(seconds)
    }

    pub fn fails_on_unhealthy(&self) -> bool {
        self.on_unhealthy == StickyOnUnhealthy::Fail
    }

    pub fn is_provider_mode(&self) -> bool {
        self.mode == StickyMode::Provider
    }
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum StickyMode {
    #[default]
    Unspecified,
    Endpoint,
    Provider,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum StickyOnUnhealthy {
    #[default]
    Unspecified,
    Rehome,
    Fail,
}

#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Endpoint {
    pub id: EndpointId,
    #[serde(default)]
    pub provider: String,
    #[serde(rename = "type", default)]
    pub kind: EndpointType,
    pub url: String,
    #[serde(default)]
    pub region: String,
    #[serde(default)]
    pub priority: u32,
    #[serde(default = "default_weight")]
    pub weight: u32,
    #[serde(default)]
    pub health: Health,
    #[serde(default)]
    pub ready_replicas: u32,
    #[serde(default = "default_max_concurrency")]
    pub max_concurrency: u32,
    #[serde(default)]
    pub inject_headers: BTreeMap<String, String>,
    #[serde(default)]
    pub health_path: String,
}

pub const DEFAULT_HEALTH_PATH: &str = "/health";

fn default_weight() -> u32 {
    1
}

fn default_max_concurrency() -> u32 {
    32
}

impl Endpoint {
    pub fn health_path(&self) -> &str {
        if self.health_path.is_empty() {
            DEFAULT_HEALTH_PATH
        } else {
            &self.health_path
        }
    }

    pub fn accepts_traffic(&self) -> bool {
        matches!(
            self.health,
            Health::Ready | Health::Degraded | Health::Draining | Health::Unspecified
        )
    }

    pub fn accepts_new_sessions(&self) -> bool {
        matches!(
            self.health,
            Health::Ready | Health::Degraded | Health::Unspecified
        )
    }
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum EndpointType {
    #[default]
    Unspecified,
    Kubernetes,
    Modal,
    Runpod,
    Baseten,
    Replicate,
    Docker,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Health {
    #[default]
    Unspecified,
    Ready,
    Degraded,
    Draining,
    Down,
}

#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Degraded {
    pub service: String,
    pub provider: String,
    pub reason: DegradedReason,
    pub observed_concurrency: u32,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DegradedReason {
    #[default]
    Unspecified,
    QueueDepth,
    TtftP95,
    CapacityErrors,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn json_round_trip_keeps_every_field() {
        let snapshot = Snapshot {
            version: 7,
            at: Some(Timestamp {
                seconds: 1_700_000_000,
                nanos: 5,
            }),
            routes: vec![Route {
                id: "llama".into(),
                hostname: "api.example.com".into(),
                path_prefix: "/v1".into(),
                protocol: Protocol::Sse,
                failover: Failover {
                    policy: FailoverPolicy::Priority,
                    retry_on: vec!["capacity".into()],
                    max_retries: 2,
                },
                auth: Auth {
                    api_key_hashes: vec!["abc".into()],
                },
                sticky: Some(Sticky {
                    key: "header:X-Session-Id".into(),
                    ttl_seconds: 1800,
                    mode: StickyMode::Endpoint,
                    on_unhealthy: StickyOnUnhealthy::Rehome,
                    fallback_key: "body:$.messages[0]".into(),
                }),
                endpoints: vec![Endpoint {
                    id: "gke-prod".into(),
                    provider: "gke-prod".into(),
                    kind: EndpointType::Kubernetes,
                    url: "http://10.0.0.1:8000".into(),
                    region: "eu".into(),
                    priority: 1,
                    weight: 10,
                    health: Health::Ready,
                    ready_replicas: 2,
                    max_concurrency: 64,
                    inject_headers: BTreeMap::from([("X-Token".to_string(), "env".to_string())]),
                    health_path: "/healthz".into(),
                }],
            }],
        };
        let json = serde_json::to_string(&snapshot).unwrap();
        assert!(json.contains("\"type\":\"kubernetes\""));
        let parsed: Snapshot = serde_json::from_str(&json).unwrap();
        assert_eq!(parsed, snapshot);
    }

    #[test]
    fn minimal_json_applies_defaults() {
        let json =
            r#"{"version":1,"routes":[{"id":"r","endpoints":[{"id":"e","url":"http://x"}]}]}"#;
        let parsed: Snapshot = serde_json::from_str(json).unwrap();
        let endpoint = parsed.find_endpoint("e").unwrap();
        assert_eq!(parsed.routes[0].path_prefix, "/");
        assert_eq!(parsed.routes[0].failover.max_retries, 2);
        assert_eq!(endpoint.weight, 1);
        assert_eq!(endpoint.max_concurrency, 32);
        assert_eq!(endpoint.health_path(), "/health");
        assert!(endpoint.accepts_traffic());
    }

    #[test]
    fn docker_endpoint_type_uses_lowercase_json_name() {
        let json = r#"{"id":"e","url":"http://127.0.0.1:18001","type":"docker"}"#;
        let endpoint: Endpoint = serde_json::from_str(json).unwrap();
        assert_eq!(endpoint.kind, EndpointType::Docker);
        assert!(serde_json::to_string(&endpoint)
            .unwrap()
            .contains("\"type\":\"docker\""));
    }

    #[test]
    fn sticky_ttl_defaults_to_thirty_minutes() {
        let sticky = Sticky {
            key: "client-ip".into(),
            ..Default::default()
        };
        assert_eq!(sticky.ttl(), std::time::Duration::from_secs(1800));
        assert!(!sticky.fails_on_unhealthy());
        assert!(!sticky.is_provider_mode());
        let sticky = Sticky {
            ttl_seconds: 60,
            mode: StickyMode::Provider,
            on_unhealthy: StickyOnUnhealthy::Fail,
            ..sticky
        };
        assert_eq!(sticky.ttl(), std::time::Duration::from_secs(60));
        assert!(sticky.fails_on_unhealthy());
        assert!(sticky.is_provider_mode());
    }

    #[test]
    fn draining_keeps_sessions_but_refuses_new_ones() {
        let mut endpoint = Endpoint {
            health: Health::Draining,
            ..Default::default()
        };
        assert!(!endpoint.accepts_new_sessions());
        assert!(endpoint.accepts_traffic());
        endpoint.health = Health::Down;
        assert!(!endpoint.accepts_traffic());
        assert!(!endpoint.accepts_new_sessions());
    }
}
