use dashmap::DashMap;
use router_core::circuit::{Circuit, State};
use router_core::limit::{Gradient2, Gradient2Config};
use router_core::outcome::Outcome;
use router_core::retry::RetryBudget;
use router_core::rng::Rng;
use router_core::score::EndpointStateView;
use router_core::snapshot::{Endpoint, EndpointId, Snapshot, Sticky};
use router_core::sticky::{KeyHash, PinEntry, PinTable};
use std::collections::HashSet;
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

pub struct EndpointRuntime {
    pub id: EndpointId,
    circuit: Mutex<Circuit>,
    limiter: Mutex<Gradient2>,
    outstanding: AtomicU32,
    ewma_ttft: Mutex<Option<f64>>,
}

impl EndpointRuntime {
    fn new(endpoint: &Endpoint) -> Self {
        Self {
            id: endpoint.id.clone(),
            circuit: Mutex::new(Circuit::default()),
            limiter: Mutex::new(Gradient2::new(Gradient2Config::for_max_concurrency(
                endpoint.max_concurrency,
            ))),
            outstanding: AtomicU32::new(0),
            ewma_ttft: Mutex::new(None),
        }
    }

    pub fn outstanding(&self) -> u32 {
        self.outstanding.load(Ordering::Relaxed)
    }

    pub fn limit(&self) -> u32 {
        lock(&self.limiter).limit()
    }

    pub fn circuit_state(&self, now: Duration) -> State {
        lock(&self.circuit).peek(now)
    }

    pub fn ewma_ttft_secs(&self) -> Option<f64> {
        *lock(&self.ewma_ttft)
    }

    pub fn has_headroom(&self) -> bool {
        lock(&self.limiter).has_headroom(self.outstanding())
    }

    pub fn admit(&self, now: Duration, rng: &mut impl Rng) -> bool {
        lock(&self.circuit).admit(now, rng)
    }

    pub fn record_outcome(&self, outcome: Outcome, now: Duration, rng: &mut impl Rng) {
        lock(&self.circuit).record(outcome, now, rng);
        if outcome == Outcome::Capacity {
            lock(&self.limiter).on_capacity();
        }
    }

    pub fn record_ttft(&self, ttft: Duration) {
        let in_flight = self.outstanding();
        lock(&self.limiter).on_sample(ttft, in_flight);
        let mut ewma = lock(&self.ewma_ttft);
        let sample = ttft.as_secs_f64();
        *ewma = Some(match *ewma {
            None => sample,
            Some(previous) => previous + 0.2 * (sample - previous),
        });
    }
}

pub struct OutstandingGuard {
    endpoint: Arc<EndpointRuntime>,
}

impl OutstandingGuard {
    pub fn acquire(endpoint: Arc<EndpointRuntime>) -> Self {
        endpoint.outstanding.fetch_add(1, Ordering::Relaxed);
        Self { endpoint }
    }

    pub fn endpoint(&self) -> &Arc<EndpointRuntime> {
        &self.endpoint
    }
}

impl Drop for OutstandingGuard {
    fn drop(&mut self) {
        self.endpoint.outstanding.fetch_sub(1, Ordering::Relaxed);
    }
}

pub const MAX_PINS_PER_ROUTE: usize = 100_000;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SessionEntry {
    pub route: String,
    pub pin: PinEntry,
}

pub struct Runtime {
    endpoints: DashMap<EndpointId, Arc<EndpointRuntime>>,
    budgets: DashMap<String, Mutex<RetryBudget>>,
    pins: DashMap<String, Mutex<PinTable>>,
    started: Instant,
}

impl Default for Runtime {
    fn default() -> Self {
        Self::new()
    }
}

impl Runtime {
    pub fn new() -> Self {
        Self {
            endpoints: DashMap::new(),
            budgets: DashMap::new(),
            pins: DashMap::new(),
            started: Instant::now(),
        }
    }

    pub fn now(&self) -> Duration {
        self.started.elapsed()
    }

    pub fn endpoint(&self, endpoint: &Endpoint) -> Arc<EndpointRuntime> {
        self.endpoints
            .entry(endpoint.id.clone())
            .or_insert_with(|| Arc::new(EndpointRuntime::new(endpoint)))
            .clone()
    }

    pub fn get(&self, id: &str) -> Option<Arc<EndpointRuntime>> {
        self.endpoints.get(id).map(|e| e.clone())
    }

    pub fn endpoint_healthy(&self, endpoint: &Endpoint, now: Duration) -> bool {
        endpoint.accepts_traffic()
            && self
                .get(&endpoint.id)
                .map(|rt| !rt.circuit_state(now).is_open() && rt.has_headroom())
                .unwrap_or(true)
    }

    pub fn retain_snapshot(&self, snapshot: &Snapshot) {
        let live: HashSet<&str> = snapshot.endpoints().map(|(_, e)| e.id.as_str()).collect();
        self.endpoints.retain(|id, _| live.contains(id.as_str()));
        let routes: HashSet<&str> = snapshot.routes.iter().map(|r| r.id.as_str()).collect();
        self.budgets.retain(|id, _| routes.contains(id.as_str()));
        self.pins.retain(|id, _| routes.contains(id.as_str()));
    }

    pub fn pinned(&self, route_id: &str, sticky: &Sticky, key: &KeyHash) -> Option<EndpointId> {
        let now = self.now();
        lock(&self.pin_entry(route_id, sticky)).get(key, now)
    }

    pub fn pin(&self, route_id: &str, sticky: &Sticky, key: KeyHash, endpoint: EndpointId) {
        let now = self.now();
        lock(&self.pin_entry(route_id, sticky)).pin(key, endpoint, now);
        self.publish_session_gauge();
    }

    pub fn sessions(&self) -> Vec<SessionEntry> {
        let now = self.now();
        let mut sessions: Vec<SessionEntry> = self
            .pins
            .iter()
            .flat_map(|entry| {
                let route = entry.key().clone();
                lock(entry.value())
                    .entries(now)
                    .into_iter()
                    .map(move |pin| SessionEntry {
                        route: route.clone(),
                        pin,
                    })
                    .collect::<Vec<_>>()
            })
            .collect();
        sessions.sort_by(|a, b| {
            a.route
                .cmp(&b.route)
                .then_with(|| a.pin.age.cmp(&b.pin.age))
        });
        sessions
    }

    pub fn expire_sessions(&self) -> usize {
        let now = self.now();
        let expired = self
            .pins
            .iter()
            .map(|entry| lock(entry.value()).expire(now))
            .sum();
        self.publish_session_gauge();
        expired
    }

    pub fn active_sessions(&self) -> usize {
        self.pins
            .iter()
            .map(|entry| lock(entry.value()).len())
            .sum()
    }

    fn publish_session_gauge(&self) {
        metrics::gauge!(router_obs::metrics::STICKY_SESSIONS_ACTIVE)
            .set(self.active_sessions() as f64);
    }

    fn pin_entry(
        &self,
        route_id: &str,
        sticky: &Sticky,
    ) -> dashmap::mapref::one::Ref<'_, String, Mutex<PinTable>> {
        self.pins
            .entry(route_id.to_string())
            .or_insert_with(|| Mutex::new(PinTable::new(MAX_PINS_PER_ROUTE, sticky.ttl())))
            .downgrade()
    }

    pub fn record_request(&self, route_id: &str) {
        let now = self.now();
        lock(&self.budget_entry(route_id)).record_request(now);
    }

    pub fn with_budget<T>(
        &self,
        route_id: &str,
        f: impl FnOnce(&mut RetryBudget, Duration) -> T,
    ) -> T {
        let now = self.now();
        let entry = self.budget_entry(route_id);
        let mut budget = lock(&entry);
        f(&mut budget, now)
    }

    fn budget_entry(
        &self,
        route_id: &str,
    ) -> dashmap::mapref::one::Ref<'_, String, Mutex<RetryBudget>> {
        self.budgets
            .entry(route_id.to_string())
            .or_insert_with(|| Mutex::new(RetryBudget::default()))
            .downgrade()
    }

    pub fn view<'a>(
        &'a self,
        excluded: &'a HashSet<EndpointId>,
        region: Option<&'a str>,
    ) -> RuntimeView<'a> {
        RuntimeView {
            runtime: self,
            now: self.now(),
            excluded,
            region,
        }
    }
}

pub struct RuntimeView<'a> {
    runtime: &'a Runtime,
    now: Duration,
    excluded: &'a HashSet<EndpointId>,
    region: Option<&'a str>,
}

impl EndpointStateView for RuntimeView<'_> {
    fn available(&self, id: &str) -> bool {
        if self.excluded.contains(id) {
            return false;
        }
        match self.runtime.get(id) {
            Some(endpoint) => {
                !endpoint.circuit_state(self.now).is_open() && endpoint.has_headroom()
            }
            None => true,
        }
    }

    fn outstanding(&self, id: &str) -> u32 {
        self.runtime.get(id).map(|e| e.outstanding()).unwrap_or(0)
    }

    fn ewma_ttft_secs(&self, id: &str) -> Option<f64> {
        self.runtime.get(id).and_then(|e| e.ewma_ttft_secs())
    }

    fn local_region(&self) -> Option<&str> {
        self.region
    }
}

pub struct ThreadRng;

impl Rng for ThreadRng {
    fn next_f64(&mut self) -> f64 {
        rand::random::<f64>()
    }
}

fn lock<T>(mutex: &Mutex<T>) -> std::sync::MutexGuard<'_, T> {
    mutex
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

#[cfg(test)]
mod tests {
    use super::*;
    use router_core::snapshot::Route;

    fn endpoint(id: &str) -> Endpoint {
        Endpoint {
            id: id.into(),
            url: "http://127.0.0.1:1".into(),
            max_concurrency: 4,
            ..Default::default()
        }
    }

    #[test]
    fn outstanding_guard_counts_in_flight() {
        let runtime = Runtime::new();
        let rt = runtime.endpoint(&endpoint("a"));
        assert_eq!(rt.outstanding(), 0);
        let guard = OutstandingGuard::acquire(rt.clone());
        assert_eq!(rt.outstanding(), 1);
        drop(guard);
        assert_eq!(rt.outstanding(), 0);
    }

    #[test]
    fn retain_drops_endpoints_missing_from_snapshot() {
        let runtime = Runtime::new();
        runtime.endpoint(&endpoint("a"));
        runtime.endpoint(&endpoint("b"));
        runtime.record_request("r1");
        runtime.record_request("r2");
        let snapshot = Snapshot {
            version: 1,
            at: None,
            routes: vec![Route {
                id: "r1".into(),
                endpoints: vec![endpoint("a")],
                ..Default::default()
            }],
        };
        runtime.retain_snapshot(&snapshot);
        assert!(runtime.get("a").is_some());
        assert!(runtime.get("b").is_none());
        assert_eq!(runtime.budgets.len(), 1);
    }

    #[test]
    fn view_excludes_open_circuits_and_excluded_ids() {
        let runtime = Runtime::new();
        let a = runtime.endpoint(&endpoint("a"));
        runtime.endpoint(&endpoint("b"));
        let mut rng = ThreadRng;
        for _ in 0..5 {
            a.record_outcome(Outcome::Transient, runtime.now(), &mut rng);
        }
        let excluded = HashSet::from(["b".to_string()]);
        let view = runtime.view(&excluded, None);
        assert!(!view.available("a"));
        assert!(!view.available("b"));
        assert!(view.available("unknown"));
    }

    #[test]
    fn pins_are_per_route_and_listed_with_age() {
        let runtime = Runtime::new();
        let sticky = Sticky {
            key: "client-ip".into(),
            ttl_seconds: 60,
            ..Default::default()
        };
        let key = router_core::sticky::hash_key(b"session");
        assert_eq!(runtime.pinned("r1", &sticky, &key), None);
        runtime.pin("r1", &sticky, key, "a".into());
        runtime.pin("r2", &sticky, key, "b".into());
        assert_eq!(runtime.pinned("r1", &sticky, &key).as_deref(), Some("a"));
        assert_eq!(runtime.pinned("r2", &sticky, &key).as_deref(), Some("b"));
        let sessions = runtime.sessions();
        assert_eq!(sessions.len(), 2);
        assert_eq!(sessions[0].route, "r1");
        assert_eq!(sessions[0].pin.endpoint, "a");
        assert_eq!(runtime.active_sessions(), 2);
        assert_eq!(runtime.expire_sessions(), 0);
        let snapshot = Snapshot {
            version: 1,
            at: None,
            routes: vec![Route {
                id: "r2".into(),
                ..Default::default()
            }],
        };
        runtime.retain_snapshot(&snapshot);
        assert_eq!(runtime.active_sessions(), 1);
    }

    #[test]
    fn capacity_outcome_lowers_limit() {
        let runtime = Runtime::new();
        let a = runtime.endpoint(&endpoint("a"));
        let before = a.limit();
        a.record_ttft(Duration::from_millis(10));
        a.record_outcome(Outcome::Capacity, runtime.now(), &mut ThreadRng);
        assert!(a.limit() <= before);
        assert!(a.ewma_ttft_secs().is_some());
    }
}
