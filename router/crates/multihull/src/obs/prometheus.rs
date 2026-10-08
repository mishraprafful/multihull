use metrics_exporter_prometheus::{BuildError, PrometheusBuilder};

pub use metrics_exporter_prometheus::PrometheusHandle;

pub fn install_prometheus() -> Result<PrometheusHandle, BuildError> {
    let handle = PrometheusBuilder::new().install_recorder()?;
    crate::obs::metrics::describe_all();
    Ok(handle)
}

#[cfg(test)]
pub(crate) fn process_recorder() -> PrometheusHandle {
    static HANDLE: std::sync::OnceLock<PrometheusHandle> = std::sync::OnceLock::new();
    HANDLE
        .get_or_init(|| install_prometheus().expect("first recorder in this process"))
        .clone()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn installed_recorder_renders_counters_in_text_exposition() {
        let handle = process_recorder();
        metrics::counter!(
            crate::obs::metrics::REQUESTS_TOTAL,
            crate::obs::metrics::labels::ROUTE => "llama",
            crate::obs::metrics::labels::ENDPOINT => "e1",
            crate::obs::metrics::labels::OUTCOME => "success"
        )
        .increment(3);
        metrics::gauge!(
            crate::obs::metrics::CIRCUIT_STATE,
            crate::obs::metrics::labels::ENDPOINT => "e1",
            crate::obs::metrics::labels::PROVIDER => "p1"
        )
        .set(2.0);
        let text = handle.render();
        assert!(text.contains("# TYPE router_requests_total counter"));
        assert!(text.contains(
            "router_requests_total{route=\"llama\",endpoint=\"e1\",outcome=\"success\"} 3"
        ));
        assert!(text.contains("router_circuit_state{endpoint=\"e1\",provider=\"p1\"} 2"));
        assert!(install_prometheus().is_err());
    }
}
