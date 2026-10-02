use crate::snapshot::{Degraded, DegradedReason};
use serde::{Deserialize, Serialize};
use std::collections::{HashMap, VecDeque};
use std::time::Duration;

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct PressureConfig {
    pub queue_wait_fraction: f64,
    #[serde(with = "crate::serde_secs")]
    pub sustained: Duration,
    #[serde(with = "crate::serde_secs")]
    pub stale_after: Duration,
    pub ttft_degrade_factor: f64,
    pub ttft_window: usize,
    pub ttft_baseline_smoothing: f64,
}

impl PressureConfig {
    pub fn validate(&self) -> Result<(), String> {
        if !(self.queue_wait_fraction > 0.0 && self.queue_wait_fraction <= 1.0) {
            return Err("pressure.queue_wait_fraction must be in (0, 1]".into());
        }
        if self.sustained.is_zero() {
            return Err("pressure.sustained must be positive".into());
        }
        if self.stale_after <= self.sustained {
            return Err("pressure.stale_after must exceed pressure.sustained".into());
        }
        if self.ttft_degrade_factor <= 1.0 {
            return Err("pressure.ttft_degrade_factor must exceed 1".into());
        }
        if self.ttft_window < 2 {
            return Err("pressure.ttft_window must be at least 2".into());
        }
        if !(self.ttft_baseline_smoothing > 0.0 && self.ttft_baseline_smoothing <= 1.0) {
            return Err("pressure.ttft_baseline_smoothing must be in (0, 1]".into());
        }
        Ok(())
    }
}

impl Default for PressureConfig {
    fn default() -> Self {
        Self {
            queue_wait_fraction: 0.5,
            sustained: Duration::from_secs(2),
            stale_after: Duration::from_secs(10),
            ttft_degrade_factor: 2.0,
            ttft_window: 50,
            ttft_baseline_smoothing: 0.2,
        }
    }
}

#[derive(Clone, Debug, Default)]
struct QueueState {
    pressured_since: Option<Duration>,
    last_sample: Duration,
    observed_concurrency: u32,
    reported: bool,
}

#[derive(Clone, Debug, Default)]
struct TtftState {
    service: String,
    provider: String,
    recent: VecDeque<f64>,
    baseline_p95: Option<f64>,
    observed_concurrency: u32,
}

#[derive(Clone, Debug)]
pub struct PressureDetector {
    config: PressureConfig,
    max_wait: Duration,
    queues: HashMap<String, QueueState>,
    ttft: HashMap<String, TtftState>,
    pending: Vec<Degraded>,
}

impl PressureDetector {
    pub fn new(config: PressureConfig, max_wait: Duration) -> Self {
        Self {
            config,
            max_wait,
            queues: HashMap::new(),
            ttft: HashMap::new(),
            pending: Vec::new(),
        }
    }

    pub fn record_queue_wait(
        &mut self,
        service: &str,
        wait: Duration,
        observed_concurrency: u32,
        now: Duration,
    ) {
        let threshold = self.max_wait.mul_f64(self.config.queue_wait_fraction);
        let state = self.queues.entry(service.to_string()).or_default();
        state.last_sample = now;
        state.observed_concurrency = observed_concurrency;
        if wait > threshold {
            state.pressured_since.get_or_insert(now);
        } else {
            state.pressured_since = None;
            state.reported = false;
        }
    }

    pub fn record_ttft(
        &mut self,
        service: &str,
        provider: &str,
        endpoint: &str,
        ttft: Duration,
        observed_concurrency: u32,
    ) {
        let window = self.config.ttft_window.max(2);
        let factor = self.config.ttft_degrade_factor;
        let smoothing = self.config.ttft_baseline_smoothing;
        let state = self.ttft.entry(endpoint.to_string()).or_default();
        state.service = service.to_string();
        state.provider = provider.to_string();
        state.observed_concurrency = observed_concurrency;
        state.recent.push_back(ttft.as_secs_f64());
        if state.recent.len() < window {
            return;
        }
        let p95 = percentile(state.recent.iter().copied(), 0.95);
        state.recent.clear();
        match state.baseline_p95 {
            Some(baseline) if p95 > baseline * factor => {
                self.pending.push(Degraded {
                    service: state.service.clone(),
                    provider: state.provider.clone(),
                    reason: DegradedReason::TtftP95,
                    observed_concurrency: state.observed_concurrency,
                });
                state.baseline_p95 = Some(baseline + smoothing * (p95 - baseline));
            }
            Some(baseline) => {
                state.baseline_p95 = Some(baseline + smoothing * (p95 - baseline));
            }
            None => state.baseline_p95 = Some(p95),
        }
    }

    pub fn tick(&mut self, now: Duration) -> Vec<Degraded> {
        let sustained = self.config.sustained;
        let stale_after = self.config.stale_after;
        for (service, state) in &mut self.queues {
            if now.saturating_sub(state.last_sample) > stale_after {
                state.pressured_since = None;
                state.reported = false;
                continue;
            }
            let Some(since) = state.pressured_since else {
                continue;
            };
            if !state.reported && now.saturating_sub(since) >= sustained {
                state.reported = true;
                self.pending.push(Degraded {
                    service: service.clone(),
                    provider: String::new(),
                    reason: DegradedReason::QueueDepth,
                    observed_concurrency: state.observed_concurrency,
                });
            }
        }
        std::mem::take(&mut self.pending)
    }

    pub fn forget_except(&mut self, services: &[&str], endpoints: &[&str]) {
        self.queues
            .retain(|service, _| services.contains(&service.as_str()));
        self.ttft
            .retain(|endpoint, _| endpoints.contains(&endpoint.as_str()));
    }
}

fn percentile(samples: impl Iterator<Item = f64>, fraction: f64) -> f64 {
    let mut sorted: Vec<f64> = samples.collect();
    if sorted.is_empty() {
        return 0.0;
    }
    sorted.sort_by(|a, b| a.total_cmp(b));
    let rank = ((sorted.len() as f64) * fraction).ceil() as usize;
    sorted[rank.clamp(1, sorted.len()) - 1]
}

#[cfg(test)]
mod tests {
    use super::*;

    fn secs(s: u64) -> Duration {
        Duration::from_secs(s)
    }

    fn millis(ms: u64) -> Duration {
        Duration::from_millis(ms)
    }

    #[test]
    fn queue_pressure_reports_once_after_it_is_sustained() {
        let mut detector = PressureDetector::new(PressureConfig::default(), secs(5));
        detector.record_queue_wait("llama", millis(2600), 7, secs(0));
        assert!(detector.tick(secs(1)).is_empty());
        detector.record_queue_wait("llama", millis(3000), 8, secs(1));
        let reported = detector.tick(secs(2));
        assert_eq!(reported.len(), 1);
        assert_eq!(reported[0].service, "llama");
        assert_eq!(reported[0].reason, DegradedReason::QueueDepth);
        assert_eq!(reported[0].observed_concurrency, 8);
        detector.record_queue_wait("llama", millis(3000), 8, secs(2));
        assert!(detector.tick(secs(3)).is_empty());
        detector.record_queue_wait("llama", millis(100), 2, secs(3));
        assert!(detector.tick(secs(4)).is_empty());
        detector.record_queue_wait("llama", millis(4000), 9, secs(4));
        assert!(detector.tick(secs(5)).is_empty());
        assert_eq!(detector.tick(secs(6)).len(), 1);
    }

    #[test]
    fn stale_queue_pressure_clears_without_samples() {
        let mut detector = PressureDetector::new(PressureConfig::default(), secs(5));
        detector.record_queue_wait("llama", millis(4000), 1, secs(0));
        assert!(detector.tick(secs(11)).is_empty());
        assert!(detector.tick(secs(12)).is_empty());
    }

    #[test]
    fn ttft_p95_degradation_is_reported_against_the_baseline() {
        let config = PressureConfig {
            ttft_window: 10,
            ..PressureConfig::default()
        };
        let mut detector = PressureDetector::new(config, secs(5));
        for _ in 0..10 {
            detector.record_ttft("llama", "modal", "m1", millis(100), 3);
        }
        assert!(detector.tick(secs(0)).is_empty());
        for _ in 0..10 {
            detector.record_ttft("llama", "modal", "m1", millis(120), 3);
        }
        assert!(detector.tick(secs(1)).is_empty());
        for _ in 0..10 {
            detector.record_ttft("llama", "modal", "m1", millis(900), 6);
        }
        let reported = detector.tick(secs(2));
        assert_eq!(reported.len(), 1);
        assert_eq!(reported[0].provider, "modal");
        assert_eq!(reported[0].reason, DegradedReason::TtftP95);
        assert_eq!(reported[0].observed_concurrency, 6);
    }

    #[test]
    fn config_validation_names_the_offending_key() {
        assert!(PressureConfig::default().validate().is_ok());
        let stale = PressureConfig {
            stale_after: secs(1),
            ..PressureConfig::default()
        };
        assert!(stale.validate().unwrap_err().contains("stale_after"));
        let factor = PressureConfig {
            ttft_degrade_factor: 1.0,
            ..PressureConfig::default()
        };
        assert!(factor
            .validate()
            .unwrap_err()
            .contains("ttft_degrade_factor"));
        let window = PressureConfig {
            ttft_window: 1,
            ..PressureConfig::default()
        };
        assert!(window.validate().unwrap_err().contains("ttft_window"));
        let parsed: PressureConfig = serde_json::from_str(r#"{"sustained":0.5}"#).unwrap();
        assert_eq!(parsed.sustained, millis(500));
    }

    #[test]
    fn percentile_picks_the_nearest_rank() {
        let samples = [5.0, 1.0, 3.0, 2.0, 4.0];
        assert_eq!(percentile(samples.iter().copied(), 0.95), 5.0);
        assert_eq!(percentile(samples.iter().copied(), 0.5), 3.0);
        assert_eq!(percentile(std::iter::empty(), 0.95), 0.0);
    }

    #[test]
    fn forget_except_drops_unknown_services_and_endpoints() {
        let mut detector = PressureDetector::new(PressureConfig::default(), secs(5));
        detector.record_queue_wait("old", millis(1), 1, secs(0));
        detector.record_queue_wait("kept", millis(1), 1, secs(0));
        detector.record_ttft("kept", "p", "e-old", millis(1), 1);
        detector.record_ttft("kept", "p", "e-kept", millis(1), 1);
        detector.forget_except(&["kept"], &["e-kept"]);
        assert_eq!(detector.queues.len(), 1);
        assert_eq!(detector.ttft.len(), 1);
    }
}
