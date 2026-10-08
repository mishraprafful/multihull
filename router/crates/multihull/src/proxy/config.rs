use crate::core::circuit::CircuitConfig;
use crate::core::limit::AdmissionQueue;
use crate::core::pressure::PressureConfig;
use crate::core::probe::ProbeConfig;
use crate::core::retry::RetryConfig;
use serde::{Deserialize, Serialize};
use std::net::SocketAddr;
use std::path::PathBuf;
use std::time::Duration;

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct PhaseTimeouts {
    #[serde(with = "crate::core::serde_secs")]
    pub connect: Duration,
    #[serde(with = "crate::core::serde_secs")]
    pub first_byte: Duration,
    #[serde(with = "crate::core::serde_secs")]
    pub idle: Duration,
    #[serde(with = "crate::core::serde_secs")]
    pub total: Duration,
}

impl Default for PhaseTimeouts {
    fn default() -> Self {
        Self {
            connect: Duration::from_secs(2),
            first_byte: Duration::from_secs(30),
            idle: Duration::from_secs(60),
            total: Duration::from_secs(600),
        }
    }
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct ProxyConfig {
    pub listen: SocketAddr,
    pub timeouts: PhaseTimeouts,
    pub max_buffered_body_bytes: usize,
    pub region: Option<String>,
    pub upstream_ca: Option<PathBuf>,
    pub circuit: CircuitConfig,
    pub admission: AdmissionQueue,
    pub pressure: PressureConfig,
    pub probe: ProbeConfig,
    pub retry: RetryConfig,
}

impl ProxyConfig {
    pub fn validate(&self) -> Result<(), String> {
        self.timeouts.validate()?;
        self.circuit.validate()?;
        self.admission.validate()?;
        self.pressure.validate()?;
        self.probe.validate()?;
        self.retry.validate()
    }
}

impl PhaseTimeouts {
    pub fn validate(&self) -> Result<(), String> {
        if self.connect.is_zero() {
            return Err("timeouts.connect must be positive".into());
        }
        if self.first_byte.is_zero() {
            return Err("timeouts.first_byte must be positive".into());
        }
        if self.idle.is_zero() {
            return Err("timeouts.idle must be positive".into());
        }
        if self.total < self.first_byte {
            return Err("timeouts.total must be at least timeouts.first_byte".into());
        }
        Ok(())
    }
}

impl Default for ProxyConfig {
    fn default() -> Self {
        Self {
            listen: "0.0.0.0:8080"
                .parse()
                .expect("valid default listen address"),
            timeouts: PhaseTimeouts::default(),
            max_buffered_body_bytes: 1024 * 1024,
            region: None,
            upstream_ca: None,
            circuit: CircuitConfig::default(),
            admission: AdmissionQueue::default(),
            pressure: PressureConfig::default(),
            probe: ProbeConfig::default(),
            retry: RetryConfig::default(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_match_plan_phase_timeouts() {
        let timeouts = PhaseTimeouts::default();
        assert_eq!(timeouts.connect, Duration::from_secs(2));
        assert_eq!(timeouts.first_byte, Duration::from_secs(30));
        assert_eq!(timeouts.idle, Duration::from_secs(60));
        assert_eq!(timeouts.total, Duration::from_secs(600));
    }

    #[test]
    fn default_proxy_config_validates_and_reports_bad_tables() {
        assert!(ProxyConfig::default().validate().is_ok());
        let bad_timeouts = ProxyConfig {
            timeouts: PhaseTimeouts {
                total: Duration::from_secs(1),
                ..PhaseTimeouts::default()
            },
            ..ProxyConfig::default()
        };
        assert!(bad_timeouts
            .validate()
            .unwrap_err()
            .contains("timeouts.total"));
        let bad_retry = ProxyConfig {
            retry: RetryConfig {
                budget_ratio: 2.0,
                ..RetryConfig::default()
            },
            ..ProxyConfig::default()
        };
        assert!(bad_retry.validate().unwrap_err().contains("retry."));
        let json = serde_json::to_value(ProxyConfig::default()).unwrap();
        assert_eq!(json["circuit"]["consecutive_failures"], 5);
        assert_eq!(json["admission"]["max_wait"], 5.0);
        assert_eq!(json["pressure"]["sustained"], 2.0);
        assert_eq!(json["probe"]["interval"], 5.0);
        assert_eq!(json["retry"]["budget_ratio"], 0.2);
    }

    #[test]
    fn timeouts_deserialize_from_seconds() {
        let parsed: PhaseTimeouts = serde_json::from_str(r#"{"connect":1.5,"total":120}"#).unwrap();
        assert_eq!(parsed.connect, Duration::from_millis(1500));
        assert_eq!(parsed.total, Duration::from_secs(120));
        assert_eq!(parsed.first_byte, Duration::from_secs(30));
        assert!(serde_json::from_str::<PhaseTimeouts>(r#"{"connect":-1}"#).is_err());
    }
}
