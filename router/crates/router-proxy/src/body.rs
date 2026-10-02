use bytes::Bytes;
use http_body_util::combinators::BoxBody;
use hyper::body::{Body, Frame, Incoming};
use std::future::Future;
use std::pin::Pin;
use std::task::{ready, Context, Poll};
use std::time::{Duration, Instant};
use tokio::time::Sleep;

use crate::runtime::OutstandingGuard;

pub type ProxyBody = BoxBody<Bytes, BodyError>;

#[derive(Debug, thiserror::Error)]
pub enum BodyError {
    #[error("upstream idle timeout")]
    IdleTimeout,
    #[error("upstream total timeout")]
    TotalTimeout,
    #[error("upstream body error: {0}")]
    Upstream(#[from] hyper::Error),
}

pub struct TimedBody {
    inner: Incoming,
    first: Option<Frame<Bytes>>,
    idle: Duration,
    deadline: Instant,
    idle_timer: Pin<Box<Sleep>>,
    total_timer: Pin<Box<Sleep>>,
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
            deadline,
            idle_timer: Box::pin(tokio::time::sleep(idle)),
            total_timer: Box::pin(tokio::time::sleep_until(deadline.into())),
            _guard: guard,
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
        if self.total_timer.as_mut().poll(cx).is_ready() {
            return Poll::Ready(Some(Err(BodyError::TotalTimeout)));
        }
        if let Some(frame) = self.first.take() {
            return Poll::Ready(Some(Ok(frame)));
        }
        match Pin::new(&mut self.inner).poll_frame(cx) {
            Poll::Ready(frame) => {
                let idle = self.idle;
                self.idle_timer
                    .as_mut()
                    .reset(tokio::time::Instant::now() + idle);
                Poll::Ready(frame.map(|result| result.map_err(BodyError::from)))
            }
            Poll::Pending => {
                ready!(self.idle_timer.as_mut().poll(cx));
                Poll::Ready(Some(Err(BodyError::IdleTimeout)))
            }
        }
    }

    fn is_end_stream(&self) -> bool {
        self.first.is_none() && (self.inner.is_end_stream() || Instant::now() >= self.deadline)
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
        if let Some(upper) = inner.upper() {
            hint.set_upper(upper + buffered);
        }
        hint
    }
}
