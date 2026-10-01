use http::{header, HeaderValue, Method, Request, Response};
use http_body_util::{BodyExt, Limited};
use hyper::body::Incoming;
use router_auth::ApiKey;
use router_core::circuit::{apply_panic_threshold, PANIC_THRESHOLD};
use router_core::outcome::{classify, Outcome};
use router_core::retry::{decide, RetryContext, RetryDecision};
use router_core::score::{select, Candidate, Preset};
use router_core::snapshot::{Endpoint, EndpointId, FailoverPolicy, Route};
use std::collections::HashSet;
use std::sync::Arc;
use std::time::Instant;

use crate::attempt::{response_headers, send};
use crate::body::{ProxyBody, TimedBody};
use crate::error::ProxyError;
use crate::runtime::{OutstandingGuard, ThreadRng};
use crate::state::ProxyState;

pub async fn handle(state: Arc<ProxyState>, request: Request<Incoming>) -> Response<ProxyBody> {
    match proxy(state, request).await {
        Ok(response) => response,
        Err((error, attempts)) => error.into_response(attempts),
    }
}

async fn proxy(
    state: Arc<ProxyState>,
    request: Request<Incoming>,
) -> Result<Response<ProxyBody>, (ProxyError, u32)> {
    let started = Instant::now();
    let deadline = started + state.config.timeouts.total;
    let (parts, body) = request.into_parts();
    let host = parts
        .headers
        .get(header::HOST)
        .and_then(|v| v.to_str().ok())
        .or_else(|| parts.uri.host());
    let path = parts.uri.path();
    let route = state
        .table()
        .matches(host, path)
        .ok_or((ProxyError::NoRoute, 0))?;
    authorize(&route, &parts.headers)?;

    let limit = state.config.max_buffered_body_bytes;
    let body = Limited::new(body, limit)
        .collect()
        .await
        .map_err(|error| {
            if error
                .downcast_ref::<http_body_util::LengthLimitError>()
                .is_some()
            {
                (ProxyError::BodyTooLarge, 0)
            } else {
                (ProxyError::BodyRead, 0)
            }
        })?
        .to_bytes();

    let path_and_query = parts
        .uri
        .path_and_query()
        .map(|pq| pq.as_str())
        .unwrap_or("/");
    let idempotent = is_idempotent(&parts.method, &parts.headers);
    let preset = preset_for(&route);
    state.runtime.record_request(&route.id);

    let mut excluded: HashSet<EndpointId> = HashSet::new();
    let mut excluded_providers: HashSet<String> = HashSet::new();
    let mut attempts: u32 = 0;
    let mut rng = ThreadRng;
    let mut last_error = ProxyError::NoHealthyUpstream;

    loop {
        let Some(endpoint) = pick_endpoint(
            &state,
            &route,
            &excluded,
            &excluded_providers,
            preset,
            &mut rng,
        ) else {
            return Err((last_error, attempts));
        };
        attempts += 1;
        let runtime = state.runtime.endpoint(&endpoint);
        let guard = OutstandingGuard::acquire(runtime.clone());
        let remaining = deadline.saturating_duration_since(Instant::now());
        let first_byte = state.config.timeouts.first_byte.min(remaining);
        let attempt = send(
            &state.client,
            &endpoint,
            parts.method.clone(),
            path_and_query,
            &parts.headers,
            body.clone(),
            first_byte,
        )
        .await;
        let ttft_timed_out = matches!(
            attempt.error,
            Some(router_core::outcome::AttemptError::FirstByteTimeout)
        );
        let outcome = classify(attempt.status(), attempt.error.as_ref(), ttft_timed_out);
        let now = state.runtime.now();
        if attempt.response.is_some() {
            runtime.record_ttft(attempt.ttft);
        }
        runtime.record_outcome(outcome, now, &mut rng);
        metrics::counter!(
            router_obs::metrics::REQUESTS_TOTAL,
            router_obs::metrics::labels::ROUTE => route.id.clone(),
            router_obs::metrics::labels::ENDPOINT => endpoint.id.clone(),
            router_obs::metrics::labels::OUTCOME => outcome.label()
        )
        .increment(1);

        if outcome == Outcome::Success || outcome == Outcome::Fatal {
            let response = attempt
                .response
                .expect("status present for success or fatal");
            return Ok(forward(
                response, &state, &endpoint, attempts, deadline, guard,
            ));
        }

        let retry_ctx = RetryContext {
            bytes_committed: false,
            body_buffered: true,
            idempotent,
            retries_used: attempts.saturating_sub(1),
            max_retries: route.failover.max_retries,
        };
        let decision = state.runtime.with_budget(&route.id, |budget, now| {
            decide(outcome, attempt.error.as_ref(), &retry_ctx, budget, now)
        });
        last_error = match (outcome, attempt.error) {
            (Outcome::Capacity, _) => ProxyError::UpstreamTimeout,
            _ => ProxyError::UpstreamUnavailable,
        };
        match decision {
            RetryDecision::Retry { exclude_provider } => {
                excluded.insert(endpoint.id.clone());
                if exclude_provider && !endpoint.provider.is_empty() {
                    excluded_providers.insert(endpoint.provider.clone());
                }
                metrics::counter!(
                    router_obs::metrics::FAILOVERS_TOTAL,
                    router_obs::metrics::labels::FROM => endpoint.provider.clone(),
                    router_obs::metrics::labels::REASON => outcome.label()
                )
                .increment(1);
                drop(guard);
                if Instant::now() >= deadline {
                    return Err((ProxyError::UpstreamTimeout, attempts));
                }
                continue;
            }
            RetryDecision::Stop(_) => {
                return match attempt.response {
                    Some(response) => Ok(forward(
                        response, &state, &endpoint, attempts, deadline, guard,
                    )),
                    None => Err((last_error, attempts)),
                };
            }
        }
    }
}

fn authorize(route: &Route, headers: &http::HeaderMap) -> Result<(), (ProxyError, u32)> {
    if route.auth.api_key_hashes.is_empty() {
        return Ok(());
    }
    let raw = headers
        .get(header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .ok_or((ProxyError::Unauthorized, 0))?;
    let key = ApiKey::from_bearer(raw).map_err(|_| (ProxyError::Unauthorized, 0))?;
    if key.matches_any(route.auth.api_key_hashes.iter().map(String::as_str)) {
        Ok(())
    } else {
        Err((ProxyError::Unauthorized, 0))
    }
}

fn is_idempotent(method: &Method, headers: &http::HeaderMap) -> bool {
    method.is_idempotent() || headers.contains_key("idempotency-key")
}

fn preset_for(route: &Route) -> Preset {
    match route.failover.policy {
        FailoverPolicy::Weighted => Preset::Weighted,
        FailoverPolicy::Latency => Preset::EwmaLatency,
        FailoverPolicy::Priority | FailoverPolicy::Unspecified => Preset::PrioritySpillover,
    }
}

fn pick_endpoint(
    state: &ProxyState,
    route: &Route,
    excluded: &HashSet<EndpointId>,
    excluded_providers: &HashSet<String>,
    preset: Preset,
    rng: &mut ThreadRng,
) -> Option<Endpoint> {
    let eligible: Vec<&Endpoint> = route
        .endpoints
        .iter()
        .filter(|e| e.accepts_traffic())
        .filter(|e| !excluded.contains(&e.id))
        .filter(|e| !excluded_providers.contains(&e.provider))
        .collect();
    if eligible.is_empty() {
        return None;
    }
    let now = state.runtime.now();
    let routable = apply_panic_threshold(
        &eligible,
        |e| {
            state
                .runtime
                .get(&e.id)
                .map(|rt| rt.circuit_state(now).is_open())
                .unwrap_or(false)
        },
        PANIC_THRESHOLD,
    );
    let panic_mode = routable.len() == eligible.len()
        && eligible.iter().any(|e| {
            state
                .runtime
                .get(&e.id)
                .map(|rt| rt.circuit_state(now).is_open())
                .unwrap_or(false)
        });
    let candidates: Vec<Candidate> = routable
        .iter()
        .map(|e| {
            Candidate::new(e.id.clone(), e.priority)
                .with_weight(e.weight)
                .with_region(e.region.clone())
        })
        .collect();
    let mut locally_excluded = excluded.clone();
    for _ in 0..candidates.len().max(1) {
        let chosen = if panic_mode {
            let pool: Vec<&Candidate> = candidates
                .iter()
                .filter(|c| !locally_excluded.contains(&c.id))
                .collect();
            if pool.is_empty() {
                return None;
            }
            Some(pool[rng_index(rng, pool.len())].id.clone())
        } else {
            let view = state
                .runtime
                .view(&locally_excluded, state.config.region.as_deref());
            select(&candidates, preset, &view, rng)
        };
        let id = chosen?;
        let endpoint = routable.iter().find(|e| e.id == id)?;
        let runtime = state.runtime.endpoint(endpoint);
        if panic_mode || runtime.admit(now, rng) {
            return Some(Endpoint::clone(endpoint));
        }
        locally_excluded.insert(id);
    }
    None
}

fn rng_index(rng: &mut ThreadRng, len: usize) -> usize {
    router_core::rng::Rng::next_below(rng, len)
}

fn forward(
    response: Response<Incoming>,
    state: &ProxyState,
    endpoint: &Endpoint,
    attempts: u32,
    deadline: Instant,
    guard: OutstandingGuard,
) -> Response<ProxyBody> {
    let (parts, body) = response.into_parts();
    let timed = TimedBody::new(body, state.config.timeouts.idle, deadline, guard);
    let mut builder = Response::builder()
        .status(parts.status)
        .version(parts.version);
    if let Some(headers) = builder.headers_mut() {
        headers.extend(response_headers(&parts.headers));
        if let Ok(value) = HeaderValue::from_str(&endpoint.id) {
            headers.insert("x-hull-endpoint", value);
        }
        if let Ok(value) = HeaderValue::from_str(&endpoint.provider) {
            headers.insert("x-hull-provider", value);
        }
        headers.insert("x-hull-attempts", HeaderValue::from(attempts));
    }
    builder
        .body(timed.boxed())
        .unwrap_or_else(|_| ProxyError::Internal.into_response(attempts))
}
