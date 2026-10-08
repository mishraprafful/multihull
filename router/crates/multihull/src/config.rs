use anyhow::Context;
use router_core::circuit::CircuitConfig;
use router_core::limit::AdmissionQueue;
use router_core::pressure::PressureConfig;
use router_core::probe::ProbeConfig;
use router_core::retry::RetryConfig;
use router_cp::{SnapshotSource, SourceSecurity};
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
    pub upstream_ca: Option<PathBuf>,
    #[serde(default)]
    pub timeouts: PhaseTimeouts,
    #[serde(default = "default_max_body")]
    pub max_buffered_body_bytes: usize,
    #[serde(default)]
    pub log: LogConfig,
    #[serde(default)]
    pub circuit: CircuitConfig,
    #[serde(default)]
    pub admission: AdmissionQueue,
    #[serde(default)]
    pub pressure: PressureConfig,
    #[serde(default)]
    pub probe: ProbeConfig,
    #[serde(default)]
    pub retry: RetryConfig,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SnapshotConfig {
    pub source: String,
    #[serde(default)]
    pub ca: Option<PathBuf>,
    #[serde(default)]
    pub client_cert: Option<PathBuf>,
    #[serde(default)]
    pub client_key: Option<PathBuf>,
    #[serde(default)]
    pub token_env: Option<String>,
    #[serde(default)]
    pub insecure: bool,
}

impl SnapshotConfig {
    pub fn parsed_source(&self) -> anyhow::Result<SnapshotSource> {
        Ok(SnapshotSource::parse(&self.source)?)
    }

    pub fn security(&self) -> SourceSecurity {
        SourceSecurity {
            ca: self.ca.clone(),
            client_cert: self.client_cert.clone(),
            client_key: self.client_key.clone(),
            token_env: self.token_env.clone(),
            insecure: self.insecure,
        }
    }
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
        let config: Self = toml::from_str(text)?;
        config.validate()?;
        Ok(config)
    }

    fn validate(&self) -> anyhow::Result<()> {
        let source = self.snapshot.parsed_source().context("[snapshot] source")?;
        self.snapshot
            .security()
            .check(&source)
            .context("[snapshot]")?;
        self.proxy_config()
            .validate()
            .map_err(|message| anyhow::anyhow!("invalid tuning: {message}"))
    }

    pub fn proxy_config(&self) -> router_proxy::ProxyConfig {
        router_proxy::ProxyConfig {
            listen: self.listen,
            timeouts: self.timeouts.clone(),
            max_buffered_body_bytes: self.max_buffered_body_bytes,
            region: self.region.clone(),
            upstream_ca: self.upstream_ca.clone(),
            circuit: self.circuit.clone(),
            admission: self.admission.clone(),
            pressure: self.pressure.clone(),
            probe: self.probe.clone(),
            retry: self.retry.clone(),
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
        assert!(config.upstream_ca.is_none());
        assert_eq!(config.proxy_config(), documented_defaults());
    }

    fn documented_defaults() -> router_proxy::ProxyConfig {
        router_proxy::ProxyConfig {
            listen: default_listen(),
            region: Some("eu".to_string()),
            ..router_proxy::ProxyConfig::default()
        }
    }

    #[test]
    fn tuning_tables_override_defaults_and_are_validated() {
        let text = "[snapshot]\nsource = \"x\"\n\n[circuit]\nconsecutive_failures = 3\nbase_backoff = 1.5\n\n[admission]\nmax_wait = 2\nbound = 64\n\n[pressure]\nsustained = 1\nstale_after = 4\n\n[retry]\nbudget_ratio = 0.1\nbudget_window = 20\n";
        let config = Config::parse(text).unwrap();
        let proxy = config.proxy_config();
        assert_eq!(proxy.circuit.consecutive_failures, 3);
        assert_eq!(proxy.circuit.base_backoff, Duration::from_millis(1500));
        assert_eq!(proxy.circuit.error_ratio, 0.5);
        assert_eq!(proxy.admission.max_wait, Duration::from_secs(2));
        assert_eq!(proxy.admission.bound, 64);
        assert_eq!(proxy.pressure.sustained, Duration::from_secs(1));
        assert_eq!(proxy.pressure.stale_after, Duration::from_secs(4));
        assert_eq!(proxy.retry.budget_ratio, 0.1);
        assert_eq!(proxy.retry.budget_window, Duration::from_secs(20));
        assert_eq!(proxy.retry.min_retries_per_second, 10);

        let error = Config::parse("[snapshot]\nsource = \"x\"\n[circuit]\nerror_ratio = 0\n")
            .unwrap_err()
            .to_string();
        assert!(error.contains("circuit.error_ratio"), "{error}");
        let error = Config::parse("[snapshot]\nsource = \"x\"\n[admission]\nbound = 0\n")
            .unwrap_err()
            .to_string();
        assert!(error.contains("admission.bound"), "{error}");
        assert!(Config::parse("[snapshot]\nsource = \"x\"\n[circuit]\nbogus = 1\n").is_err());
    }

    #[test]
    fn invalid_tuning_values_are_rejected_with_the_key_named() {
        let cases = [
            (
                "[circuit]\npanic_threshold = 0.0\n",
                "circuit.panic_threshold",
            ),
            (
                "[pressure]\nttft_degrade_factor = nan\n",
                "pressure.ttft_degrade_factor",
            ),
            (
                "[pressure]\nttft_degrade_factor = inf\n",
                "pressure.ttft_degrade_factor",
            ),
            ("[circuit]\nratio_window = 10.5\n", "circuit.ratio_window"),
            ("[retry]\nbudget_window = 2.5\n", "retry.budget_window"),
        ];
        for (table, key) in cases {
            let text = format!("[snapshot]\nsource = \"x\"\n{table}");
            let error = Config::parse(&text).unwrap_err().to_string();
            assert!(error.contains(key), "{table}: {error}");
        }
        let config = Config::parse(
            "[snapshot]\nsource = \"x\"\n[circuit]\npanic_threshold = 1.0\nratio_window = 20.0\n",
        )
        .unwrap();
        assert_eq!(config.circuit.panic_threshold, 1.0);
        assert_eq!(config.circuit.ratio_window, Duration::from_secs(20));
    }

    #[test]
    fn huge_durations_are_rejected_with_the_key_named() {
        let error = Config::parse("[snapshot]\nsource = \"x\"\n[circuit]\nmax_backoff = 1e20\n")
            .unwrap_err();
        let message = format!("{error:#}");
        assert!(message.contains("max_backoff"), "{message}");
        assert!(message.contains("too large"), "{message}");
        let error = Config::parse(
            "[snapshot]\nsource = \"x\"\n[circuit]\nbase_backoff = 1e19\nmax_backoff = 1e19\n",
        )
        .unwrap_err();
        let message = format!("{error:#}");
        assert!(message.contains("circuit.base_backoff"), "{message}");
    }

    #[test]
    fn upstream_ca_is_optional_and_independent_of_listener_tls() {
        let config = Config::parse(
            "upstream_ca = \"/etc/multihull/upstream-ca.pem\"\n[snapshot]\nsource = \"x\"\n",
        )
        .unwrap();
        assert!(config.tls.is_none());
        assert_eq!(
            config.proxy_config().upstream_ca.as_deref(),
            Some(Path::new("/etc/multihull/upstream-ca.pem"))
        );
    }

    #[test]
    fn snapshot_table_carries_tls_token_and_insecure_settings() {
        let config = Config::parse(
            "[snapshot]\nsource = \"grpcs://controller:7700\"\nca = \"/etc/multihull/discovery/ca.crt\"\nclient_cert = \"/etc/multihull/discovery/tls.crt\"\nclient_key = \"/etc/multihull/discovery/tls.key\"\ntoken_env = \"MULTIHULL_DISCOVERY_TOKEN\"\n",
        )
        .unwrap();
        let security = config.snapshot.security();
        assert_eq!(
            security.ca.as_deref(),
            Some(Path::new("/etc/multihull/discovery/ca.crt"))
        );
        assert_eq!(
            security.client_key.as_deref(),
            Some(Path::new("/etc/multihull/discovery/tls.key"))
        );
        assert_eq!(
            security.token_env.as_deref(),
            Some("MULTIHULL_DISCOVERY_TOKEN")
        );
        assert!(!security.insecure);

        let error = format!(
            "{:#}",
            Config::parse("[snapshot]\nsource = \"grpc://controller:7700\"\n").unwrap_err()
        );
        assert!(error.contains("insecure = true"), "{error}");
        let plaintext =
            Config::parse("[snapshot]\nsource = \"grpc://controller:7700\"\ninsecure = true\n")
                .unwrap();
        assert!(plaintext.snapshot.insecure);
        let error = format!(
            "{:#}",
            Config::parse("[snapshot]\nsource = \"grpcs://c:1\"\nclient_cert = \"/c\"\n")
                .unwrap_err()
        );
        assert!(error.contains("together"), "{error}");
        assert!(Config::parse("[snapshot]\nsource = \"ftp://c\"\n").is_err());
    }

    #[test]
    fn minimal_config_uses_defaults() {
        let config = Config::parse("[snapshot]\nsource = \"grpcs://controller:7777\"\n").unwrap();
        assert_eq!(config.listen, default_listen());
        assert_eq!(config.timeouts, PhaseTimeouts::default());
        assert_eq!(config.max_buffered_body_bytes, 1024 * 1024);
        assert!(config.tls.is_none());
    }

    #[test]
    fn probe_table_overrides_defaults_and_is_validated() {
        let config = Config::parse(
            "[snapshot]\nsource = \"x\"\n[probe]\ninterval = 2\ntimeout = 0.5\nfailure_threshold = 2\n",
        )
        .unwrap();
        assert_eq!(config.probe.interval, Duration::from_secs(2));
        assert_eq!(config.probe.timeout, Duration::from_millis(500));
        assert_eq!(config.probe.failure_threshold, 2);
        assert_eq!(config.probe.success_threshold, 3);
        assert_eq!(config.proxy_config().probe, config.probe);
        let error = Config::parse("[snapshot]\nsource = \"x\"\n[probe]\ninterval = 0\n")
            .unwrap_err()
            .to_string();
        assert!(error.contains("probe.interval"), "{error}");
    }

    #[test]
    fn unknown_keys_are_rejected() {
        assert!(Config::parse("[snapshot]\nsource = \"x\"\nbogus = 1\n").is_err());
        assert!(Config::parse("listen = \"0.0.0.0:1\"\n").is_err());
    }
}
