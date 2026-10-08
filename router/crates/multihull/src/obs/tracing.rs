use tracing_subscriber::{fmt, EnvFilter};

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum TracingFormat {
    #[default]
    Text,
    Json,
}

#[derive(Clone, Debug)]
pub struct TracingConfig {
    pub format: TracingFormat,
    pub default_filter: String,
}

impl Default for TracingConfig {
    fn default() -> Self {
        Self {
            format: TracingFormat::Text,
            default_filter: "info".to_string(),
        }
    }
}

pub fn init_tracing(config: &TracingConfig) -> bool {
    let filter = EnvFilter::try_from_default_env()
        .unwrap_or_else(|_| EnvFilter::new(config.default_filter.clone()));
    let result = match config.format {
        TracingFormat::Text => fmt().with_env_filter(filter).with_target(false).try_init(),
        TracingFormat::Json => fmt().with_env_filter(filter).json().try_init(),
    };
    result.is_ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn second_init_reports_false_instead_of_panicking() {
        let config = TracingConfig::default();
        let first = init_tracing(&config);
        let second = init_tracing(&config);
        assert!(first || !second);
        assert!(!second);
    }
}
