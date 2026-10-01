use router_core::snapshot::{Route, Snapshot};
use std::collections::HashMap;
use std::sync::Arc;

#[derive(Default)]
pub struct RouteTable {
    by_host: HashMap<String, matchit::Router<usize>>,
    any_host: matchit::Router<usize>,
    routes: Vec<Arc<Route>>,
}

impl RouteTable {
    pub fn build(snapshot: &Snapshot) -> Self {
        let mut table = Self::default();
        for route in &snapshot.routes {
            let index = table.routes.len();
            table.routes.push(Arc::new(route.clone()));
            let router = if route.hostname.is_empty() {
                &mut table.any_host
            } else {
                table
                    .by_host
                    .entry(route.hostname.to_ascii_lowercase())
                    .or_default()
            };
            for pattern in prefix_patterns(&route.path_prefix) {
                if let Err(error) = router.insert(pattern.clone(), index) {
                    tracing::warn!(route = %route.id, %pattern, %error, "skipping route pattern");
                }
            }
        }
        table
    }

    pub fn matches(&self, host: Option<&str>, path: &str) -> Option<Arc<Route>> {
        let host = host.map(strip_port).map(str::to_ascii_lowercase);
        let from_host = host
            .as_deref()
            .and_then(|h| self.by_host.get(h))
            .and_then(|router| router.at(path).ok().map(|m| *m.value));
        let index = from_host.or_else(|| self.any_host.at(path).ok().map(|m| *m.value))?;
        self.routes.get(index).cloned()
    }

    pub fn len(&self) -> usize {
        self.routes.len()
    }

    pub fn is_empty(&self) -> bool {
        self.routes.is_empty()
    }
}

fn strip_port(host: &str) -> &str {
    if host.starts_with('[') {
        host.split(']').next().map(|h| &h[1..]).unwrap_or(host)
    } else {
        host.split(':').next().unwrap_or(host)
    }
}

fn prefix_patterns(prefix: &str) -> Vec<String> {
    let trimmed = prefix.trim_end_matches('/');
    if trimmed.is_empty() {
        return vec!["/".to_string(), "/{*rest}".to_string()];
    }
    vec![trimmed.to_string(), format!("{trimmed}/{{*rest}}")]
}

#[cfg(test)]
mod tests {
    use super::*;

    fn snapshot() -> Snapshot {
        Snapshot {
            version: 1,
            at: None,
            routes: vec![
                Route {
                    id: "llama".into(),
                    hostname: "api.example.com".into(),
                    path_prefix: "/v1".into(),
                    ..Default::default()
                },
                Route {
                    id: "whisper".into(),
                    hostname: "api.example.com".into(),
                    path_prefix: "/audio/".into(),
                    ..Default::default()
                },
                Route {
                    id: "catchall".into(),
                    hostname: String::new(),
                    path_prefix: "/".into(),
                    ..Default::default()
                },
            ],
        }
    }

    #[test]
    fn matches_host_and_prefix() {
        let table = RouteTable::build(&snapshot());
        assert_eq!(table.len(), 3);
        let m = table
            .matches(Some("api.example.com:443"), "/v1/chat/completions")
            .unwrap();
        assert_eq!(m.id, "llama");
        let m = table.matches(Some("API.example.com"), "/v1").unwrap();
        assert_eq!(m.id, "llama");
        let m = table
            .matches(Some("api.example.com"), "/audio/transcribe")
            .unwrap();
        assert_eq!(m.id, "whisper");
    }

    #[test]
    fn falls_back_to_hostless_route() {
        let table = RouteTable::build(&snapshot());
        assert_eq!(
            table.matches(Some("other.host"), "/anything").unwrap().id,
            "catchall"
        );
        assert_eq!(table.matches(None, "/").unwrap().id, "catchall");
        assert_eq!(
            table.matches(Some("api.example.com"), "/v2/x").unwrap().id,
            "catchall"
        );
    }

    #[test]
    fn no_match_without_catchall() {
        let mut snap = snapshot();
        snap.routes.pop();
        let table = RouteTable::build(&snap);
        assert!(table.matches(Some("api.example.com"), "/v2").is_none());
        assert!(table.matches(None, "/v1").is_none());
    }

    #[test]
    fn prefix_does_not_match_longer_segment() {
        let mut snap = snapshot();
        snap.routes.pop();
        let table = RouteTable::build(&snap);
        assert!(table
            .matches(Some("api.example.com"), "/v10/chat")
            .is_none());
    }
}
