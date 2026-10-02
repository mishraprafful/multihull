use bytes::Bytes;
use http::{HeaderMap, HeaderValue, Version};
use http_body_util::combinators::BoxBody;
use hyper::body::{Body, Frame, Incoming};
use router_core::outcome::Outcome;
use router_core::snapshot::{Endpoint, Protocol, Route};
use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;
use std::task::{ready, Context, Poll};
use std::time::{Duration, Instant};
use tokio::time::Sleep;

use crate::runtime::{OutstandingGuard, ThreadRng};
use crate::state::ProxyState;

pub type ProxyBody = BoxBody<Bytes, BodyError>;

pub const UPSTREAM_DISCONNECTED: &str = "upstream_disconnected";
pub const ERROR_HEADER: &str = "x-hull-error";
pub const ERROR_REASON_HEADER: &str = "x-hull-error-reason";

#[derive(Debug, thiserror::Error)]
pub enum BodyError {
    #[error("upstream idle timeout")]
    IdleTimeout,
    #[error("upstream total timeout")]
    TotalTimeout,
    #[error("upstream body error: {0}")]
    Upstream(#[from] hyper::Error),
}

impl BodyError {
    pub fn reason(&self) -> &'static str {
        match self {
            BodyError::IdleTimeout => "idle_timeout",
            BodyError::TotalTimeout => "total_timeout",
            BodyError::Upstream(_) => "disconnect",
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Termination {
    SseEvent,
    Http2Trailers,
    Close,
}

impl Termination {
    pub fn for_response(route: &Route, content_type: Option<&str>, client: Version) -> Self {
        let sse = route.protocol == Protocol::Sse
            || content_type.is_some_and(|value| {
                value
                    .trim_start()
                    .to_ascii_lowercase()
                    .starts_with("text/event-stream")
            });
        if sse {
            Termination::SseEvent
        } else if client == Version::HTTP_2 {
            Termination::Http2Trailers
        } else {
            Termination::Close
        }
    }
}

pub struct StreamContext {
    pub state: Arc<ProxyState>,
    pub route_id: String,
    pub endpoint: Endpoint,
    pub termination: Termination,
}

impl StreamContext {
    fn record_disconnect(&self, error: &BodyError) {
        metrics::counter!(
            router_obs::metrics::REQUESTS_TOTAL,
            router_obs::metrics::labels::ROUTE => self.route_id.clone(),
            router_obs::metrics::labels::ENDPOINT => self.endpoint.id.clone(),
            router_obs::metrics::labels::OUTCOME => UPSTREAM_DISCONNECTED
        )
        .increment(1);
        let now = self.state.runtime.now();
        self.state.runtime.record_attempt(
            &self.endpoint,
            Outcome::Transient,
            None,
            now,
            &mut ThreadRng,
        );
        tracing::warn!(
            route = %self.route_id,
            endpoint = %self.endpoint.id,
            provider = %self.endpoint.provider,
            reason = error.reason(),
            "upstream disconnected mid-stream"
        );
    }

    fn terminal_frame(&self, error: &BodyError) -> Option<Frame<Bytes>> {
        match self.termination {
            Termination::SseEvent => Some(Frame::data(sse_terminal_event(
                &self.endpoint.provider,
                error.reason(),
            ))),
            Termination::Http2Trailers => {
                let mut trailers = HeaderMap::new();
                trailers.insert(
                    ERROR_HEADER,
                    HeaderValue::from_static(UPSTREAM_DISCONNECTED),
                );
                trailers.insert(
                    ERROR_REASON_HEADER,
                    HeaderValue::from_static(error.reason()),
                );
                Some(Frame::trailers(trailers))
            }
            Termination::Close => None,
        }
    }
}

pub fn sse_terminal_event(provider: &str, reason: &str) -> Bytes {
    let payload = serde_json::json!({
        "error": {
            "type": UPSTREAM_DISCONNECTED,
            "retryable": true,
            "provider": provider,
            "reason": reason,
        }
    });
    Bytes::from(format!("\n\ndata: {payload}\n\ndata: [DONE]\n\n"))
}

pub struct TimedBody {
    inner: Incoming,
    first: Option<Frame<Bytes>>,
    idle: Duration,
    idle_timer: Pin<Box<Sleep>>,
    total_timer: Pin<Box<Sleep>>,
    context: Option<StreamContext>,
    committed: bool,
    finished: bool,
    _guard: OutstandingGuard,
}

impl TimedBody {
    pub fn new(
        inner: Incoming,
        first: Option<Frame<Bytes>>,
        idle: Duration,
        deadline: Instant,
        guard: OutstandingGuard,
    ) -> Self {
        Self {
            inner,
            first,
            idle,
            idle_timer: Box::pin(tokio::time::sleep(idle)),
            total_timer: Box::pin(tokio::time::sleep_until(deadline.into())),
            context: None,
            committed: false,
            finished: false,
            _guard: guard,
        }
    }

    pub fn with_context(mut self, context: StreamContext) -> Self {
        self.context = Some(context);
        self
    }

    fn fail(&mut self, error: BodyError) -> Poll<Option<Result<Frame<Bytes>, BodyError>>> {
        self.finished = true;
        let Some(context) = self.context.as_ref() else {
            return Poll::Ready(Some(Err(error)));
        };
        if !self.committed {
            return Poll::Ready(Some(Err(error)));
        }
        context.record_disconnect(&error);
        match context.terminal_frame(&error) {
            Some(frame) => Poll::Ready(Some(Ok(frame))),
            None => Poll::Ready(Some(Err(error))),
        }
    }
}

impl Body for TimedBody {
    type Data = Bytes;
    type Error = BodyError;

    fn poll_frame(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
    ) -> Poll<Option<Result<Frame<Self::Data>, Self::Error>>> {
        if self.finished {
            return Poll::Ready(None);
        }
        if self.total_timer.as_mut().poll(cx).is_ready() {
            return self.fail(BodyError::TotalTimeout);
        }
        if let Some(frame) = self.first.take() {
            self.committed = true;
            return Poll::Ready(Some(Ok(frame)));
        }
        match Pin::new(&mut self.inner).poll_frame(cx) {
            Poll::Ready(Some(Ok(frame))) => {
                let idle = self.idle;
                self.idle_timer
                    .as_mut()
                    .reset(tokio::time::Instant::now() + idle);
                self.committed = true;
                Poll::Ready(Some(Ok(frame)))
            }
            Poll::Ready(Some(Err(error))) => self.fail(BodyError::Upstream(error)),
            Poll::Ready(None) => {
                self.finished = true;
                Poll::Ready(None)
            }
            Poll::Pending => {
                ready!(self.idle_timer.as_mut().poll(cx));
                self.fail(BodyError::IdleTimeout)
            }
        }
    }

    fn is_end_stream(&self) -> bool {
        self.finished || (self.first.is_none() && self.inner.is_end_stream())
    }

    fn size_hint(&self) -> hyper::body::SizeHint {
        let buffered = self
            .first
            .as_ref()
            .and_then(Frame::data_ref)
            .map(|data| data.len() as u64)
            .unwrap_or(0);
        let inner = self.inner.size_hint();
        let mut hint = hyper::body::SizeHint::new();
        hint.set_lower(inner.lower() + buffered);
        if self.context.is_none() {
            if let Some(upper) = inner.upper() {
                hint.set_upper(upper + buffered);
            }
        }
        hint
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sse_routes_and_event_stream_bodies_get_a_terminal_event() {
        let route = Route::default();
        assert_eq!(
            Termination::for_response(
                &route,
                Some("text/event-stream; charset=utf-8"),
                Version::HTTP_11
            ),
            Termination::SseEvent
        );
        assert_eq!(
            Termination::for_response(&route, Some("application/json"), Version::HTTP_11),
            Termination::Close
        );
        assert_eq!(
            Termination::for_response(&route, Some("application/octet-stream"), Version::HTTP_2),
            Termination::Http2Trailers
        );
        let sse_route = Route {
            protocol: Protocol::Sse,
            ..Route::default()
        };
        assert_eq!(
            Termination::for_response(&sse_route, None, Version::HTTP_2),
            Termination::SseEvent
        );
    }

    #[test]
    fn terminal_event_names_the_provider_and_ends_with_done() {
        let text = String::from_utf8(sse_terminal_event("modal", "disconnect").to_vec()).unwrap();
        assert!(text.starts_with("\n\ndata: {"));
        assert!(text.ends_with("\n\ndata: [DONE]\n\n"));
        let payload = text
            .trim()
            .lines()
            .next()
            .unwrap()
            .strip_prefix("data: ")
            .unwrap();
        let parsed: serde_json::Value = serde_json::from_str(payload).unwrap();
        assert_eq!(parsed["error"]["type"], UPSTREAM_DISCONNECTED);
        assert_eq!(parsed["error"]["retryable"], true);
        assert_eq!(parsed["error"]["provider"], "modal");
        assert_eq!(parsed["error"]["reason"], "disconnect");
    }
}
