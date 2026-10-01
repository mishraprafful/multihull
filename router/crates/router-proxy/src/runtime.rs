use dashmap::DashMap;
use router_core::circuit::{Circuit, CircuitConfig, ProviderCircuit, State};
use router_core::limit::{AdmissionQueue, Gradient2, Gradient2Config};
use router_core::outcome::Outcome;
use router_core::pressure::{PressureConfig, PressureDetector};
use router_core::retry::RetryBudget;
use router_core::rng::Rng;
use router_core::score::EndpointStateView;
use router_core::snapshot::{Degraded, Endpoint, EndpointId, Route, Snapshot, Sticky};
use router_core::sticky::{KeyHash, PinEntry, PinTable};
use std::collections::HashSet;
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tokio::sync::Notify;

use crate::admission::Admission;

pub struct EndpointRuntime {
    pub id: EndpointId,
    pub provider: String,
    circuit: Mutex<Circuit>,
    limiter: Mutex<Gradient2>,
    outstanding: AtomicU32,
    ewma_ttft: Mutex<Option<f64>>,
    released: Arc<Notify>,
}

impl EndpointRuntime {
    fn new(endpoint: &Endpoint, circuit: CircuitConfig, released: Arc<Notify>) -> Self {
        Self {
            id: endpoint.id.clone(),
            provider: endpoint.provider.clone(),
            circuit: Mutex::new(Circuit::new(circuit)),
            limiter: Mutex::new(Gradient2::new(Gradient2Config::for_max_concurrency(
                endpoint.max_concurrency,
            ))),
            outstanding: AtomicU32::new(0),
            ewma_ttft: Mutex::new(None),
            released,
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
        self.endpoint.released.notify_waiters();
    }
}

pub const MAX_PINS_PER_ROUTE: usize = 100_000;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SessionEntry {
    pub route: String,
    pub pin: PinEntry,
}

#[derive(Clone, Debug, PartialEq)]
pub struct EndpointStatus {
    pub circuit: &'static str,
    pub provider_circuit_open: bool,
    pub outstanding: u32,
    pub concurrency_limit: u32,
    pub ewma_ttft_secs: Option<f64>,
}

pub struct Runtime {
    endpoints: DashMap<EndpointId, Arc<EndpointRuntime>>,
    providers: DashMap<String, Mutex<ProviderCircuit>>,
    budgets: DashMap<String, Mutex<RetryBudget>>,
    pins: DashMap<String, Mutex<PinTable>>,
    pub admission: Admission,
    pressure: Mutex<PressureDetector>,
    released: Arc<Notify>,
    circuit_config: CircuitConfig,
    started: Instant,
}

impl Default for Runtime {
    fn default() -> Self {
        Self::new(
            CircuitConfig::default(),
            AdmissionQueue::default(),
            PressureConfig::default(),
        )
    }
}

impl Runtime {
    pub fn new(
        circuit_config: CircuitConfig,
        admission: AdmissionQueue,
        pressure: PressureConfig,
    ) -> Self {
        let released = Arc::new(Notify::new());
        Self {
            endpoints: DashMap::new(),
            providers: DashMap::new(),
            budgets: DashMap::new(),
            pins: DashMap::new(),
            pressure: Mutex::new(PressureDetector::new(pressure, admission.max_wait)),
            admission: Admission::new(admission, released.clone()),
            released,
            circuit_config,
            started: Instant::now(),
        }
    }

    pub fn now(&self) -> Duration {
        self.started.elapsed()
    }

    pub fn endpoint(&self, endpoint: &Endpoint) -> Arc<EndpointRuntime> {
        self.endpoints
            .entry(endpoint.id.clone())
            .or_insert_with(|| {
                Arc::new(EndpointRuntime::new(
                    endpoint,
                    self.circuit_config.clone(),
                    self.released.clone(),
                ))
            })
            .clone()
    }

    pub fn route_saturated(&self, route: &Route) -> bool {
        let now = self.now();
        let mut candidates = 0;
        for endpoint in route
            .endpoints
            .iter()
            .filter(|e| e.accepts_traffic() && !self.endpoint_open(e, now))
        {
            candidates += 1;
            match self.get(&endpoint.id) {
                None => return false,
                Some(runtime) if runtime.has_headroom() => return false,
                Some(_) => {}
            }
        }
        candidates > 0
    }

    pub fn route_outstanding(&self, route: &Route) -> u32 {
        route
            .endpoints
            .iter()
            .filter_map(|e| self.get(&e.id))
            .map(|runtime| runtime.outstanding())
            .sum()
    }

    pub fn record_queue_wait(&self, route: &Route, wait: Duration) {
        metrics::histogram!(
            router_obs::metrics::QUEUE_WAIT_SECONDS,
            router_obs::metrics::labels::ROUTE => route.id.clone()
        )
        .record(wait.as_secs_f64());
        let now = self.now();
        lock(&self.pressure).record_queue_wait(&route.id, wait, self.route_outstanding(route), now);
    }

    pub fn record_ttft(&self, route: &Route, endpoint: &Endpoint, ttft: Duration) {
        let runtime = self.endpoint(endpoint);
        runtime.record_ttft(ttft);
        metrics::histogram!(
            router_obs::metrics::UPSTREAM_TTFT_SECONDS,
            router_obs::metrics::labels::ENDPOINT => endpoint.id.clone(),
            router_obs::metrics::labels::PROVIDER => endpoint.provider.clone()
        )
        .record(ttft.as_secs_f64());
        lock(&self.pressure).record_ttft(
            &route.id,
            &endpoint.provider,
            &endpoint.id,
            ttft,
            runtime.outstanding(),
        );
    }

    pub fn poll_degraded(&self) -> Vec<Degraded> {
        let now = self.now();
        lock(&self.pressure).tick(now)
    }

    pub fn get(&self, id: &str) -> Option<Arc<EndpointRuntime>> {
        self.endpoints.get(id).map(|e| e.clone())
    }

    pub fn record_attempt(
        &self,
        endpoint: &Endpoint,
        outcome: Outcome,
        status: Option<u16>,
        now: Duration,
        rng: &mut impl Rng,
    ) {
        let runtime = self.endpoint(endpoint);
        runtime.record_outcome(outcome, now, rng);
        if endpoint.provider.is_empty() {
            return;
        }
        let provider = self.provider_entry(&endpoint.provider);
        let mut circuit = lock(&provider);
        match (outcome, status) {
            (_, Some(401 | 403)) => circuit.record_auth_failure(),
            (Outcome::Success, _) => circuit.clear_auth_failure(),
            _ => {}
        }
        drop(circuit);
        self.publish_circuit_gauges(endpoint, now);
    }

    pub fn provider_open(&self, provider: &str, now: Duration) -> bool {
        if provider.is_empty() {
            return false;
        }
        let states: Vec<State> = self
            .endpoints
            .iter()
            .filter(|entry| entry.value().provider == provider)
            .map(|entry| entry.value().circuit_state(now))
            .collect();
        match self.providers.get(provider) {
            Some(circuit) => lock(circuit.value()).is_open(states.iter()),
            None => ProviderCircuit::default().is_open(states.iter()),
        }
    }

    pub fn endpoint_circuit_open(&self, endpoint: &Endpoint, now: Duration) -> bool {
        self.get(&endpoint.id)
            .map(|rt| rt.circuit_state(now).is_open())
            .unwrap_or(false)
    }

    pub fn endpoint_open(&self, endpoint: &Endpoint, now: Duration) -> bool {
        self.endpoint_circuit_open(endpoint, now) || self.provider_open(&endpoint.provider, now)
    }

    pub fn endpoint_healthy(&self, endpoint: &Endpoint, now: Duration) -> bool {
        endpoint.accepts_traffic()
            && !self.endpoint_open(endpoint, now)
            && self
                .get(&endpoint.id)
                .map(|rt| rt.has_headroom())
                .unwrap_or(true)
    }

    pub fn status(&self, endpoint: &Endpoint) -> Option<EndpointStatus> {
        let runtime = self.get(&endpoint.id)?;
        let now = self.now();
        Some(EndpointStatus {
            circuit: runtime.circuit_state(now).label(),
            provider_circuit_open: self.provider_open(&endpoint.provider, now),
            outstanding: runtime.outstanding(),
            concurrency_limit: runtime.limit(),
            ewma_ttft_secs: runtime.ewma_ttft_secs(),
        })
    }

    fn publish_circuit_gauges(&self, endpoint: &Endpoint, now: Duration) {
        if let Some(runtime) = self.get(&endpoint.id) {
            metrics::gauge!(
                router_obs::metrics::CIRCUIT_STATE,
                router_obs::metrics::labels::ENDPOINT => endpoint.id.clone(),
                router_obs::metrics::labels::PROVIDER => endpoint.provider.clone()
            )
            .set(runtime.circuit_state(now).gauge_value());
            metrics::gauge!(
                router_obs::metrics::CONCURRENCY_LIMIT,
                router_obs::metrics::labels::ENDPOINT => endpoint.id.clone()
            )
            .set(f64::from(runtime.limit()));
        }
    }

    fn provider_entry(
        &self,
        provider: &str,
    ) -> dashmap::mapref::one::Ref<'_, String, Mutex<ProviderCircuit>> {
        self.providers
            .entry(provider.to_string())
            .or_insert_with(|| Mutex::new(ProviderCircuit::default()))
            .downgrade()
    }

    pub fn retain_snapshot(&self, snapshot: &Snapshot) {
        let live: HashSet<&str> = snapshot.endpoints().map(|(_, e)| e.id.as_str()).collect();
        self.endpoints.retain(|id, _| live.contains(id.as_str()));
        let providers: HashSet<&str> = snapshot
            .endpoints()
            .map(|(_, e)| e.provider.as_str())
            .collect();
        self.providers
            .retain(|id, _| providers.contains(id.as_str()));
        let routes: HashSet<&str> = snapshot.routes.iter().map(|r| r.id.as_str()).collect();
        self.budgets.retain(|id, _| routes.contains(id.as_str()));
        self.pins.retain(|id, _| routes.contains(id.as_str()));
        self.admission.retain_routes(|id| routes.contains(id));
        let routes: Vec<&str> = routes.into_iter().collect();
        let live: Vec<&str> = live.into_iter().collect();
        lock(&self.pressure).forget_except(&routes, &live);
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
                !endpoint.circuit_state(self.now).is_open()
                    && !self.runtime.provider_open(&endpoint.provider, self.now)
                    && endpoint.has_headroom()
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
        let runtime = Runtime::default();
        let rt = runtime.endpoint(&endpoint("a"));
        assert_eq!(rt.outstanding(), 0);
        let guard = OutstandingGuard::acquire(rt.clone());
        assert_eq!(rt.outstanding(), 1);
        drop(guard);
        assert_eq!(rt.outstanding(), 0);
    }

    #[test]
    fn retain_drops_endpoints_missing_from_snapshot() {
        let runtime = Runtime::default();
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
        let runtime = Runtime::default();
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
    fn provider_circuit_opens_at_half_of_its_endpoints_or_on_auth_failure() {
        let runtime = Runtime::default();
        let a1 = Endpoint {
            provider: "pa".into(),
            ..endpoint("a1")
        };
        let a2 = Endpoint {
            provider: "pa".into(),
            ..endpoint("a2")
        };
        let b1 = Endpoint {
            provider: "pb".into(),
            ..endpoint("b1")
        };
        let mut rng = ThreadRng;
        for e in [&a1, &a2, &b1] {
            runtime.endpoint(e);
        }
        let now = runtime.now();
        assert!(!runtime.provider_open("pa", now));
        for _ in 0..5 {
            runtime.record_attempt(&a1, Outcome::Transient, Some(502), now, &mut rng);
        }
        assert!(runtime.provider_open("pa", now));
        assert!(!runtime.provider_open("pb", now));
        assert!(runtime.endpoint_open(&a2, now));
        assert!(!runtime.endpoint_healthy(&a2, now));
        assert!(runtime.endpoint_healthy(&b1, now));
        let excluded = HashSet::new();
        let view = runtime.view(&excluded, None);
        assert!(!view.available("a2"));
        assert!(view.available("b1"));

        runtime.record_attempt(&b1, Outcome::Fatal, Some(401), now, &mut rng);
        assert!(runtime.provider_open("pb", now));
        runtime.record_attempt(&b1, Outcome::Success, Some(200), now, &mut rng);
        assert!(!runtime.provider_open("pb", now));

        let status = runtime.status(&a1).unwrap();
        assert_eq!(status.circuit, "open");
        assert!(status.provider_circuit_open);
        assert!(runtime.status(&endpoint("unknown")).is_none());
    }

    #[test]
    fn pins_are_per_route_and_listed_with_age() {
        let runtime = Runtime::default();
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
        let runtime = Runtime::default();
        let a = runtime.endpoint(&endpoint("a"));
        let before = a.limit();
        a.record_ttft(Duration::from_millis(10));
        a.record_outcome(Outcome::Capacity, runtime.now(), &mut ThreadRng);
        assert!(a.limit() <= before);
        assert!(a.ewma_ttft_secs().is_some());
    }
}
