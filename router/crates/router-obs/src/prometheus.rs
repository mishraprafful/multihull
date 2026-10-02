use metrics_exporter_prometheus::{BuildError, PrometheusBuilder};

pub use metrics_exporter_prometheus::PrometheusHandle;

pub fn install_prometheus() -> Result<PrometheusHandle, BuildError> {
    let handle = PrometheusBuilder::new().install_recorder()?;
    crate::metrics::describe_all();
    Ok(handle)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn installed_recorder_renders_counters_in_text_exposition() {
        let handle = install_prometheus().expect("first recorder in this process");
        metrics::counter!(
            crate::metrics::REQUESTS_TOTAL,
            crate::metrics::labels::ROUTE => "llama",
            crate::metrics::labels::ENDPOINT => "e1",
            crate::metrics::labels::OUTCOME => "success"
        )
        .increment(3);
        metrics::gauge!(
            crate::metrics::CIRCUIT_STATE,
            crate::metrics::labels::ENDPOINT => "e1",
            crate::metrics::labels::PROVIDER => "p1"
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
