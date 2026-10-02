use http::header::HeaderName;
use http::{header, HeaderValue, Method, Request, Response};
use http_body_util::{BodyExt, Limited};
use hyper::body::Incoming;
use router_auth::ApiKey;
use router_core::circuit::{apply_panic_threshold, PANIC_THRESHOLD};
use router_core::outcome::{classify, Outcome};
use router_core::retry::{decide, RetryContext, RetryDecision};
use router_core::score::{select, Candidate, Preset};
use router_core::snapshot::{Endpoint, EndpointId, FailoverPolicy, Health, Route, Sticky};
use router_core::sticky::{hash_key, owner, owner_in_provider, rank, KeyHash};
use std::collections::HashSet;
use std::net::{IpAddr, SocketAddr};
use std::sync::Arc;
use std::time::Instant;

use crate::attempt::{response_headers, send, UpstreamResponse};
use crate::body::{ProxyBody, TimedBody};
use crate::error::ProxyError;
use crate::runtime::{OutstandingGuard, ThreadRng};
use crate::state::ProxyState;
use crate::sticky::{
    mint_session_id, rehomed_header, session_cookie, StickyPlan, REHOMED_HEADER, SESSION_HEADER,
};

pub async fn handle(
    state: Arc<ProxyState>,
    request: Request<Incoming>,
    peer: SocketAddr,
) -> Response<ProxyBody> {
    match proxy(state, request, peer).await {
        Ok(response) => response,
        Err((error, attempts)) => error.into_response(attempts),
    }
}

async fn proxy(
    state: Arc<ProxyState>,
    request: Request<Incoming>,
    peer: SocketAddr,
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

    let admitted = state
        .runtime
        .admission
        .wait_for_slot(&route.id, || !state.runtime.route_saturated(&route))
        .await;
    let waited = match admitted {
        Ok(waited) => waited,
        Err(_) => state.runtime.admission.config().max_wait,
    };
    state.runtime.record_queue_wait(&route, waited);
    admitted.map_err(|_| (ProxyError::QueueOverflow, 0))?;

    let mut rng = ThreadRng;
    let mut session = resolve_session(&state, &route, &parts.headers, &body, peer.ip())
        .map_err(|error| (error, 0))?;
    let mut preferred = session.as_ref().and_then(|s| s.owner.clone());

    let mut excluded: HashSet<EndpointId> = HashSet::new();
    let mut excluded_providers: HashSet<String> = HashSet::new();
    let mut attempts: u32 = 0;
    let mut last_error = ProxyError::NoHealthyUpstream;
    let mut fallback: Option<(UpstreamResponse, Endpoint, OutstandingGuard)> = None;

    loop {
        let endpoint = preferred
            .take()
            .and_then(|id| take_preferred(&state, &route, &id, &mut rng))
            .or_else(|| {
                pick_endpoint(
                    &state,
                    &route,
                    &excluded,
                    &excluded_providers,
                    preset,
                    &mut rng,
                )
            });
        let Some(endpoint) = endpoint else {
            return match fallback.take() {
                Some((response, endpoint, guard)) => {
                    let extra = finish_session(&state, &route, session.take(), Some(&endpoint));
                    Ok(forward(
                        response, &state, &endpoint, attempts, deadline, guard, extra,
                    ))
                }
                None => {
                    finish_session(&state, &route, session.take(), None);
                    Err((last_error, attempts))
                }
            };
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
            state.runtime.record_ttft(&route, &endpoint, attempt.ttft);
        }
        state
            .runtime
            .record_attempt(&endpoint, outcome, attempt.status(), now, &mut rng);
        metrics::counter!(
            router_obs::metrics::REQUESTS_TOTAL,
            router_obs::metrics::labels::ROUTE => route.id.clone(),
            router_obs::metrics::labels::ENDPOINT => endpoint.id.clone(),
            router_obs::metrics::labels::OUTCOME => outcome.label()
        )
        .increment(1);

        let server_error = matches!(attempt.status(), Some(500..=599));
        if outcome == Outcome::Success || (outcome == Outcome::Fatal && !server_error) {
            let response = attempt
                .response
                .expect("status present for success or fatal");
            let extra = finish_session(&state, &route, session.take(), Some(&endpoint));
            return Ok(forward(
                response, &state, &endpoint, attempts, deadline, guard, extra,
            ));
        }

        let retry_ctx = RetryContext {
            bytes_committed: false,
            body_buffered: true,
            idempotent,
            server_error,
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
                fallback = attempt
                    .response
                    .map(|response| (response, endpoint.clone(), guard));
                if Instant::now() >= deadline {
                    finish_session(&state, &route, session.take(), None);
                    return Err((ProxyError::UpstreamTimeout, attempts));
                }
                continue;
            }
            RetryDecision::Stop(_) => {
                return match attempt.response {
                    Some(response) => {
                        let extra = finish_session(&state, &route, session.take(), Some(&endpoint));
                        Ok(forward(
                            response, &state, &endpoint, attempts, deadline, guard, extra,
                        ))
                    }
                    None => {
                        finish_session(&state, &route, session.take(), None);
                        Err((last_error, attempts))
                    }
                };
            }
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum StickyOutcome {
    Hit,
    Miss,
    Rehomed,
    Failed,
}

impl StickyOutcome {
    fn label(self) -> &'static str {
        match self {
            StickyOutcome::Hit => "hit",
            StickyOutcome::Miss => "miss",
            StickyOutcome::Rehomed => "rehomed",
            StickyOutcome::Failed => "failed",
        }
    }
}

struct Session<'a> {
    sticky: &'a Sticky,
    plan: StickyPlan,
    key_hash: KeyHash,
    owner: Option<EndpointId>,
    origin: Option<EndpointId>,
    minted: Option<String>,
    outcome: StickyOutcome,
}

fn resolve_session<'a>(
    state: &ProxyState,
    route: &'a Route,
    headers: &http::HeaderMap,
    body: &[u8],
    peer: IpAddr,
) -> Result<Option<Session<'a>>, ProxyError> {
    let Some(sticky) = route.sticky.as_ref() else {
        return Ok(None);
    };
    let Some(plan) = StickyPlan::parse(&sticky.key, &sticky.fallback_key) else {
        tracing::warn!(route = %route.id, key = %sticky.key, "ignoring sticky config with invalid key");
        return Ok(None);
    };
    let (key, minted) = match plan.extract(headers, body, peer) {
        Some(key) => (key, None),
        None => {
            let id = mint_session_id();
            (id.clone().into_bytes(), Some(id))
        }
    };
    let key_hash = hash_key(&key);
    if minted.is_some() {
        return Ok(Some(Session {
            sticky,
            plan,
            key_hash,
            owner: None,
            origin: None,
            minted,
            outcome: StickyOutcome::Miss,
        }));
    }

    let now = state.runtime.now();
    let find = |id: &str| route.endpoints.iter().find(|e| e.id == id);
    let healthy = |e: &Endpoint| state.runtime.endpoint_healthy(e, now);
    let pinned = state.runtime.pinned(&route.id, sticky, &key_hash);
    if let Some(endpoint) = pinned.as_deref().and_then(find) {
        if healthy(endpoint) {
            return Ok(Some(Session {
                sticky,
                plan,
                key_hash,
                owner: Some(endpoint.id.clone()),
                origin: None,
                minted: None,
                outcome: StickyOutcome::Hit,
            }));
        }
    }

    let is_new = pinned.is_none();
    let candidates: Vec<router_core::sticky::Candidate<'_>> = route
        .endpoints
        .iter()
        .filter(|e| e.accepts_traffic())
        .map(|e| router_core::sticky::Candidate::new(&e.id, &e.provider, e.max_concurrency))
        .collect();
    let eligible =
        |id: &str| find(id).is_some_and(|e| healthy(e) && (!is_new || e.accepts_new_sessions()));
    let primary = rank(&key, &candidates).first().map(|id| id.to_string());
    let placed = if sticky.is_provider_mode() {
        owner_in_provider(&key, &candidates, eligible)
    } else {
        owner(&key, &candidates, eligible)
    };
    let Some((owner_id, moved)) = placed else {
        return Ok(Some(Session {
            sticky,
            plan,
            key_hash,
            owner: None,
            origin: pinned.or(primary),
            minted: None,
            outcome: StickyOutcome::Failed,
        }));
    };
    let from = pinned.or(if moved { primary } else { None });
    let skipped_draining = from
        .as_deref()
        .and_then(find)
        .is_some_and(|e| is_new && e.health == Health::Draining);
    let (origin, outcome) = match from {
        None => (None, StickyOutcome::Hit),
        Some(_) if skipped_draining => (None, StickyOutcome::Hit),
        Some(_) if sticky.fails_on_unhealthy() => {
            record_sticky(StickyOutcome::Failed);
            return Err(ProxyError::SessionLost);
        }
        Some(from) => (Some(from), StickyOutcome::Rehomed),
    };
    Ok(Some(Session {
        sticky,
        plan,
        key_hash,
        owner: Some(owner_id),
        origin,
        minted: None,
        outcome,
    }))
}

fn finish_session(
    state: &ProxyState,
    route: &Route,
    session: Option<Session<'_>>,
    served_by: Option<&Endpoint>,
) -> Vec<(HeaderName, HeaderValue)> {
    let Some(session) = session else {
        return Vec::new();
    };
    let Some(endpoint) = served_by else {
        record_sticky(StickyOutcome::Failed);
        return Vec::new();
    };
    state.runtime.pin(
        &route.id,
        session.sticky,
        session.key_hash,
        endpoint.id.clone(),
    );
    let mut extra = Vec::new();
    let expected = session.origin.clone().or_else(|| session.owner.clone());
    let moved = expected
        .as_deref()
        .is_some_and(|expected| expected != endpoint.id);
    let outcome = if moved {
        StickyOutcome::Rehomed
    } else {
        session.outcome
    };
    if let (true, Some(from)) = (moved, expected.as_deref()) {
        if let Some(value) = rehomed_header(from, &endpoint.id) {
            extra.push((HeaderName::from_static(REHOMED_HEADER), value));
        }
    }
    if let Some(id) = session.minted.as_deref() {
        if let Ok(value) = HeaderValue::from_str(id) {
            extra.push((HeaderName::from_static(SESSION_HEADER), value));
        }
        if let Some(cookie) = session
            .plan
            .cookie_name()
            .and_then(|name| session_cookie(name, id, session.sticky.ttl()))
        {
            extra.push(cookie);
        }
    }
    record_sticky(outcome);
    extra
}

fn record_sticky(outcome: StickyOutcome) {
    metrics::counter!(
        router_obs::metrics::STICKY_REQUESTS_TOTAL,
        router_obs::metrics::labels::OUTCOME => outcome.label()
    )
    .increment(1);
}

fn take_preferred(
    state: &ProxyState,
    route: &Route,
    id: &str,
    rng: &mut ThreadRng,
) -> Option<Endpoint> {
    let endpoint = route.endpoints.iter().find(|e| e.id == id)?;
    let now = state.runtime.now();
    if !state.runtime.endpoint_healthy(endpoint, now) {
        return None;
    }
    state
        .runtime
        .endpoint(endpoint)
        .admit(now, rng)
        .then(|| endpoint.clone())
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
    let circuit_open = |e: &&Endpoint| state.runtime.endpoint_circuit_open(e, now);
    let routable: Vec<&Endpoint> = apply_panic_threshold(&eligible, circuit_open, PANIC_THRESHOLD)
        .into_iter()
        .copied()
        .collect();
    let panic_mode = routable.len() == eligible.len() && eligible.iter().any(circuit_open);
    let provider_closed: Vec<&Endpoint> = routable
        .iter()
        .copied()
        .filter(|e| !state.runtime.provider_open(&e.provider, now))
        .collect();
    let routable = if provider_closed.is_empty() {
        routable
    } else {
        provider_closed
    };
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
    upstream: UpstreamResponse,
    state: &ProxyState,
    endpoint: &Endpoint,
    attempts: u32,
    deadline: Instant,
    guard: OutstandingGuard,
    extra: Vec<(HeaderName, HeaderValue)>,
) -> Response<ProxyBody> {
    let UpstreamResponse {
        response,
        first_frame,
    } = upstream;
    let (parts, body) = response.into_parts();
    let timed = TimedBody::new(
        body,
        first_frame,
        state.config.timeouts.idle,
        deadline,
        guard,
    );
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
        for (name, value) in extra {
            headers.append(name, value);
        }
    }
    builder
        .body(timed.boxed())
        .unwrap_or_else(|_| ProxyError::Internal.into_response(attempts))
}
