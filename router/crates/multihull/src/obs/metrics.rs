pub const REQUESTS_TOTAL: &str = "router_requests_total";
pub const RESPONSES_TOTAL: &str = "router_responses_total";
pub const FAILOVERS_TOTAL: &str = "router_failovers_total";
pub const CIRCUIT_STATE: &str = "router_circuit_state";
pub const UPSTREAM_TTFT_SECONDS: &str = "router_upstream_ttft_seconds";
pub const QUEUE_WAIT_SECONDS: &str = "router_queue_wait_seconds";
pub const CONCURRENCY_LIMIT: &str = "router_concurrency_limit";
pub const RETRY_BUDGET_REMAINING: &str = "router_retry_budget_remaining";
pub const OUTPUT_TOKENS_TOTAL: &str = "router_output_tokens_total";
pub const STICKY_REQUESTS_TOTAL: &str = "router_sticky_requests_total";
pub const STICKY_SESSIONS_ACTIVE: &str = "router_sticky_sessions_active";
pub const PROBE_TOTAL: &str = "router_probe_total";

pub const ALL: &[&str] = &[
    REQUESTS_TOTAL,
    RESPONSES_TOTAL,
    FAILOVERS_TOTAL,
    CIRCUIT_STATE,
    UPSTREAM_TTFT_SECONDS,
    QUEUE_WAIT_SECONDS,
    CONCURRENCY_LIMIT,
    RETRY_BUDGET_REMAINING,
    OUTPUT_TOKENS_TOTAL,
    STICKY_REQUESTS_TOTAL,
    STICKY_SESSIONS_ACTIVE,
    PROBE_TOTAL,
];

pub mod labels {
    pub const ROUTE: &str = "route";
    pub const PROVIDER: &str = "provider";
    pub const ENDPOINT: &str = "endpoint";
    pub const OUTCOME: &str = "outcome";
    pub const STATUS: &str = "status";
    pub const FROM: &str = "from";
    pub const TO: &str = "to";
    pub const REASON: &str = "reason";
}

pub mod spans {
    pub const REQUEST: &str = "router.request";
    pub const UPSTREAM_ATTEMPT: &str = "upstream.attempt";
}

pub fn describe_all() {
    metrics::describe_counter!(REQUESTS_TOTAL, "Requests handled by the router");
    metrics::describe_counter!(
        RESPONSES_TOTAL,
        "Responses returned to clients by route and HTTP status"
    );
    metrics::describe_counter!(
        FAILOVERS_TOTAL,
        "Retries that moved a request to another provider"
    );
    metrics::describe_gauge!(
        CIRCUIT_STATE,
        "Circuit state per endpoint: 0 closed, 1 half-open, 2 open"
    );
    metrics::describe_histogram!(UPSTREAM_TTFT_SECONDS, "Upstream time to first byte");
    metrics::describe_histogram!(QUEUE_WAIT_SECONDS, "Time spent in the admission queue");
    metrics::describe_gauge!(CONCURRENCY_LIMIT, "Adaptive concurrency limit per endpoint");
    metrics::describe_gauge!(
        RETRY_BUDGET_REMAINING,
        "Retries left in the sliding budget per route"
    );
    metrics::describe_counter!(OUTPUT_TOKENS_TOTAL, "Output tokens observed in responses");
    metrics::describe_counter!(STICKY_REQUESTS_TOTAL, "Sticky routing decisions by outcome");
    metrics::describe_gauge!(
        STICKY_SESSIONS_ACTIVE,
        "Pinned sticky sessions held in memory"
    );
    metrics::describe_counter!(PROBE_TOTAL, "Active health probes by endpoint and outcome");
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn metric_names_use_router_prefix_and_are_unique() {
        let mut seen = std::collections::HashSet::new();
        for name in ALL {
            assert!(name.starts_with("router_"), "{name}");
            assert!(seen.insert(*name), "duplicate {name}");
        }
    }

    #[test]
    fn describe_all_is_safe_without_a_recorder() {
        describe_all();
    }
}
