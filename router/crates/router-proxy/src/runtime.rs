use dashmap::DashMap;
use router_core::circuit::{Circuit, CircuitConfig, ProviderCircuit, State};
use router_core::limit::{AdmissionQueue, Gradient2, Gradient2Config};
use router_core::outcome::Outcome;
use router_core::pressure::{PressureConfig, PressureDetector};
use router_core::probe::{ProbeConfig, ProbeOutcome, ProbeState, ProbeTracker, ProbeTransition};
use router_core::retry::{RetryBudget, RetryConfig};
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
    probe: Mutex<ProbeTracker>,
    outstanding: AtomicU32,
    ewma_ttft: Mutex<Option<f64>>,
    released: Arc<Notify>,
}

impl EndpointRuntime {
    fn new(
        endpoint: &Endpoint,
        circuit: CircuitConfig,
        probe: &ProbeConfig,
        released: Arc<Notify>,
    ) -> Self {
        Self {
            id: endpoint.id.clone(),
            provider: endpoint.provider.clone(),
            circuit: Mutex::new(Circuit::new(circuit)),
            limiter: Mutex::new(Gradient2::new(Gradient2Config::for_max_concurrency(
                endpoint.max_concurrency,
            ))),
            probe: Mutex::new(ProbeTracker::new(probe)),
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

    pub fn probe_down(&self) -> bool {
        lock(&self.probe).is_down()
    }

    pub fn probe_status(&self) -> ProbeStatus {
        let tracker = lock(&self.probe);
        ProbeStatus {
            state: tracker.state(),
            consecutive_failures: tracker.consecutive_failures(),
            consecutive_successes: tracker.consecutive_successes(),
            last_outcome: tracker.last_outcome(),
            last_status: tracker.last_status(),
            probes: tracker.probes(),
        }
    }

    pub fn record_probe(
        &self,
        outcome: ProbeOutcome,
        status: Option<u16>,
        now: Duration,
        rng: &mut impl Rng,
    ) -> ProbeTransition {
        let mut tracker = lock(&self.probe);
        let transition = tracker.record(outcome, status);
        let down = tracker.is_down();
        drop(tracker);
        let mut circuit = lock(&self.circuit);
        match (outcome, transition) {
            (ProbeOutcome::Failure, _) if down => {
                circuit.eject(now, rng);
            }
            (ProbeOutcome::Failure, _) => circuit.record(Outcome::Transient, now, rng),
            (ProbeOutcome::Success, ProbeTransition::CameUp) => {
                circuit.release(now);
            }
            (ProbeOutcome::Success | ProbeOutcome::Warming, _) => {}
        }
        transition
    }

    pub fn ewma_ttft_secs(&self) -> Option<f64> {
        *lock(&self.ewma_ttft)
    }

    pub fn has_headroom(&self) -> bool {
        lock(&self.limiter).has_headroom(self.outstanding())
    }

    pub fn try_reserve(self: &Arc<Self>) -> Option<OutstandingGuard> {
        let limiter = lock(&self.limiter);
        if !limiter.has_headroom(self.outstanding()) {
            return None;
        }
        self.outstanding.fetch_add(1, Ordering::Relaxed);
        drop(limiter);
        Some(OutstandingGuard {
            endpoint: self.clone(),
        })
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

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ProbeStatus {
    pub state: ProbeState,
    pub consecutive_failures: u32,
    pub consecutive_successes: u32,
    pub last_outcome: Option<ProbeOutcome>,
    pub last_status: Option<u16>,
    pub probes: u64,
}

#[derive(Clone, Debug, PartialEq)]
pub struct EndpointStatus {
    pub circuit: &'static str,
    pub provider_circuit_open: bool,
    pub outstanding: u32,
    pub concurrency_limit: u32,
    pub ewma_ttft_secs: Option<f64>,
    pub probe: ProbeStatus,
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
    probe_config: ProbeConfig,
    retry_config: RetryConfig,
    started: Instant,
}

impl Default for Runtime {
    fn default() -> Self {
        Self::new(
            CircuitConfig::default(),
            AdmissionQueue::default(),
            PressureConfig::default(),
            ProbeConfig::default(),
            RetryConfig::default(),
        )
    }
}

impl Runtime {
    pub fn new(
        circuit_config: CircuitConfig,
        admission: AdmissionQueue,
        pressure: PressureConfig,
        probe_config: ProbeConfig,
        retry_config: RetryConfig,
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
            probe_config,
            retry_config,
            started: Instant::now(),
        }
    }

    pub fn circuit_config(&self) -> &CircuitConfig {
        &self.circuit_config
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
                    &self.probe_config,
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

    pub fn route_has_routable(&self, route: &Route) -> bool {
        let now = self.now();
        route
            .endpoints
            .iter()
            .any(|e| e.accepts_traffic() && !self.endpoint_open(e, now))
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
        let still_queued = self.admission.waiting(&route.id);
        lock(&self.pressure).record_queue_wait(
            &route.id,
            wait,
            still_queued,
            self.route_outstanding(route),
            now,
        );
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

    pub fn finish_attempt(
        &self,
        route_id: &str,
        endpoint: &Endpoint,
        outcome: Outcome,
        label: &'static str,
        status: Option<u16>,
        rng: &mut impl Rng,
    ) {
        let now = self.now();
        self.record_attempt(endpoint, outcome, status, now, rng);
        metrics::counter!(
            router_obs::metrics::REQUESTS_TOTAL,
            router_obs::metrics::labels::ROUTE => route_id.to_string(),
            router_obs::metrics::labels::ENDPOINT => endpoint.id.clone(),
            router_obs::metrics::labels::OUTCOME => label
        )
        .increment(1);
    }

    pub fn record_probe(&self, endpoint: &Endpoint, outcome: ProbeOutcome, status: Option<u16>) {
        let runtime = self.endpoint(endpoint);
        let now = self.now();
        let transition = runtime.record_probe(outcome, status, now, &mut ThreadRng);
        metrics::counter!(
            router_obs::metrics::PROBE_TOTAL,
            router_obs::metrics::labels::ENDPOINT => endpoint.id.clone(),
            router_obs::metrics::labels::OUTCOME => outcome.label()
        )
        .increment(1);
        match transition {
            ProbeTransition::WentDown => {
                tracing::warn!(
                    endpoint = %endpoint.id,
                    provider = %endpoint.provider,
                    status = ?status,
                    "active probe ejected endpoint"
                );
                metrics::counter!(
                    router_obs::metrics::FAILOVERS_TOTAL,
                    router_obs::metrics::labels::FROM => endpoint.provider.clone(),
                    router_obs::metrics::labels::REASON => "probe"
                )
                .increment(1);
            }
            ProbeTransition::CameUp => {
                tracing::info!(
                    endpoint = %endpoint.id,
                    provider = %endpoint.provider,
                    "active probe restored endpoint"
                );
            }
            ProbeTransition::Unchanged => {}
        }
        if outcome != ProbeOutcome::Warming {
            self.publish_circuit_gauges(endpoint, now);
        }
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
            None => self
                .circuit_config
                .provider_circuit()
                .is_open(states.iter()),
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
            probe: runtime.probe_status(),
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
            .or_insert_with(|| Mutex::new(self.circuit_config.provider_circuit()))
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
            .or_insert_with(|| Mutex::new(self.retry_config.budget()))
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
    use router_core::snapshot::{DegradedReason, Route};

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
    fn reservations_stop_at_the_adaptive_limit() {
        let runtime = Runtime::default();
        let rt = runtime.endpoint(&endpoint("a"));
        let limit = rt.limit() as usize;
        let guards: Vec<OutstandingGuard> = (0..limit).filter_map(|_| rt.try_reserve()).collect();
        assert_eq!(guards.len(), limit);
        assert!(rt.try_reserve().is_none());
        assert!(!rt.has_headroom());
        drop(guards);
        assert!(rt.try_reserve().is_some());
    }

    #[test]
    fn probe_failures_eject_and_recovery_moves_to_half_open_not_closed() {
        let runtime = Runtime::default();
        let a = endpoint("a");
        let b = endpoint("b");
        runtime.endpoint(&b);
        for _ in 0..2 {
            runtime.record_probe(&a, ProbeOutcome::Failure, Some(503));
            assert!(!runtime.endpoint_circuit_open(&a, runtime.now()));
        }
        runtime.record_probe(&a, ProbeOutcome::Failure, Some(503));
        let now = runtime.now();
        assert!(runtime.endpoint_circuit_open(&a, now));
        assert!(!runtime.endpoint_healthy(&a, now));
        assert!(runtime.endpoint_healthy(&b, now));
        let status = runtime.status(&a).unwrap();
        assert_eq!(status.circuit, "open");
        assert_eq!(status.probe.state, ProbeState::Down);
        assert_eq!(status.probe.consecutive_failures, 3);
        let excluded = HashSet::new();
        assert!(!runtime.view(&excluded, None).available("a"));

        runtime.record_probe(&a, ProbeOutcome::Warming, Some(204));
        assert!(runtime.endpoint_circuit_open(&a, runtime.now()));
        for _ in 0..2 {
            runtime.record_probe(&a, ProbeOutcome::Success, Some(200));
            assert!(runtime.endpoint_circuit_open(&a, runtime.now()));
        }
        runtime.record_probe(&a, ProbeOutcome::Success, Some(200));
        let now = runtime.now();
        assert!(!runtime.endpoint_circuit_open(&a, now));
        let status = runtime.status(&a).unwrap();
        assert_eq!(status.circuit, "half_open");
        assert_eq!(status.probe.state, ProbeState::Up);
        assert_eq!(status.probe.probes, 7);
        assert!(runtime.view(&excluded, None).available("a"));
    }

    #[test]
    fn first_probe_success_streak_leaves_a_circuit_opened_by_traffic_open() {
        let runtime = Runtime::default();
        let rt = runtime.endpoint(&endpoint("a"));
        let mut rng = ThreadRng;
        let t0 = Duration::from_secs(100);
        for _ in 0..5 {
            rt.record_outcome(Outcome::Fatal, t0, &mut rng);
        }
        assert!(rt.circuit_state(t0).is_open());
        let mut transitions = Vec::new();
        for i in 1..=3 {
            transitions.push(rt.record_probe(
                ProbeOutcome::Success,
                Some(200),
                t0 + Duration::from_secs(i),
                &mut rng,
            ));
        }
        assert_eq!(transitions.last(), Some(&ProbeTransition::CameUp));
        assert_eq!(rt.probe_status().state, ProbeState::Up);
        assert!(rt.circuit_state(t0 + Duration::from_secs(3)).is_open());
    }

    #[test]
    fn probe_successes_never_close_a_half_open_circuit() {
        let runtime = Runtime::default();
        let rt = runtime.endpoint(&endpoint("a"));
        let mut rng = router_core::rng::ZeroRng;
        let t0 = Duration::from_secs(100);
        for i in 0..3 {
            rt.record_probe(
                ProbeOutcome::Success,
                Some(200),
                t0 + Duration::from_secs(i),
                &mut rng,
            );
        }
        assert_eq!(rt.probe_status().state, ProbeState::Up);
        for _ in 0..5 {
            rt.record_outcome(Outcome::Fatal, t0 + Duration::from_secs(3), &mut rng);
        }
        let trial = t0 + Duration::from_secs(10);
        assert!(rt.admit(trial, &mut rng));
        for i in 0..5 {
            rt.record_probe(
                ProbeOutcome::Success,
                Some(200),
                trial + Duration::from_secs(i),
                &mut rng,
            );
            assert_eq!(
                rt.circuit_state(trial + Duration::from_secs(i)).label(),
                "half_open"
            );
        }
        for i in 0..3 {
            rt.record_outcome(
                Outcome::Success,
                trial + Duration::from_secs(5 + i),
                &mut rng,
            );
        }
        assert!(rt.circuit_state(trial + Duration::from_secs(8)).is_closed());
    }

    #[test]
    fn probe_down_keeps_an_endpoint_out_of_every_gate_until_the_probe_recovers() {
        let runtime = Runtime::new(
            CircuitConfig {
                base_backoff: Duration::from_millis(1),
                max_backoff: Duration::from_millis(1),
                jitter_fraction: 0.0,
                ..CircuitConfig::default()
            },
            AdmissionQueue::default(),
            PressureConfig::default(),
            ProbeConfig::default(),
            RetryConfig::default(),
        );
        let down = Endpoint {
            provider: "pa".into(),
            ..endpoint("down")
        };
        let traffic = Endpoint {
            provider: "pb".into(),
            ..endpoint("traffic")
        };
        let spare = Endpoint {
            provider: "pc".into(),
            ..endpoint("spare")
        };
        runtime.endpoint(&spare);
        let mut rng = router_core::rng::ZeroRng;
        for _ in 0..3 {
            runtime.record_probe(&down, ProbeOutcome::Failure, Some(503));
        }
        let traffic_rt = runtime.endpoint(&traffic);
        for _ in 0..5 {
            runtime.record_attempt(&traffic, Outcome::Fatal, Some(500), runtime.now(), &mut rng);
        }
        std::thread::sleep(Duration::from_millis(20));

        let now = runtime.now();
        let down_rt = runtime.get("down").unwrap();
        assert_eq!(down_rt.probe_status().state, ProbeState::Down);
        assert!(runtime.endpoint_circuit_open(&down, now));
        assert!(runtime.endpoint_open(&down, now));
        assert!(!runtime.endpoint_healthy(&down, now));
        assert!(runtime.provider_open("pa", now));
        let excluded = HashSet::new();
        assert!(!runtime.view(&excluded, None).available("down"));
        assert_eq!(runtime.status(&down).unwrap().circuit, "open");
        assert!(!down_rt.admit(now, &mut rng));

        assert_eq!(traffic_rt.probe_status().state, ProbeState::Unknown);
        assert_eq!(runtime.status(&traffic).unwrap().circuit, "half_open");
        assert!(runtime.view(&excluded, None).available("traffic"));
        assert!(traffic_rt.admit(now, &mut rng));

        for _ in 0..3 {
            runtime.record_probe(&down, ProbeOutcome::Success, Some(200));
        }
        let now = runtime.now();
        assert_eq!(runtime.status(&down).unwrap().circuit, "half_open");
        assert!(runtime.view(&excluded, None).available("down"));
        assert!(down_rt.admit(now, &mut rng));
        for _ in 0..3 {
            runtime.record_attempt(&down, Outcome::Success, Some(200), runtime.now(), &mut rng);
        }
        assert_eq!(runtime.status(&down).unwrap().circuit, "closed");
    }

    #[test]
    fn probe_failures_while_down_reopen_after_the_backoff_expires() {
        let runtime = Runtime::default();
        let a = endpoint("a");
        let rt = runtime.endpoint(&a);
        let mut rng = ThreadRng;
        let t0 = Duration::from_secs(100);
        for i in 0..3 {
            rt.record_probe(
                ProbeOutcome::Failure,
                Some(503),
                t0 + Duration::from_secs(i),
                &mut rng,
            );
        }
        assert!(rt.circuit_state(t0 + Duration::from_secs(2)).is_open());
        assert!(rt.probe_down());
        rt.record_probe(
            ProbeOutcome::Failure,
            Some(503),
            t0 + Duration::from_secs(20),
            &mut rng,
        );
        assert!(rt.circuit_state(t0 + Duration::from_secs(20)).is_open());
        assert_eq!(rt.probe_status().consecutive_failures, 4);
        rt.record_outcome(Outcome::Transient, t0 + Duration::from_secs(21), &mut rng);
        assert!(rt.circuit_state(t0 + Duration::from_secs(21)).is_open());
    }

    #[tokio::test]
    async fn a_short_wait_ends_queue_pressure_only_once_nothing_is_left_queued() {
        let sustained = Duration::from_millis(50);
        let runtime = Arc::new(Runtime::new(
            CircuitConfig::default(),
            AdmissionQueue::default(),
            PressureConfig {
                sustained,
                ..PressureConfig::default()
            },
            ProbeConfig::default(),
            RetryConfig::default(),
        ));
        let route = Route {
            id: "llama".into(),
            endpoints: vec![endpoint("a")],
            ..Default::default()
        };
        let threshold = runtime
            .admission
            .config()
            .max_wait
            .mul_f64(PressureConfig::default().queue_wait_fraction);
        let long_wait = threshold * 2;
        let lucky_wait = threshold / 2;
        let queued = {
            let runtime = runtime.clone();
            tokio::spawn(async move { runtime.admission.wait_for_slot("llama", || false).await })
        };
        while runtime.admission.waiting("llama") == 0 {
            tokio::task::yield_now().await;
        }

        runtime.record_queue_wait(&route, long_wait);
        runtime.record_queue_wait(&route, lucky_wait);
        tokio::time::sleep(sustained * 2).await;
        let signals = runtime.poll_degraded();
        assert_eq!(signals.len(), 1, "{signals:?}");
        assert_eq!(signals[0].service, "llama");
        assert_eq!(signals[0].reason, DegradedReason::QueueDepth);

        queued.abort();
        let _ = queued.await;
        assert_eq!(runtime.admission.waiting("llama"), 0);
        runtime.record_queue_wait(&route, lucky_wait);
        runtime.record_queue_wait(&route, long_wait);
        tokio::time::sleep(sustained * 2).await;
        assert_eq!(runtime.poll_degraded().len(), 1);
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
