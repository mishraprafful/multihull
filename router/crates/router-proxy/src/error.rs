use bytes::Bytes;
use http::{header, Response, StatusCode};
use http_body_util::{BodyExt, Full};

use crate::body::ProxyBody;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ProxyError {
    NoRoute,
    Unauthorized,
    BodyTooLarge,
    BodyRead,
    NoHealthyUpstream,
    SessionLost,
    QueueOverflow,
    UpstreamUnavailable,
    UpstreamTimeout,
    Internal,
}

impl ProxyError {
    pub fn status(&self) -> StatusCode {
        match self {
            ProxyError::NoRoute => StatusCode::NOT_FOUND,
            ProxyError::Unauthorized => StatusCode::UNAUTHORIZED,
            ProxyError::BodyTooLarge => StatusCode::PAYLOAD_TOO_LARGE,
            ProxyError::BodyRead => StatusCode::BAD_REQUEST,
            ProxyError::NoHealthyUpstream => StatusCode::SERVICE_UNAVAILABLE,
            ProxyError::SessionLost => StatusCode::SERVICE_UNAVAILABLE,
            ProxyError::QueueOverflow => StatusCode::TOO_MANY_REQUESTS,
            ProxyError::UpstreamUnavailable => StatusCode::BAD_GATEWAY,
            ProxyError::UpstreamTimeout => StatusCode::GATEWAY_TIMEOUT,
            ProxyError::Internal => StatusCode::INTERNAL_SERVER_ERROR,
        }
    }

    pub fn kind(&self) -> &'static str {
        match self {
            ProxyError::NoRoute => "no_route",
            ProxyError::Unauthorized => "unauthorized",
            ProxyError::BodyTooLarge => "body_too_large",
            ProxyError::BodyRead => "body_read_failed",
            ProxyError::NoHealthyUpstream => "no_healthy_upstream",
            ProxyError::SessionLost => "session_lost",
            ProxyError::QueueOverflow => "queue_overflow",
            ProxyError::UpstreamUnavailable => "upstream_unavailable",
            ProxyError::UpstreamTimeout => "upstream_timeout",
            ProxyError::Internal => "internal",
        }
    }

    pub fn into_response(self, attempts: u32) -> Response<ProxyBody> {
        let body = format!(
            r#"{{"error":{{"type":"{}","message":"{}","attempts":{}}}}}"#,
            self.kind(),
            self.status().canonical_reason().unwrap_or("error"),
            attempts
        );
        let mut builder = Response::builder()
            .status(self.status())
            .header(header::CONTENT_TYPE, "application/json")
            .header("x-hull-attempts", attempts.to_string());
        if matches!(
            self,
            ProxyError::NoHealthyUpstream
                | ProxyError::QueueOverflow
                | ProxyError::UpstreamUnavailable
                | ProxyError::UpstreamTimeout
        ) {
            builder = builder.header(header::RETRY_AFTER, "1");
        }
        builder
            .body(
                Full::new(Bytes::from(body))
                    .map_err(|never| match never {})
                    .boxed(),
            )
            .expect("static response is valid")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn retryable_errors_carry_retry_after() {
        let response = ProxyError::NoHealthyUpstream.into_response(2);
        assert_eq!(response.status(), StatusCode::SERVICE_UNAVAILABLE);
        assert_eq!(response.headers().get(header::RETRY_AFTER).unwrap(), "1");
        assert_eq!(response.headers().get("x-hull-attempts").unwrap(), "2");
        let response = ProxyError::NoRoute.into_response(0);
        assert!(response.headers().get(header::RETRY_AFTER).is_none());
    }

    #[test]
    fn queue_overflow_is_429_with_retry_after() {
        let response = ProxyError::QueueOverflow.into_response(0);
        assert_eq!(response.status(), StatusCode::TOO_MANY_REQUESTS);
        assert_eq!(response.headers().get(header::RETRY_AFTER).unwrap(), "1");
    }

    #[test]
    fn session_lost_is_503_without_retry_after() {
        let response = ProxyError::SessionLost.into_response(0);
        assert_eq!(response.status(), StatusCode::SERVICE_UNAVAILABLE);
        assert!(response.headers().get(header::RETRY_AFTER).is_none());
        assert_eq!(ProxyError::SessionLost.kind(), "session_lost");
    }
}
