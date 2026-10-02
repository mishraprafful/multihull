use crate::outcome::Outcome;
use crate::rng::Rng;
use serde::{Deserialize, Serialize};
use std::collections::VecDeque;
use std::time::Duration;

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct CircuitConfig {
    pub consecutive_failures: u32,
    pub error_ratio: f64,
    pub ratio_window: Duration,
    pub min_samples: u32,
    pub base_backoff: Duration,
    pub max_backoff: Duration,
    pub jitter_fraction: f64,
    pub half_open_ramp: Duration,
    pub probe_successes_to_close: u32,
}

impl Default for CircuitConfig {
    fn default() -> Self {
        Self {
            consecutive_failures: 5,
            error_ratio: 0.5,
            ratio_window: Duration::from_secs(10),
            min_samples: 20,
            base_backoff: Duration::from_secs(5),
            max_backoff: Duration::from_secs(300),
            jitter_fraction: 0.25,
            half_open_ramp: Duration::from_secs(30),
            probe_successes_to_close: 3,
        }
    }
}

impl CircuitConfig {
    pub fn backoff(&self, backoff_n: u32, rng: &mut impl Rng) -> Duration {
        let exponent = backoff_n.min(30);
        let scaled = self.base_backoff.saturating_mul(1u32 << exponent);
        let capped = scaled.min(self.max_backoff);
        capped.mul_f64(1.0 + self.jitter_fraction * rng.next_f64())
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum State {
    Closed,
    Open {
        since: Duration,
        backoff_n: u32,
        wait: Duration,
    },
    HalfOpen {
        admitted: u32,
        started: Duration,
        probe_successes: u32,
        backoff_n: u32,
    },
}

impl State {
    pub fn is_open(&self) -> bool {
        matches!(self, State::Open { .. })
    }

    pub fn is_closed(&self) -> bool {
        matches!(self, State::Closed)
    }

    pub fn label(&self) -> &'static str {
        match self {
            State::Closed => "closed",
            State::Open { .. } => "open",
            State::HalfOpen { .. } => "half_open",
        }
    }

    pub fn gauge_value(&self) -> f64 {
        match self {
            State::Closed => 0.0,
            State::HalfOpen { .. } => 1.0,
            State::Open { .. } => 2.0,
        }
    }
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
struct Bucket {
    second: u64,
    failures: u32,
    total: u32,
}

#[derive(Clone, Debug)]
pub struct Circuit {
    config: CircuitConfig,
    state: State,
    consecutive_failures: u32,
    buckets: VecDeque<Bucket>,
}

impl Default for Circuit {
    fn default() -> Self {
        Self::new(CircuitConfig::default())
    }
}

impl Circuit {
    pub fn new(config: CircuitConfig) -> Self {
        Self {
            config,
            state: State::Closed,
            consecutive_failures: 0,
            buckets: VecDeque::new(),
        }
    }

    pub fn state(&self) -> State {
        self.state
    }

    pub fn config(&self) -> &CircuitConfig {
        &self.config
    }

    pub fn peek(&self, now: Duration) -> State {
        match self.state {
            State::Open {
                since,
                backoff_n,
                wait,
            } if now >= since + wait => State::HalfOpen {
                admitted: 0,
                started: now,
                probe_successes: 0,
                backoff_n,
            },
            other => other,
        }
    }

    pub fn admit(&mut self, now: Duration, rng: &mut impl Rng) -> bool {
        self.advance(now);
        match &mut self.state {
            State::Closed => true,
            State::Open { .. } => false,
            State::HalfOpen {
                admitted, started, ..
            } => {
                if *admitted == 0 {
                    *admitted += 1;
                    return true;
                }
                let fraction =
                    half_open_fraction(now.saturating_sub(*started), self.config.half_open_ramp);
                if rng.next_f64() < fraction {
                    *admitted += 1;
                    true
                } else {
                    false
                }
            }
        }
    }

    pub fn record(&mut self, outcome: Outcome, now: Duration, rng: &mut impl Rng) {
        self.advance(now);
        match self.state {
            State::Closed => self.record_closed(outcome, now, rng),
            State::Open { .. } => {}
            State::HalfOpen {
                probe_successes,
                backoff_n,
                ..
            } => self.record_half_open(outcome, now, rng, probe_successes, backoff_n),
        }
    }

    fn advance(&mut self, now: Duration) {
        if let State::Open {
            since,
            backoff_n,
            wait,
        } = self.state
        {
            if now >= since + wait {
                self.state = State::HalfOpen {
                    admitted: 0,
                    started: now,
                    probe_successes: 0,
                    backoff_n,
                };
            }
        }
    }

    fn record_closed(&mut self, outcome: Outcome, now: Duration, rng: &mut impl Rng) {
        match outcome {
            Outcome::Success => {
                self.consecutive_failures = 0;
                self.bucket_mut(now).total += 1;
            }
            outcome if outcome.counts_toward_ejection() => {
                self.consecutive_failures += 1;
                let bucket = self.bucket_mut(now);
                bucket.total += 1;
                bucket.failures += 1;
            }
            _ => return,
        }
        if self.should_trip(now) {
            self.open(now, 0, rng);
        }
    }

    fn record_half_open(
        &mut self,
        outcome: Outcome,
        now: Duration,
        rng: &mut impl Rng,
        probe_successes: u32,
        backoff_n: u32,
    ) {
        match outcome {
            Outcome::Success => {
                let successes = probe_successes + 1;
                if successes >= self.config.probe_successes_to_close {
                    self.close();
                } else if let State::HalfOpen {
                    probe_successes, ..
                } = &mut self.state
                {
                    *probe_successes = successes;
                }
            }
            outcome if outcome.counts_toward_ejection() => {
                self.open(now, backoff_n + 1, rng);
            }
            _ => {}
        }
    }

    fn should_trip(&mut self, now: Duration) -> bool {
        if self.consecutive_failures >= self.config.consecutive_failures {
            return true;
        }
        self.prune(now);
        let (failures, total) = self
            .buckets
            .iter()
            .fold((0u32, 0u32), |(f, t), b| (f + b.failures, t + b.total));
        total >= self.config.min_samples
            && f64::from(failures) / f64::from(total) > self.config.error_ratio
    }

    fn open(&mut self, now: Duration, backoff_n: u32, rng: &mut impl Rng) {
        let wait = self.config.backoff(backoff_n, rng);
        self.state = State::Open {
            since: now,
            backoff_n,
            wait,
        };
        self.consecutive_failures = 0;
        self.buckets.clear();
    }

    fn close(&mut self) {
        self.state = State::Closed;
        self.consecutive_failures = 0;
        self.buckets.clear();
    }

    fn bucket_mut(&mut self, now: Duration) -> &mut Bucket {
        self.prune(now);
        let second = now.as_secs();
        let needs_new = self
            .buckets
            .back()
            .map(|b| b.second != second)
            .unwrap_or(true);
        if needs_new {
            self.buckets.push_back(Bucket {
                second,
                ..Bucket::default()
            });
        }
        self.buckets.back_mut().expect("bucket pushed above")
    }

    fn prune(&mut self, now: Duration) {
        let oldest_kept = now
            .as_secs()
            .saturating_sub(self.config.ratio_window.as_secs().saturating_sub(1));
        while self
            .buckets
            .front()
            .map(|b| b.second < oldest_kept)
            .unwrap_or(false)
        {
            self.buckets.pop_front();
        }
    }
}

fn half_open_fraction(elapsed: Duration, ramp: Duration) -> f64 {
    let step = ramp / 3;
    if elapsed < step {
        0.05
    } else if elapsed < step * 2 {
        0.25
    } else {
        1.0
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct ProviderCircuit {
    pub open_ratio_threshold: f64,
    auth_failure: bool,
}

impl Default for ProviderCircuit {
    fn default() -> Self {
        Self {
            open_ratio_threshold: 0.5,
            auth_failure: false,
        }
    }
}

impl ProviderCircuit {
    pub fn record_auth_failure(&mut self) {
        self.auth_failure = true;
    }

    pub fn clear_auth_failure(&mut self) {
        self.auth_failure = false;
    }

    pub fn auth_failed(&self) -> bool {
        self.auth_failure
    }

    pub fn is_open<'a>(&self, endpoint_states: impl IntoIterator<Item = &'a State>) -> bool {
        if self.auth_failure {
            return true;
        }
        let (open, total) = endpoint_states
            .into_iter()
            .fold((0usize, 0usize), |(open, total), state| {
                (open + usize::from(state.is_open()), total + 1)
            });
        total > 0 && open as f64 / total as f64 >= self.open_ratio_threshold
    }
}

pub const PANIC_THRESHOLD: f64 = 0.5;

pub fn apply_panic_threshold<T>(
    candidates: &[T],
    is_open: impl Fn(&T) -> bool,
    threshold: f64,
) -> Vec<&T> {
    if candidates.is_empty() {
        return Vec::new();
    }
    let open = candidates.iter().filter(|c| is_open(c)).count();
    if open as f64 / candidates.len() as f64 > threshold {
        candidates.iter().collect()
    } else {
        candidates.iter().filter(|c| !is_open(c)).collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::rng::{SeededRng, ZeroRng};

    fn secs(s: u64) -> Duration {
        Duration::from_secs(s)
    }

    fn millis(ms: u64) -> Duration {
        Duration::from_millis(ms)
    }

    #[test]
    fn five_consecutive_failures_open_the_circuit() {
        let mut circuit = Circuit::default();
        let mut rng = ZeroRng;
        for i in 0..4 {
            circuit.record(Outcome::Transient, millis(i), &mut rng);
            assert!(circuit.state().is_closed());
        }
        circuit.record(Outcome::Fatal, millis(4), &mut rng);
        assert_eq!(
            circuit.state(),
            State::Open {
                since: millis(4),
                backoff_n: 0,
                wait: secs(5)
            }
        );
    }

    #[test]
    fn success_resets_consecutive_counter() {
        let mut circuit = Circuit::default();
        let mut rng = ZeroRng;
        for i in 0..4 {
            circuit.record(Outcome::Transient, millis(i), &mut rng);
        }
        circuit.record(Outcome::Success, millis(10), &mut rng);
        for i in 0..4 {
            circuit.record(Outcome::Transient, millis(20 + i), &mut rng);
        }
        assert!(circuit.state().is_closed());
    }

    #[test]
    fn capacity_never_opens_the_circuit() {
        let mut circuit = Circuit::default();
        let mut rng = ZeroRng;
        for i in 0..100 {
            circuit.record(Outcome::Capacity, millis(i), &mut rng);
        }
        assert!(circuit.state().is_closed());
    }

    #[test]
    fn error_ratio_needs_min_samples() {
        let mut circuit = Circuit::default();
        let mut rng = ZeroRng;
        for i in 0..19 {
            let outcome = if i % 2 == 0 {
                Outcome::Transient
            } else {
                Outcome::Success
            };
            circuit.record(outcome, millis(i * 100), &mut rng);
        }
        assert!(circuit.state().is_closed());
    }

    #[test]
    fn error_ratio_above_half_opens_with_enough_samples() {
        let mut circuit = Circuit::default();
        let mut rng = ZeroRng;
        let pattern = [
            Outcome::Transient,
            Outcome::Transient,
            Outcome::Success,
            Outcome::Transient,
            Outcome::Success,
        ];
        for i in 0..20 {
            circuit.record(pattern[i % pattern.len()], millis(i as u64 * 200), &mut rng);
        }
        assert!(circuit.state().is_open());
    }

    #[test]
    fn old_samples_fall_out_of_the_window() {
        let mut circuit = Circuit::default();
        let mut rng = ZeroRng;
        for i in 0..15 {
            circuit.record(Outcome::Transient, millis(i * 10), &mut rng);
            circuit.record(Outcome::Success, millis(i * 10 + 5), &mut rng);
            circuit.record(Outcome::Success, millis(i * 10 + 6), &mut rng);
        }
        assert!(circuit.state().is_closed());
        for i in 0..12 {
            let outcome = if i % 3 == 0 {
                Outcome::Success
            } else {
                Outcome::Transient
            };
            circuit.record(outcome, secs(30) + millis(i * 10), &mut rng);
            if i % 3 == 2 {
                circuit.record(Outcome::Success, secs(30) + millis(i * 10 + 1), &mut rng);
            }
        }
        assert!(circuit.state().is_closed());
    }

    #[test]
    fn peek_reports_pending_half_open_without_mutating() {
        let mut circuit = Circuit::default();
        let mut rng = ZeroRng;
        for i in 0..5 {
            circuit.record(Outcome::Transient, millis(i), &mut rng);
        }
        assert!(circuit.peek(secs(1)).is_open());
        assert!(!circuit.peek(secs(6)).is_open());
        assert!(circuit.state().is_open());
    }

    #[test]
    fn open_rejects_until_backoff_elapsed_then_half_opens() {
        let mut circuit = Circuit::default();
        let mut rng = ZeroRng;
        for i in 0..5 {
            circuit.record(Outcome::Transient, millis(i), &mut rng);
        }
        assert!(!circuit.admit(secs(4), &mut rng));
        assert!(circuit.state().is_open());
        assert!(circuit.admit(secs(6), &mut rng));
        assert!(matches!(
            circuit.state(),
            State::HalfOpen {
                admitted: 1,
                probe_successes: 0,
                backoff_n: 0,
                ..
            }
        ));
    }

    #[test]
    fn half_open_ramp_admits_one_then_five_then_twentyfive_then_all_percent() {
        let mut circuit = Circuit::default();
        let mut rng = ZeroRng;
        for i in 0..5 {
            circuit.record(Outcome::Transient, millis(i), &mut rng);
        }
        let start = secs(10);
        assert!(circuit.admit(start, &mut rng));
        let mut low = || 0.04;
        let mut mid = || 0.2;
        let mut high = || 0.9;
        assert!(circuit.admit(start + secs(1), &mut low));
        assert!(!circuit.admit(start + secs(1), &mut mid));
        assert!(circuit.admit(start + secs(11), &mut mid));
        assert!(!circuit.admit(start + secs(11), &mut high));
        assert!(circuit.admit(start + secs(21), &mut high));
    }

    #[test]
    fn three_probe_successes_close_the_circuit() {
        let mut circuit = Circuit::default();
        let mut rng = ZeroRng;
        for i in 0..5 {
            circuit.record(Outcome::Transient, millis(i), &mut rng);
        }
        assert!(circuit.admit(secs(6), &mut rng));
        circuit.record(Outcome::Success, secs(6), &mut rng);
        circuit.record(Outcome::Success, secs(7), &mut rng);
        assert!(matches!(
            circuit.state(),
            State::HalfOpen {
                probe_successes: 2,
                ..
            }
        ));
        circuit.record(Outcome::Success, secs(8), &mut rng);
        assert!(circuit.state().is_closed());
    }

    #[test]
    fn half_open_failure_reopens_with_doubled_backoff() {
        let mut circuit = Circuit::default();
        let mut rng = ZeroRng;
        for i in 0..5 {
            circuit.record(Outcome::Transient, millis(i), &mut rng);
        }
        assert!(circuit.admit(secs(6), &mut rng));
        circuit.record(Outcome::Transient, secs(6), &mut rng);
        assert_eq!(
            circuit.state(),
            State::Open {
                since: secs(6),
                backoff_n: 1,
                wait: secs(10)
            }
        );
        assert!(!circuit.admit(secs(15), &mut rng));
        assert!(circuit.admit(secs(16), &mut rng));
        circuit.record(Outcome::Fatal, secs(16), &mut rng);
        assert_eq!(
            circuit.state(),
            State::Open {
                since: secs(16),
                backoff_n: 2,
                wait: secs(20)
            }
        );
    }

    #[test]
    fn backoff_caps_at_five_minutes_and_adds_jitter() {
        let config = CircuitConfig::default();
        let mut zero = ZeroRng;
        assert_eq!(config.backoff(0, &mut zero), secs(5));
        assert_eq!(config.backoff(3, &mut zero), secs(40));
        assert_eq!(config.backoff(10, &mut zero), secs(300));
        assert_eq!(config.backoff(60, &mut zero), secs(300));
        let mut full = || 1.0;
        assert_eq!(config.backoff(0, &mut full), Duration::from_millis(6250));
        let mut seeded = SeededRng::new(3);
        let jittered = config.backoff(0, &mut seeded);
        assert!(jittered >= secs(5) && jittered <= Duration::from_millis(6250));
    }

    #[test]
    fn provider_opens_at_half_of_endpoints_open_or_on_auth_failure() {
        let open = State::Open {
            since: secs(0),
            backoff_n: 0,
            wait: secs(5),
        };
        let provider = ProviderCircuit::default();
        assert!(!provider.is_open([&State::Closed, &State::Closed, &open]));
        assert!(provider.is_open([&State::Closed, &open]));
        assert!(provider.is_open([&open]));
        assert!(!provider.is_open(std::iter::empty()));
        let mut failed = ProviderCircuit::default();
        failed.record_auth_failure();
        assert!(failed.is_open([&State::Closed, &State::Closed]));
        failed.clear_auth_failure();
        assert!(!failed.is_open([&State::Closed, &State::Closed]));
    }

    #[test]
    fn panic_threshold_routes_to_all_when_most_are_open() {
        let endpoints = [("a", true), ("b", true), ("c", false)];
        let routed = apply_panic_threshold(&endpoints, |e| e.1, PANIC_THRESHOLD);
        assert_eq!(routed.len(), 3);
        let endpoints = [("a", true), ("b", false), ("c", false)];
        let routed = apply_panic_threshold(&endpoints, |e| e.1, PANIC_THRESHOLD);
        assert_eq!(routed.len(), 2);
        assert!(routed.iter().all(|e| !e.1));
        let none: [(&str, bool); 0] = [];
        assert!(apply_panic_threshold(&none, |e| e.1, PANIC_THRESHOLD).is_empty());
    }
}
