use crate::rng::Rng;
use crate::snapshot::EndpointType;
use serde::{Deserialize, Serialize};
use std::time::Duration;

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct ProbeConfig {
    pub enabled: bool,
    #[serde(with = "crate::serde_secs")]
    pub interval: Duration,
    #[serde(with = "crate::serde_secs")]
    pub timeout: Duration,
    pub jitter_fraction: f64,
    pub failure_threshold: u32,
    pub success_threshold: u32,
}

impl Default for ProbeConfig {
    fn default() -> Self {
        Self {
            enabled: true,
            interval: Duration::from_secs(5),
            timeout: Duration::from_secs(2),
            jitter_fraction: 0.2,
            failure_threshold: 3,
            success_threshold: 3,
        }
    }
}

impl ProbeConfig {
    pub fn next_delay(&self, rng: &mut impl Rng) -> Duration {
        let spread = self.jitter_fraction * (2.0 * rng.next_f64() - 1.0);
        self.interval.mul_f64((1.0 + spread).max(0.0))
    }

    pub fn validate(&self) -> Result<(), String> {
        if self.interval.is_zero() {
            return Err("probe.interval must be positive".into());
        }
        if self.timeout.is_zero() {
            return Err("probe.timeout must be positive".into());
        }
        if !(0.0..1.0).contains(&self.jitter_fraction) {
            return Err("probe.jitter_fraction must be in [0, 1)".into());
        }
        if self.failure_threshold == 0 {
            return Err("probe.failure_threshold must be at least 1".into());
        }
        if self.success_threshold == 0 {
            return Err("probe.success_threshold must be at least 1".into());
        }
        Ok(())
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ProbeOutcome {
    Success,
    Warming,
    Failure,
}

impl ProbeOutcome {
    pub fn label(&self) -> &'static str {
        match self {
            ProbeOutcome::Success => "success",
            ProbeOutcome::Warming => "warming",
            ProbeOutcome::Failure => "failure",
        }
    }
}

pub fn classify_probe(kind: EndpointType, status: Option<u16>) -> ProbeOutcome {
    match (kind, status) {
        (EndpointType::Runpod, Some(204)) => ProbeOutcome::Warming,
        (_, Some(429)) => ProbeOutcome::Warming,
        (_, Some(200..=299)) => ProbeOutcome::Success,
        _ => ProbeOutcome::Failure,
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ProbeTransition {
    Unchanged,
    WentDown,
    CameUp,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ProbeState {
    Unknown,
    Up,
    Down,
}

impl ProbeState {
    pub fn label(&self) -> &'static str {
        match self {
            ProbeState::Unknown => "unknown",
            ProbeState::Up => "up",
            ProbeState::Down => "down",
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ProbeTracker {
    failure_threshold: u32,
    success_threshold: u32,
    state: ProbeState,
    consecutive_failures: u32,
    consecutive_successes: u32,
    last_outcome: Option<ProbeOutcome>,
    last_status: Option<u16>,
    probes: u64,
}

impl ProbeTracker {
    pub fn new(config: &ProbeConfig) -> Self {
        Self {
            failure_threshold: config.failure_threshold.max(1),
            success_threshold: config.success_threshold.max(1),
            state: ProbeState::Unknown,
            consecutive_failures: 0,
            consecutive_successes: 0,
            last_outcome: None,
            last_status: None,
            probes: 0,
        }
    }

    pub fn record(&mut self, outcome: ProbeOutcome, status: Option<u16>) -> ProbeTransition {
        self.probes += 1;
        self.last_outcome = Some(outcome);
        self.last_status = status;
        match outcome {
            ProbeOutcome::Warming => ProbeTransition::Unchanged,
            ProbeOutcome::Failure => {
                self.consecutive_successes = 0;
                self.consecutive_failures = self.consecutive_failures.saturating_add(1);
                if self.consecutive_failures >= self.failure_threshold
                    && self.state != ProbeState::Down
                {
                    self.state = ProbeState::Down;
                    return ProbeTransition::WentDown;
                }
                ProbeTransition::Unchanged
            }
            ProbeOutcome::Success => {
                self.consecutive_failures = 0;
                self.consecutive_successes = self.consecutive_successes.saturating_add(1);
                if self.consecutive_successes >= self.success_threshold
                    && self.state != ProbeState::Up
                {
                    self.state = ProbeState::Up;
                    return ProbeTransition::CameUp;
                }
                ProbeTransition::Unchanged
            }
        }
    }

    pub fn state(&self) -> ProbeState {
        self.state
    }

    pub fn is_down(&self) -> bool {
        self.state == ProbeState::Down
    }

    pub fn consecutive_failures(&self) -> u32 {
        self.consecutive_failures
    }

    pub fn consecutive_successes(&self) -> u32 {
        self.consecutive_successes
    }

    pub fn last_outcome(&self) -> Option<ProbeOutcome> {
        self.last_outcome
    }

    pub fn last_status(&self) -> Option<u16> {
        self.last_status
    }

    pub fn probes(&self) -> u64 {
        self.probes
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::rng::ZeroRng;

    fn tracker() -> ProbeTracker {
        ProbeTracker::new(&ProbeConfig::default())
    }

    #[test]
    fn three_failures_mark_down_once_and_three_successes_mark_up_once() {
        let mut tracker = tracker();
        assert_eq!(tracker.state(), ProbeState::Unknown);
        assert_eq!(
            tracker.record(ProbeOutcome::Failure, Some(503)),
            ProbeTransition::Unchanged
        );
        assert_eq!(
            tracker.record(ProbeOutcome::Failure, None),
            ProbeTransition::Unchanged
        );
        assert_eq!(
            tracker.record(ProbeOutcome::Failure, Some(503)),
            ProbeTransition::WentDown
        );
        assert!(tracker.is_down());
        assert_eq!(
            tracker.record(ProbeOutcome::Failure, Some(503)),
            ProbeTransition::Unchanged
        );
        assert_eq!(tracker.consecutive_failures(), 4);
        for _ in 0..2 {
            assert_eq!(
                tracker.record(ProbeOutcome::Success, Some(200)),
                ProbeTransition::Unchanged
            );
            assert!(tracker.is_down());
        }
        assert_eq!(
            tracker.record(ProbeOutcome::Success, Some(200)),
            ProbeTransition::CameUp
        );
        assert_eq!(tracker.state(), ProbeState::Up);
        assert_eq!(tracker.consecutive_failures(), 0);
        assert_eq!(tracker.consecutive_successes(), 3);
        assert_eq!(tracker.probes(), 7);
        assert_eq!(tracker.last_status(), Some(200));
    }

    #[test]
    fn warming_counts_as_neither_success_nor_failure() {
        let mut tracker = tracker();
        tracker.record(ProbeOutcome::Failure, Some(503));
        tracker.record(ProbeOutcome::Failure, Some(503));
        assert_eq!(
            tracker.record(ProbeOutcome::Warming, Some(204)),
            ProbeTransition::Unchanged
        );
        assert_eq!(tracker.consecutive_failures(), 2);
        assert_eq!(tracker.last_outcome(), Some(ProbeOutcome::Warming));
        assert_eq!(
            tracker.record(ProbeOutcome::Failure, Some(503)),
            ProbeTransition::WentDown
        );
    }

    #[test]
    fn a_success_resets_the_failure_streak() {
        let mut tracker = tracker();
        tracker.record(ProbeOutcome::Failure, Some(503));
        tracker.record(ProbeOutcome::Failure, Some(503));
        tracker.record(ProbeOutcome::Success, Some(200));
        tracker.record(ProbeOutcome::Failure, Some(503));
        tracker.record(ProbeOutcome::Failure, Some(503));
        assert!(!tracker.is_down());
    }

    #[test]
    fn runpod_204_is_warming_and_other_providers_treat_2xx_as_success() {
        assert_eq!(
            classify_probe(EndpointType::Runpod, Some(204)),
            ProbeOutcome::Warming
        );
        assert_eq!(
            classify_probe(EndpointType::Kubernetes, Some(204)),
            ProbeOutcome::Success
        );
        assert_eq!(
            classify_probe(EndpointType::Docker, Some(200)),
            ProbeOutcome::Success
        );
        assert_eq!(
            classify_probe(EndpointType::Modal, Some(429)),
            ProbeOutcome::Warming
        );
        assert_eq!(
            classify_probe(EndpointType::Docker, Some(503)),
            ProbeOutcome::Failure
        );
        assert_eq!(
            classify_probe(EndpointType::Docker, Some(404)),
            ProbeOutcome::Failure
        );
        assert_eq!(
            classify_probe(EndpointType::Docker, None),
            ProbeOutcome::Failure
        );
    }

    #[test]
    fn delay_jitters_around_the_interval() {
        let config = ProbeConfig::default();
        assert_eq!(config.next_delay(&mut ZeroRng), Duration::from_millis(4000));
        let mut mid = || 0.5;
        assert_eq!(config.next_delay(&mut mid), Duration::from_secs(5));
        let mut high = || 1.0;
        assert_eq!(config.next_delay(&mut high), Duration::from_secs(6));
    }

    #[test]
    fn validation_rejects_zero_durations_and_thresholds() {
        assert!(ProbeConfig::default().validate().is_ok());
        let zero_interval = ProbeConfig {
            interval: Duration::ZERO,
            ..ProbeConfig::default()
        };
        assert!(zero_interval.validate().unwrap_err().contains("interval"));
        let zero_timeout = ProbeConfig {
            timeout: Duration::ZERO,
            ..ProbeConfig::default()
        };
        assert!(zero_timeout.validate().unwrap_err().contains("timeout"));
        let bad_jitter = ProbeConfig {
            jitter_fraction: 1.0,
            ..ProbeConfig::default()
        };
        assert!(bad_jitter.validate().unwrap_err().contains("jitter"));
        let no_failures = ProbeConfig {
            failure_threshold: 0,
            ..ProbeConfig::default()
        };
        assert!(no_failures.validate().is_err());
        let no_successes = ProbeConfig {
            success_threshold: 0,
            ..ProbeConfig::default()
        };
        assert!(no_successes.validate().is_err());
    }
}
