use anyhow::Context;
use router_proxy::PhaseTimeouts;
use serde::Deserialize;
use std::net::SocketAddr;
use std::path::{Path, PathBuf};

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    #[serde(default = "default_listen")]
    pub listen: SocketAddr,
    #[serde(default = "default_admin_listen")]
    pub admin_listen: SocketAddr,
    #[serde(default)]
    pub region: Option<String>,
    #[serde(default = "default_node_id")]
    pub node_id: String,
    pub snapshot: SnapshotConfig,
    #[serde(default)]
    pub tls: Option<TlsConfig>,
    #[serde(default)]
    pub timeouts: PhaseTimeouts,
    #[serde(default = "default_max_body")]
    pub max_buffered_body_bytes: usize,
    #[serde(default)]
    pub log: LogConfig,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SnapshotConfig {
    pub source: String,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TlsConfig {
    pub cert: PathBuf,
    pub key: PathBuf,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LogConfig {
    #[serde(default)]
    pub format: LogFormat,
    #[serde(default = "default_log_filter")]
    pub filter: String,
}

#[derive(Clone, Copy, Debug, Default, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum LogFormat {
    #[default]
    Text,
    Json,
}

fn default_listen() -> SocketAddr {
    "0.0.0.0:8080".parse().expect("valid address")
}

fn default_admin_listen() -> SocketAddr {
    "127.0.0.1:9090".parse().expect("valid address")
}

fn default_node_id() -> String {
    std::env::var("HOSTNAME").unwrap_or_else(|_| "multihull-router".to_string())
}

fn default_max_body() -> usize {
    1024 * 1024
}

fn default_log_filter() -> String {
    "info".to_string()
}

impl Config {
    pub fn load(path: &Path) -> anyhow::Result<Self> {
        let text = std::fs::read_to_string(path)
            .with_context(|| format!("reading config {}", path.display()))?;
        Self::parse(&text).with_context(|| format!("parsing config {}", path.display()))
    }

    pub fn parse(text: &str) -> anyhow::Result<Self> {
        Ok(toml::from_str(text)?)
    }

    pub fn proxy_config(&self) -> router_proxy::ProxyConfig {
        router_proxy::ProxyConfig {
            listen: self.listen,
            timeouts: self.timeouts.clone(),
            max_buffered_body_bytes: self.max_buffered_body_bytes,
            region: self.region.clone(),
            circuit: router_core::circuit::CircuitConfig::default(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    #[test]
    fn example_config_parses() {
        let text = include_str!("../../../router.example.toml");
        let config = Config::parse(text).unwrap();
        assert_eq!(config.listen.port(), 8080);
        assert_eq!(config.admin_listen.port(), 9090);
        assert_eq!(config.snapshot.source, "file://./snapshot.json");
        assert_eq!(config.region.as_deref(), Some("eu"));
        assert_eq!(config.timeouts.connect, Duration::from_secs(2));
        assert_eq!(config.log.format, LogFormat::Json);
        assert!(config.tls.is_some());
    }

    #[test]
    fn minimal_config_uses_defaults() {
        let config = Config::parse("[snapshot]\nsource = \"grpc://controller:7777\"\n").unwrap();
        assert_eq!(config.listen, default_listen());
        assert_eq!(config.timeouts, PhaseTimeouts::default());
        assert_eq!(config.max_buffered_body_bytes, 1024 * 1024);
        assert!(config.tls.is_none());
    }

    #[test]
    fn unknown_keys_are_rejected() {
        assert!(Config::parse("[snapshot]\nsource = \"x\"\nbogus = 1\n").is_err());
        assert!(Config::parse("listen = \"0.0.0.0:1\"\n").is_err());
    }
}
