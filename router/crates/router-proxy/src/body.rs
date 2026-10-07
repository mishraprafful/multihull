use bytes::{Bytes, BytesMut};
use http::{header, HeaderMap, HeaderValue, Version};
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
pub const TOTAL_TIMEOUT: &str = "total_timeout";
pub const ERROR_HEADER: &str = "x-hull-error";
pub const ERROR_REASON_HEADER: &str = "x-hull-error-reason";
pub const MAX_HELD_EVENT_BYTES: usize = 1024 * 1024;

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
            BodyError::TotalTimeout => TOTAL_TIMEOUT,
            BodyError::Upstream(_) => "disconnect",
        }
    }

    pub fn kind(&self) -> &'static str {
        match self {
            BodyError::TotalTimeout => TOTAL_TIMEOUT,
            BodyError::IdleTimeout | BodyError::Upstream(_) => UPSTREAM_DISCONNECTED,
        }
    }

    pub fn retryable(&self) -> bool {
        !matches!(self, BodyError::TotalTimeout)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Termination {
    SseEvent,
    Http2Trailers,
    Close,
}

impl Termination {
    pub fn for_response(route: &Route, content_type: Option<&str>, error_trailers: bool) -> Self {
        let sse = route.protocol == Protocol::Sse
            || content_type.is_some_and(|value| {
                value
                    .trim_start()
                    .to_ascii_lowercase()
                    .starts_with("text/event-stream")
            });
        if sse {
            Termination::SseEvent
        } else if error_trailers {
            Termination::Http2Trailers
        } else {
            Termination::Close
        }
    }
}

pub fn accepts_error_trailers(version: Version, headers: &HeaderMap) -> bool {
    if version != Version::HTTP_2 {
        return false;
    }
    let te_trailers = headers
        .get_all(header::TE)
        .iter()
        .filter_map(|value| value.to_str().ok())
        .flat_map(|value| value.split(','))
        .filter_map(|token| token.split(';').next())
        .any(|token| token.trim().eq_ignore_ascii_case("trailers"));
    let grpc = headers
        .get(header::CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .is_some_and(|value| {
            value
                .trim_start()
                .to_ascii_lowercase()
                .starts_with("application/grpc")
        });
    te_trailers || grpc
}

pub struct StreamContext {
    pub state: Arc<ProxyState>,
    pub route_id: String,
    pub endpoint: Endpoint,
    pub termination: Termination,
    pub deferred_status: Option<u16>,
}

impl StreamContext {
    fn settle(&mut self, outcome: Outcome, label: &'static str) {
        let Some(status) = self.deferred_status.take() else {
            return;
        };
        let status = (outcome == Outcome::Success).then_some(status);
        self.state.runtime.finish_attempt(
            &self.route_id,
            &self.endpoint,
            outcome,
            label,
            status,
            &mut ThreadRng,
        );
    }

    fn settle_without_outcome(&mut self, label: &'static str) {
        if self.deferred_status.take().is_some() {
            self.state
                .runtime
                .count_request(&self.route_id, &self.endpoint, label);
        }
    }

    fn settle_failure(&mut self, error: &BodyError) {
        match error {
            BodyError::TotalTimeout => self.settle_without_outcome(TOTAL_TIMEOUT),
            BodyError::IdleTimeout | BodyError::Upstream(_) => {
                self.settle(Outcome::Transient, UPSTREAM_DISCONNECTED)
            }
        }
    }

    fn record_disconnect(&mut self, error: &BodyError) {
        self.settle_failure(error);
        tracing::warn!(
            route = %self.route_id,
            endpoint = %self.endpoint.id,
            provider = %self.endpoint.provider,
            reason = error.reason(),
            "stream ended before the upstream finished"
        );
    }

    fn terminal_frame(&self, error: &BodyError, at_boundary: bool) -> Option<Frame<Bytes>> {
        match self.termination {
            Termination::SseEvent => Some(Frame::data(sse_terminal_event(
                &self.endpoint.provider,
                error,
                at_boundary,
            ))),
            Termination::Http2Trailers => {
                let mut trailers = HeaderMap::new();
                trailers.insert(ERROR_HEADER, HeaderValue::from_static(error.kind()));
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

pub fn sse_terminal_event(provider: &str, error: &BodyError, at_boundary: bool) -> Bytes {
    let payload = serde_json::json!({
        "error": {
            "type": error.kind(),
            "retryable": error.retryable(),
            "provider": provider,
            "reason": error.reason(),
        }
    });
    let separator = if at_boundary { "" } else { "\n\n" };
    Bytes::from(format!("{separator}data: {payload}\n\ndata: [DONE]\n\n"))
}

pub fn last_event_boundary(bytes: &[u8]) -> Option<usize> {
    let mut boundary = None;
    let mut line_empty = true;
    let mut index = 0;
    while index < bytes.len() {
        let line_end = match bytes[index] {
            b'\r' if bytes.get(index + 1) == Some(&b'\n') => Some(index + 2),
            b'\r' | b'\n' => Some(index + 1),
            _ => None,
        };
        match line_end {
            Some(end) => {
                if line_empty {
                    boundary = Some(end);
                }
                line_empty = true;
                index = end;
            }
            None => {
                line_empty = false;
                index += 1;
            }
        }
    }
    boundary
}

#[derive(Debug, Default)]
pub struct EventFramer {
    held: BytesMut,
    passthrough: bool,
}

impl EventFramer {
    pub fn push(&mut self, data: Bytes) -> Option<Bytes> {
        if self.passthrough {
            return Some(data);
        }
        if self.held.is_empty() {
            return match last_event_boundary(&data) {
                Some(end) if end == data.len() => Some(data),
                Some(end) => {
                    self.held.extend_from_slice(&data[end..]);
                    Some(data.slice(..end))
                }
                None => {
                    self.held.extend_from_slice(&data);
                    self.overflow()
                }
            };
        }
        self.held.extend_from_slice(&data);
        match last_event_boundary(&self.held) {
            Some(end) => Some(self.held.split_to(end).freeze()),
            None => self.overflow(),
        }
    }

    pub fn flush(&mut self) -> Option<Bytes> {
        (!self.held.is_empty()).then(|| self.held.split().freeze())
    }

    pub fn discard(&mut self) {
        self.held.clear();
    }

    pub fn is_holding(&self) -> bool {
        !self.held.is_empty()
    }

    pub fn at_boundary(&self) -> bool {
        !self.passthrough
    }

    fn overflow(&mut self) -> Option<Bytes> {
        if self.held.len() <= MAX_HELD_EVENT_BYTES {
            return None;
        }
        self.passthrough = true;
        self.flush()
    }
}

pub struct TimedBody {
    inner: Incoming,
    first: Option<Frame<Bytes>>,
    idle: Duration,
    idle_timer: Pin<Box<Sleep>>,
    total_timer: Pin<Box<Sleep>>,
    context: Option<StreamContext>,
    events: Option<EventFramer>,
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
            events: None,
            committed: false,
            finished: false,
            _guard: guard,
        }
    }

    pub fn with_context(mut self, context: StreamContext) -> Self {
        if context.termination == Termination::SseEvent {
            self.events = Some(EventFramer::default());
        }
        self.context = Some(context);
        self
    }

    fn forward(&mut self, frame: Frame<Bytes>) -> Option<Frame<Bytes>> {
        let Some(events) = self.events.as_mut() else {
            return Some(frame);
        };
        match frame.into_data() {
            Ok(data) => events.push(data).map(Frame::data),
            Err(other) => match events.flush() {
                Some(held) => {
                    self.first = Some(other);
                    Some(Frame::data(held))
                }
                None => Some(other),
            },
        }
    }

    fn end(&mut self) -> Poll<Option<Result<Frame<Bytes>, BodyError>>> {
        self.finished = true;
        if let Some(context) = self.context.as_mut() {
            context.settle(Outcome::Success, Outcome::Success.label());
        }
        let held = self.events.as_mut().and_then(EventFramer::flush);
        Poll::Ready(held.map(|data| Ok(Frame::data(data))))
    }

    fn fail(&mut self, error: BodyError) -> Poll<Option<Result<Frame<Bytes>, BodyError>>> {
        self.finished = true;
        let Some(context) = self.context.as_mut() else {
            return Poll::Ready(Some(Err(error)));
        };
        if !self.committed {
            context.settle_failure(&error);
            return Poll::Ready(Some(Err(error)));
        }
        context.record_disconnect(&error);
        let at_boundary = match self.events.as_mut() {
            Some(events) => {
                events.discard();
                events.at_boundary()
            }
            None => false,
        };
        match context.terminal_frame(&error, at_boundary) {
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
        loop {
            let frame = match self.first.take() {
                Some(frame) => frame,
                None => match Pin::new(&mut self.inner).poll_frame(cx) {
                    Poll::Ready(Some(Ok(frame))) => {
                        let idle = self.idle;
                        self.idle_timer
                            .as_mut()
                            .reset(tokio::time::Instant::now() + idle);
                        frame
                    }
                    Poll::Ready(Some(Err(error))) => return self.fail(BodyError::Upstream(error)),
                    Poll::Ready(None) => return self.end(),
                    Poll::Pending => {
                        ready!(self.idle_timer.as_mut().poll(cx));
                        return self.fail(BodyError::IdleTimeout);
                    }
                },
            };
            self.committed = true;
            if let Some(frame) = self.forward(frame) {
                return Poll::Ready(Some(Ok(frame)));
            }
        }
    }

    fn is_end_stream(&self) -> bool {
        let holding = self.events.as_ref().is_some_and(EventFramer::is_holding);
        self.finished || (self.first.is_none() && !holding && self.inner.is_end_stream())
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

impl Drop for TimedBody {
    fn drop(&mut self) {
        let outcome = if self.committed {
            Outcome::Success
        } else {
            Outcome::ClientAbort
        };
        if let Some(context) = self.context.as_mut() {
            context.settle(outcome, outcome.label());
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sse_routes_and_event_stream_bodies_get_a_terminal_event() {
        let route = Route::default();
        assert_eq!(
            Termination::for_response(&route, Some("text/event-stream; charset=utf-8"), false),
            Termination::SseEvent
        );
        assert_eq!(
            Termination::for_response(&route, Some("application/json"), false),
            Termination::Close
        );
        assert_eq!(
            Termination::for_response(&route, Some("application/octet-stream"), true),
            Termination::Http2Trailers
        );
        let sse_route = Route {
            protocol: Protocol::Sse,
            ..Route::default()
        };
        assert_eq!(
            Termination::for_response(&sse_route, None, true),
            Termination::SseEvent
        );
    }

    fn request_headers(pairs: &[(&'static str, &'static str)]) -> HeaderMap {
        pairs
            .iter()
            .map(|(name, value)| {
                (
                    header::HeaderName::from_static(name),
                    HeaderValue::from_static(value),
                )
            })
            .collect()
    }

    #[test]
    fn error_trailers_need_http2_and_te_trailers_or_grpc() {
        let opted_in = [
            request_headers(&[("te", "trailers")]),
            request_headers(&[("te", "gzip, Trailers;q=1")]),
            request_headers(&[("content-type", "application/grpc")]),
            request_headers(&[("content-type", "Application/gRPC+proto")]),
        ];
        for headers in &opted_in {
            assert!(
                accepts_error_trailers(Version::HTTP_2, headers),
                "{headers:?}"
            );
            assert!(
                !accepts_error_trailers(Version::HTTP_11, headers),
                "{headers:?}"
            );
        }
        let plain = [
            request_headers(&[]),
            request_headers(&[("te", "gzip")]),
            request_headers(&[("content-type", "application/json")]),
        ];
        for headers in &plain {
            assert!(
                !accepts_error_trailers(Version::HTTP_2, headers),
                "{headers:?}"
            );
        }
    }

    #[test]
    fn terminal_event_starts_at_a_boundary_unless_the_client_is_mid_event() {
        let mid_event = sse_terminal_event("modal", &BodyError::IdleTimeout, false);
        assert!(mid_event.starts_with(b"\n\ndata: {"));
        let at_boundary = sse_terminal_event("modal", &BodyError::IdleTimeout, true);
        assert_eq!(&mid_event[2..], &at_boundary[..]);
    }

    fn terminal_error(error: &BodyError) -> serde_json::Value {
        let text = String::from_utf8(sse_terminal_event("modal", error, true).to_vec()).unwrap();
        assert!(text.starts_with("data: {"));
        assert!(text.ends_with("\n\ndata: [DONE]\n\n"));
        let payload = text
            .trim()
            .lines()
            .next()
            .unwrap()
            .strip_prefix("data: ")
            .unwrap();
        let parsed: serde_json::Value = serde_json::from_str(payload).unwrap();
        parsed["error"].clone()
    }

    #[test]
    fn terminal_event_names_the_provider_and_ends_with_done() {
        let error = terminal_error(&BodyError::IdleTimeout);
        assert_eq!(error["type"], UPSTREAM_DISCONNECTED);
        assert_eq!(error["retryable"], true);
        assert_eq!(error["provider"], "modal");
        assert_eq!(error["reason"], "idle_timeout");
    }

    #[test]
    fn total_timeout_is_its_own_non_retryable_error() {
        let error = terminal_error(&BodyError::TotalTimeout);
        assert_eq!(error["type"], TOTAL_TIMEOUT);
        assert_eq!(error["retryable"], false);
        assert_eq!(error["provider"], "modal");
        assert_eq!(error["reason"], TOTAL_TIMEOUT);
    }

    fn push_all(framer: &mut EventFramer, chunks: &[&'static str]) -> Vec<Bytes> {
        chunks
            .iter()
            .filter_map(|chunk| framer.push(Bytes::from_static(chunk.as_bytes())))
            .collect()
    }

    #[test]
    fn boundaries_accept_lf_crlf_and_cr_blank_lines() {
        assert_eq!(last_event_boundary(b"data: a\n\n"), Some(9));
        assert_eq!(last_event_boundary(b"data: a\r\n\r\n"), Some(11));
        assert_eq!(last_event_boundary(b"data: a\r\r"), Some(9));
        assert_eq!(
            last_event_boundary(b"data: a\n\ndata: b\n\ndata: c"),
            Some(18)
        );
        assert_eq!(last_event_boundary(b"data: a\r\ndata: b\r\n"), None);
        assert_eq!(last_event_boundary(b"data: a\n"), None);
        assert_eq!(last_event_boundary(b"data: {\"cho"), None);
    }

    #[test]
    fn complete_events_pass_through_without_copying() {
        let mut framer = EventFramer::default();
        let chunk = Bytes::from_static(b"data: a\n\ndata: b\n\n");
        let forwarded = framer.push(chunk.clone()).unwrap();
        assert_eq!(forwarded.as_ptr(), chunk.as_ptr());
        assert_eq!(forwarded, chunk);
        assert!(!framer.is_holding());
    }

    #[test]
    fn half_an_event_is_held_until_its_boundary_arrives() {
        let mut framer = EventFramer::default();
        let forwarded = push_all(
            &mut framer,
            &[
                "data: a\n\ndata: {\"cho",
                "ices\":[]}\n",
                "\ndata: c\r\n\r\n",
            ],
        );
        assert_eq!(
            forwarded,
            vec![
                Bytes::from_static(b"data: a\n\n"),
                Bytes::from_static(b"data: {\"choices\":[]}\n\ndata: c\r\n\r\n"),
            ]
        );
        assert!(!framer.is_holding());
    }

    #[test]
    fn disconnect_discards_the_partial_event_and_stays_at_a_boundary() {
        let mut framer = EventFramer::default();
        let forwarded = push_all(&mut framer, &["data: a\r\r", "data: {\"cho"]);
        assert_eq!(forwarded, vec![Bytes::from_static(b"data: a\r\r")]);
        assert!(framer.is_holding());
        framer.discard();
        assert!(framer.at_boundary());
        assert_eq!(framer.flush(), None);
    }

    #[test]
    fn clean_end_flushes_a_trailing_event_unchanged() {
        let mut framer = EventFramer::default();
        let forwarded = push_all(&mut framer, &["data: a\n\ndata: tail"]);
        assert_eq!(forwarded, vec![Bytes::from_static(b"data: a\n\n")]);
        assert_eq!(framer.flush(), Some(Bytes::from_static(b"data: tail")));
        assert_eq!(framer.flush(), None);
    }

    #[test]
    fn oversized_events_fall_back_to_passthrough() {
        let mut framer = EventFramer::default();
        let big = Bytes::from(vec![b'x'; MAX_HELD_EVENT_BYTES + 1]);
        assert_eq!(framer.push(big.clone()), Some(big));
        assert!(!framer.at_boundary());
        let rest = Bytes::from_static(b"still \n\n streaming");
        assert_eq!(framer.push(rest.clone()), Some(rest));
    }
}
