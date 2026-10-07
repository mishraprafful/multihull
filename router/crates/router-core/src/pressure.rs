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
    pub resend_every: Duration,
    #[serde(with = "crate::serde_secs")]
    pub stale_after: Duration,
    pub ttft_degrade_factor: f64,
    pub ttft_window: usize,
    pub ttft_baseline_smoothing: f64,
    #[serde(with = "crate::serde_secs")]
    pub ttft_rebaseline_after: Duration,
}

impl PressureConfig {
    pub fn validate(&self) -> Result<(), String> {
        if !(self.queue_wait_fraction > 0.0 && self.queue_wait_fraction <= 1.0) {
            return Err("pressure.queue_wait_fraction must be in (0, 1]".into());
        }
        if self.sustained.is_zero() {
            return Err("pressure.sustained must be positive".into());
        }
        if self.resend_every.is_zero() {
            return Err("pressure.resend_every must be positive".into());
        }
        if self.stale_after <= self.sustained {
            return Err("pressure.stale_after must exceed pressure.sustained".into());
        }
        if !(self.ttft_degrade_factor.is_finite() && self.ttft_degrade_factor > 1.0) {
            return Err("pressure.ttft_degrade_factor must be finite and greater than 1".into());
        }
        if self.ttft_window < 2 {
            return Err("pressure.ttft_window must be at least 2".into());
        }
        if !(self.ttft_baseline_smoothing > 0.0 && self.ttft_baseline_smoothing <= 1.0) {
            return Err("pressure.ttft_baseline_smoothing must be in (0, 1]".into());
        }
        if self.ttft_rebaseline_after.is_zero() {
            return Err("pressure.ttft_rebaseline_after must be positive".into());
        }
        Ok(())
    }
}

impl Default for PressureConfig {
    fn default() -> Self {
        Self {
            queue_wait_fraction: 0.5,
            sustained: Duration::from_secs(2),
            resend_every: Duration::from_secs(30),
            stale_after: Duration::from_secs(10),
            ttft_degrade_factor: 2.0,
            ttft_window: 50,
            ttft_baseline_smoothing: 0.2,
            ttft_rebaseline_after: Duration::from_secs(3600),
        }
    }
}

#[derive(Clone, Debug, Default)]
struct QueueState {
    pressured_since: Option<Duration>,
    last_sample: Duration,
    observed_concurrency: u32,
    reported_at: Option<Duration>,
}

impl QueueState {
    fn report_due(&self, since: Duration, now: Duration, config: &PressureConfig) -> bool {
        match self.reported_at {
            None => now.saturating_sub(since) >= config.sustained,
            Some(at) => self.last_sample > at && now.saturating_sub(at) >= config.resend_every,
        }
    }
}

#[derive(Clone, Debug, Default)]
struct TtftState {
    service: String,
    provider: String,
    recent: VecDeque<f64>,
    baseline_p95: Option<f64>,
    observed_concurrency: u32,
    last_sample: Duration,
    degraded_since: Option<Duration>,
    reported_at: Option<Duration>,
}

impl TtftState {
    fn report_due(&self, now: Duration, config: &PressureConfig) -> bool {
        match self.reported_at {
            None => self.degraded_since.is_some(),
            Some(at) => self.last_sample > at && now.saturating_sub(at) >= config.resend_every,
        }
    }

    fn recover(&mut self) {
        self.degraded_since = None;
        self.reported_at = None;
    }
}

#[derive(Clone, Debug)]
pub struct PressureDetector {
    config: PressureConfig,
    max_wait: Duration,
    queues: HashMap<String, QueueState>,
    ttft: HashMap<String, TtftState>,
}

impl PressureDetector {
    pub fn new(config: PressureConfig, max_wait: Duration) -> Self {
        Self {
            config,
            max_wait,
            queues: HashMap::new(),
            ttft: HashMap::new(),
        }
    }

    pub fn record_queue_wait(
        &mut self,
        service: &str,
        wait: Duration,
        still_queued: usize,
        observed_concurrency: u32,
        now: Duration,
    ) {
        let threshold = self.max_wait.mul_f64(self.config.queue_wait_fraction);
        let state = self.queues.entry(service.to_string()).or_default();
        state.last_sample = now;
        state.observed_concurrency = observed_concurrency;
        if wait > threshold {
            state.pressured_since.get_or_insert(now);
        } else if still_queued == 0 {
            state.pressured_since = None;
            state.reported_at = None;
        }
    }

    pub fn record_ttft(
        &mut self,
        service: &str,
        provider: &str,
        endpoint: &str,
        ttft: Duration,
        observed_concurrency: u32,
        now: Duration,
    ) {
        let window = self.config.ttft_window.max(2);
        let factor = self.config.ttft_degrade_factor;
        let smoothing = self.config.ttft_baseline_smoothing;
        let rebaseline_after = self.config.ttft_rebaseline_after;
        let state = self.ttft.entry(endpoint.to_string()).or_default();
        state.service = service.to_string();
        state.provider = provider.to_string();
        state.observed_concurrency = observed_concurrency;
        state.last_sample = now;
        state.recent.push_back(ttft.as_secs_f64());
        if state.recent.len() < window {
            return;
        }
        let p95 = percentile(state.recent.iter().copied(), 0.95);
        state.recent.clear();
        match state.baseline_p95 {
            Some(baseline) if p95 > baseline * factor => {
                let since = *state.degraded_since.get_or_insert(now);
                if now.saturating_sub(since) >= rebaseline_after {
                    state.baseline_p95 = Some(p95);
                    state.recover();
                }
            }
            Some(baseline) => {
                state.baseline_p95 = Some(baseline + smoothing * (p95 - baseline));
                state.recover();
            }
            None => state.baseline_p95 = Some(p95),
        }
    }

    pub fn tick(&mut self, now: Duration) -> Vec<Degraded> {
        let mut reports = Vec::new();
        for (service, state) in &mut self.queues {
            if now.saturating_sub(state.last_sample) > self.config.stale_after {
                state.pressured_since = None;
                state.reported_at = None;
                continue;
            }
            let Some(since) = state.pressured_since else {
                continue;
            };
            if state.report_due(since, now, &self.config) {
                state.reported_at = Some(now);
                reports.push(Degraded {
                    service: service.clone(),
                    provider: String::new(),
                    reason: DegradedReason::QueueDepth,
                    observed_concurrency: state.observed_concurrency,
                });
            }
        }
        for state in self.ttft.values_mut() {
            if state.report_due(now, &self.config) {
                state.reported_at = Some(now);
                reports.push(Degraded {
                    service: state.service.clone(),
                    provider: state.provider.clone(),
                    reason: DegradedReason::TtftP95,
                    observed_concurrency: state.observed_concurrency,
                });
            }
        }
        reports
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
        detector.record_queue_wait("llama", millis(2600), 4, 7, secs(0));
        assert!(detector.tick(secs(1)).is_empty());
        detector.record_queue_wait("llama", millis(3000), 4, 8, secs(1));
        let reported = detector.tick(secs(2));
        assert_eq!(reported.len(), 1);
        assert_eq!(reported[0].service, "llama");
        assert_eq!(reported[0].reason, DegradedReason::QueueDepth);
        assert_eq!(reported[0].observed_concurrency, 8);
        detector.record_queue_wait("llama", millis(3000), 4, 8, secs(2));
        assert!(detector.tick(secs(3)).is_empty());
        detector.record_queue_wait("llama", millis(100), 0, 2, secs(3));
        assert!(detector.tick(secs(4)).is_empty());
        detector.record_queue_wait("llama", millis(4000), 4, 9, secs(4));
        assert!(detector.tick(secs(5)).is_empty());
        assert_eq!(detector.tick(secs(6)).len(), 1);
    }

    #[test]
    fn short_waits_keep_queue_pressure_until_the_queue_drains() {
        let mut detector = PressureDetector::new(PressureConfig::default(), secs(5));
        detector.record_queue_wait("llama", millis(3100), 6, 3, secs(0));
        detector.record_queue_wait("llama", millis(1500), 6, 3, millis(1500));
        detector.record_queue_wait("llama", millis(1500), 5, 3, millis(1500));
        assert!(detector.tick(millis(1600)).is_empty());
        let reported = detector.tick(secs(2));
        assert_eq!(reported.len(), 1);
        assert_eq!(reported[0].reason, DegradedReason::QueueDepth);

        detector.record_queue_wait("llama", millis(1500), 0, 1, secs(3));
        detector.record_queue_wait("llama", millis(3000), 2, 3, secs(4));
        assert!(detector.tick(millis(5500)).is_empty());
        assert_eq!(detector.tick(secs(6)).len(), 1);
    }

    #[test]
    fn queue_pressure_is_resent_while_new_samples_confirm_it() {
        let config = PressureConfig {
            resend_every: secs(3),
            ..PressureConfig::default()
        };
        let mut detector = PressureDetector::new(config, secs(5));
        detector.record_queue_wait("llama", millis(3000), 4, 7, secs(0));
        assert_eq!(detector.tick(secs(2)).len(), 1);
        detector.record_queue_wait("llama", millis(3000), 4, 8, secs(3));
        assert!(detector.tick(secs(4)).is_empty());
        let resent = detector.tick(secs(5));
        assert_eq!(resent.len(), 1);
        assert_eq!(resent[0].reason, DegradedReason::QueueDepth);
        assert_eq!(resent[0].observed_concurrency, 8);
        assert!(detector.tick(secs(9)).is_empty());
        detector.record_queue_wait("llama", millis(1000), 2, 8, secs(9));
        assert_eq!(detector.tick(secs(9)).len(), 1);
        detector.record_queue_wait("llama", millis(100), 0, 1, secs(10));
        detector.record_queue_wait("llama", millis(100), 0, 1, secs(13));
        assert!(detector.tick(secs(13)).is_empty());
    }

    #[test]
    fn stale_queue_pressure_clears_without_samples() {
        let mut detector = PressureDetector::new(PressureConfig::default(), secs(5));
        detector.record_queue_wait("llama", millis(4000), 0, 1, secs(0));
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
            detector.record_ttft("llama", "modal", "m1", millis(100), 3, secs(0));
        }
        assert!(detector.tick(secs(0)).is_empty());
        for _ in 0..10 {
            detector.record_ttft("llama", "modal", "m1", millis(120), 3, secs(1));
        }
        assert!(detector.tick(secs(1)).is_empty());
        for _ in 0..10 {
            detector.record_ttft("llama", "modal", "m1", millis(900), 6, secs(2));
        }
        let reported = detector.tick(secs(2));
        assert_eq!(reported.len(), 1);
        assert_eq!(reported[0].provider, "modal");
        assert_eq!(reported[0].reason, DegradedReason::TtftP95);
        assert_eq!(reported[0].observed_concurrency, 6);
    }

    fn ttft_config() -> PressureConfig {
        PressureConfig {
            ttft_window: 10,
            resend_every: secs(3),
            ..PressureConfig::default()
        }
    }

    fn record_window(detector: &mut PressureDetector, ttft: Duration, now: Duration) {
        for _ in 0..10 {
            detector.record_ttft("llama", "modal", "m1", ttft, 3, now);
        }
    }

    fn ttft_reports(detector: &mut PressureDetector, ttft: Duration, seconds: &[u64]) -> Vec<u64> {
        let mut reported = Vec::new();
        for &second in seconds {
            record_window(detector, ttft, secs(second));
            let signals = detector.tick(secs(second));
            assert!(signals.len() <= 1, "{signals:?}");
            for signal in signals {
                assert_eq!(signal.provider, "modal");
                assert_eq!(signal.reason, DegradedReason::TtftP95);
                reported.push(second);
            }
        }
        reported
    }

    fn range(from: u64, to: u64) -> Vec<u64> {
        (from..=to).collect()
    }

    #[test]
    fn a_sustained_ttft_slowdown_is_resent_while_it_lasts() {
        let mut detector = PressureDetector::new(ttft_config(), secs(5));
        assert!(ttft_reports(&mut detector, millis(100), &[0]).is_empty());
        assert_eq!(
            ttft_reports(&mut detector, millis(300), &range(1, 30)),
            vec![1, 4, 7, 10, 13, 16, 19, 22, 25, 28]
        );
    }

    #[test]
    fn ttft_reports_stop_once_windows_are_healthy_again() {
        let mut detector = PressureDetector::new(ttft_config(), secs(5));
        assert!(ttft_reports(&mut detector, millis(100), &[0]).is_empty());
        assert_eq!(
            ttft_reports(&mut detector, millis(300), &range(1, 9)),
            vec![1, 4, 7]
        );
        assert!(ttft_reports(&mut detector, millis(110), &range(10, 30)).is_empty());
        assert_eq!(ttft_reports(&mut detector, millis(300), &[31]), vec![31]);
    }

    #[test]
    fn a_ttft_slowdown_that_outlasts_rebaseline_after_becomes_the_baseline() {
        let config = PressureConfig {
            ttft_rebaseline_after: secs(10),
            ..ttft_config()
        };
        let mut detector = PressureDetector::new(config, secs(5));
        assert!(ttft_reports(&mut detector, millis(100), &[0]).is_empty());
        assert_eq!(
            ttft_reports(&mut detector, millis(300), &range(1, 30)),
            vec![1, 4, 7, 10]
        );
        assert!(ttft_reports(&mut detector, millis(500), &[31]).is_empty());
        assert_eq!(ttft_reports(&mut detector, millis(800), &[32]), vec![32]);
    }

    #[test]
    fn config_validation_names_the_offending_key() {
        assert!(PressureConfig::default().validate().is_ok());
        let stale = PressureConfig {
            stale_after: secs(1),
            ..PressureConfig::default()
        };
        assert!(stale.validate().unwrap_err().contains("stale_after"));
        for ttft_degrade_factor in [1.0, f64::NAN, f64::INFINITY] {
            let factor = PressureConfig {
                ttft_degrade_factor,
                ..PressureConfig::default()
            };
            assert!(
                factor
                    .validate()
                    .unwrap_err()
                    .contains("ttft_degrade_factor"),
                "{ttft_degrade_factor}"
            );
        }
        let window = PressureConfig {
            ttft_window: 1,
            ..PressureConfig::default()
        };
        assert!(window.validate().unwrap_err().contains("ttft_window"));
        let resend = PressureConfig {
            resend_every: Duration::ZERO,
            ..PressureConfig::default()
        };
        assert!(resend.validate().unwrap_err().contains("resend_every"));
        let rebaseline = PressureConfig {
            ttft_rebaseline_after: Duration::ZERO,
            ..PressureConfig::default()
        };
        assert!(rebaseline
            .validate()
            .unwrap_err()
            .contains("ttft_rebaseline_after"));
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
        detector.record_queue_wait("old", millis(1), 0, 1, secs(0));
        detector.record_queue_wait("kept", millis(1), 0, 1, secs(0));
        detector.record_ttft("kept", "p", "e-old", millis(1), 1, secs(0));
        detector.record_ttft("kept", "p", "e-kept", millis(1), 1, secs(0));
        detector.forget_except(&["kept"], &["e-kept"]);
        assert_eq!(detector.queues.len(), 1);
        assert_eq!(detector.ttft.len(), 1);
    }
}
