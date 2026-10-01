use router_core::circuit::CircuitConfig;
use serde::{Deserialize, Serialize};
use std::net::SocketAddr;
use std::time::Duration;

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct PhaseTimeouts {
    #[serde(with = "duration_secs")]
    pub connect: Duration,
    #[serde(with = "duration_secs")]
    pub first_byte: Duration,
    #[serde(with = "duration_secs")]
    pub idle: Duration,
    #[serde(with = "duration_secs")]
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
#[serde(default)]
pub struct ProxyConfig {
    pub listen: SocketAddr,
    pub timeouts: PhaseTimeouts,
    pub max_buffered_body_bytes: usize,
    pub region: Option<String>,
    pub circuit: CircuitConfig,
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
            circuit: CircuitConfig::default(),
        }
    }
}

mod duration_secs {
    use serde::{Deserialize, Deserializer, Serializer};
    use std::time::Duration;

    pub fn serialize<S: Serializer>(value: &Duration, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_f64(value.as_secs_f64())
    }

    pub fn deserialize<'de, D: Deserializer<'de>>(deserializer: D) -> Result<Duration, D::Error> {
        let secs = f64::deserialize(deserializer)?;
        if secs < 0.0 {
            return Err(serde::de::Error::custom("timeout must be non-negative"));
        }
        Ok(Duration::from_secs_f64(secs))
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
    fn timeouts_deserialize_from_seconds() {
        let parsed: PhaseTimeouts = serde_json::from_str(r#"{"connect":1.5,"total":120}"#).unwrap();
        assert_eq!(parsed.connect, Duration::from_millis(1500));
        assert_eq!(parsed.total, Duration::from_secs(120));
        assert_eq!(parsed.first_byte, Duration::from_secs(30));
        assert!(serde_json::from_str::<PhaseTimeouts>(r#"{"connect":-1}"#).is_err());
    }
}
