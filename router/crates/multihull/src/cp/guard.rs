use crate::core::Snapshot;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct KeptRoute {
    pub route: String,
    pub endpoints: usize,
}

pub fn keep_endpoints(held: &Snapshot, next: &mut Snapshot) -> Vec<KeptRoute> {
    let mut kept = Vec::new();
    for route in next.routes.iter_mut().filter(|r| r.endpoints.is_empty()) {
        let Some(previous) = held.routes.iter().find(|r| r.id == route.id) else {
            continue;
        };
        if previous.endpoints.is_empty() {
            continue;
        }
        route.endpoints = previous.endpoints.clone();
        kept.push(KeptRoute {
            route: route.id.clone(),
            endpoints: previous.endpoints.len(),
        });
    }
    kept
}

pub fn refusal(kept: &[KeptRoute]) -> String {
    let routes = kept
        .iter()
        .map(|k| format!("route {} ({} kept)", k.route, k.endpoints))
        .collect::<Vec<_>>()
        .join(", ");
    format!(
        "refused an empty endpoint set for {routes}; remove the route from the snapshot to stop serving it"
    )
}

pub fn apply(held: &Snapshot, mut next: Snapshot) -> (Snapshot, Option<String>) {
    let kept = keep_endpoints(held, &mut next);
    if kept.is_empty() {
        return (next, None);
    }
    let reason = refusal(&kept);
    tracing::warn!(version = next.version, %reason, "snapshot would empty a serving route");
    (next, Some(reason))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::core::snapshot::{Endpoint, Route};

    fn snapshot(version: u64, routes: &[(&str, &[&str])]) -> Snapshot {
        Snapshot {
            version,
            routes: routes
                .iter()
                .map(|(id, endpoints)| Route {
                    id: (*id).to_string(),
                    endpoints: endpoints
                        .iter()
                        .map(|endpoint| Endpoint {
                            id: (*endpoint).to_string(),
                            ..Default::default()
                        })
                        .collect(),
                    ..Default::default()
                })
                .collect(),
            ..Default::default()
        }
    }

    fn ids(snapshot: &Snapshot) -> Vec<(&str, Vec<&str>)> {
        snapshot
            .routes
            .iter()
            .map(|r| {
                let endpoints = r.endpoints.iter().map(|e| e.id.as_str()).collect();
                (r.id.as_str(), endpoints)
            })
            .collect()
    }

    #[test]
    fn keeps_held_endpoints_only_for_routes_sent_empty() {
        let held = snapshot(4, &[("a", &["a1", "a2"]), ("b", &["b1"]), ("c", &[])]);
        let (next, reason) = apply(
            &held,
            snapshot(5, &[("a", &[]), ("b", &["b2"]), ("c", &[])]),
        );
        assert_eq!(next.version, 5);
        assert_eq!(
            ids(&next),
            vec![("a", vec!["a1", "a2"]), ("b", vec!["b2"]), ("c", vec![])]
        );
        assert_eq!(
            reason.as_deref(),
            Some("refused an empty endpoint set for route a (2 kept); remove the route from the snapshot to stop serving it")
        );
    }

    #[test]
    fn a_removed_route_is_dropped_without_a_refusal() {
        let held = snapshot(4, &[("a", &["a1"]), ("b", &["b1"])]);
        let (next, reason) = apply(&held, snapshot(5, &[("b", &["b1"])]));
        assert_eq!(ids(&next), vec![("b", vec!["b1"])]);
        assert!(reason.is_none());
        let (next, reason) = apply(&held, snapshot(6, &[]));
        assert!(next.routes.is_empty());
        assert!(reason.is_none());
    }

    #[test]
    fn a_route_unknown_to_the_router_may_arrive_empty() {
        let (next, reason) = apply(&Snapshot::default(), snapshot(1, &[("a", &[])]));
        assert_eq!(ids(&next), vec![("a", vec![])]);
        assert!(reason.is_none());
    }

    #[test]
    fn every_kept_route_is_named() {
        let held = snapshot(1, &[("a", &["a1"]), ("b", &["b1", "b2", "b3"])]);
        let mut next = snapshot(2, &[("a", &[]), ("b", &[])]);
        let kept = keep_endpoints(&held, &mut next);
        assert_eq!(
            refusal(&kept),
            "refused an empty endpoint set for route a (1 kept), route b (3 kept); remove the route from the snapshot to stop serving it"
        );
    }
}
