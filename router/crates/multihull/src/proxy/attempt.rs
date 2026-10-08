use crate::core::outcome::AttemptError;
use crate::core::snapshot::Endpoint;
use crate::tls::TlsError;
use bytes::Bytes;
use http::{HeaderMap, Method, Request, Response, Uri};
use http_body_util::{BodyExt, Full};
use hyper::body::{Body, Frame, Incoming};
use std::path::Path;
use std::time::{Duration, Instant};

pub type UpstreamClient = crate::tls::HttpsClient;

pub fn build_client(
    connect_timeout: Duration,
    upstream_ca: Option<&Path>,
) -> Result<UpstreamClient, TlsError> {
    crate::tls::https_client(connect_timeout, upstream_ca)
}

pub struct UpstreamResponse {
    pub response: Response<Incoming>,
    pub first_frame: Option<Frame<Bytes>>,
}

impl UpstreamResponse {
    pub fn status(&self) -> u16 {
        self.response.status().as_u16()
    }

    pub fn body_pending(&self) -> bool {
        self.first_frame.is_some() && !self.response.body().is_end_stream()
    }
}

pub struct AttemptOutcome {
    pub response: Option<UpstreamResponse>,
    pub error: Option<AttemptError>,
    pub ttft: Duration,
}

impl AttemptOutcome {
    pub fn status(&self) -> Option<u16> {
        self.response.as_ref().map(UpstreamResponse::status)
    }

    fn failed(error: AttemptError, started: Instant) -> Self {
        Self {
            response: None,
            error: Some(error),
            ttft: started.elapsed(),
        }
    }
}

pub fn upstream_uri(endpoint: &Endpoint, path_and_query: &str) -> Result<Uri, http::Error> {
    let base = endpoint.url.trim_end_matches('/');
    let joined = format!("{base}{path_and_query}");
    joined.parse::<Uri>().map_err(http::Error::from)
}

const HOP_BY_HOP: &[&str] = &[
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
];

pub fn forwardable_headers(headers: &HeaderMap) -> HeaderMap {
    let mut out = HeaderMap::with_capacity(headers.len());
    for (name, value) in headers {
        let lower = name.as_str();
        if HOP_BY_HOP.contains(&lower)
            || lower == "host"
            || lower == "authorization"
            || lower == "content-length"
        {
            continue;
        }
        out.append(name.clone(), value.clone());
    }
    out
}

pub fn response_headers(headers: &HeaderMap) -> HeaderMap {
    let mut out = HeaderMap::with_capacity(headers.len());
    for (name, value) in headers {
        if HOP_BY_HOP.contains(&name.as_str()) {
            continue;
        }
        out.append(name.clone(), value.clone());
    }
    out
}

pub async fn send(
    client: &UpstreamClient,
    endpoint: &Endpoint,
    method: Method,
    path_and_query: &str,
    headers: &HeaderMap,
    body: Bytes,
    first_byte_timeout: Duration,
) -> AttemptOutcome {
    let started = Instant::now();
    let deadline = tokio::time::Instant::now() + first_byte_timeout;
    let uri = match upstream_uri(endpoint, path_and_query) {
        Ok(uri) => uri,
        Err(_) => return AttemptOutcome::failed(AttemptError::Connect, started),
    };
    let mut builder = Request::builder().method(method).uri(&uri);
    if let Some(map) = builder.headers_mut() {
        map.extend(forwardable_headers(headers));
        if let Some(authority) = uri.authority() {
            if let Ok(host) = authority.as_str().parse() {
                map.insert(http::header::HOST, host);
            }
        }
        for (name, value) in &endpoint.inject_headers {
            if let (Ok(name), Ok(value)) = (
                http::header::HeaderName::from_bytes(name.as_bytes()),
                http::header::HeaderValue::from_str(value),
            ) {
                map.insert(name, value);
            }
        }
    }
    let request = match builder.body(Full::new(body)) {
        Ok(request) => request,
        Err(_) => return AttemptOutcome::failed(AttemptError::Connect, started),
    };
    let mut response = match tokio::time::timeout_at(deadline, client.request(request)).await {
        Ok(Ok(response)) => response,
        Ok(Err(error)) if error.is_connect() => {
            return AttemptOutcome::failed(AttemptError::Connect, started)
        }
        Ok(Err(_)) => return AttemptOutcome::failed(AttemptError::Reset, started),
        Err(_) => return AttemptOutcome::failed(AttemptError::FirstByteTimeout, started),
    };
    let first_frame = match tokio::time::timeout_at(deadline, response.body_mut().frame()).await {
        Ok(Some(Ok(frame))) => Some(frame),
        Ok(None) => None,
        Ok(Some(Err(_))) => return AttemptOutcome::failed(AttemptError::Reset, started),
        Err(_) => return AttemptOutcome::failed(AttemptError::FirstByteTimeout, started),
    };
    AttemptOutcome {
        response: Some(UpstreamResponse {
            response,
            first_frame,
        }),
        error: None,
        ttft: started.elapsed(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use http::header::{HeaderName, HeaderValue};

    #[test]
    fn upstream_uri_joins_base_and_path() {
        let endpoint = Endpoint {
            url: "http://10.0.0.1:8000/".into(),
            ..Default::default()
        };
        let uri = upstream_uri(&endpoint, "/v1/chat?x=1").unwrap();
        assert_eq!(uri.to_string(), "http://10.0.0.1:8000/v1/chat?x=1");
    }

    #[test]
    fn hop_by_hop_host_and_authorization_are_stripped() {
        let mut headers = HeaderMap::new();
        headers.insert(
            HeaderName::from_static("connection"),
            HeaderValue::from_static("keep-alive"),
        );
        headers.insert(
            HeaderName::from_static("host"),
            HeaderValue::from_static("api.example.com"),
        );
        headers.insert(
            HeaderName::from_static("authorization"),
            HeaderValue::from_static("Bearer hull_x_y"),
        );
        headers.insert(
            HeaderName::from_static("content-type"),
            HeaderValue::from_static("application/json"),
        );
        let out = forwardable_headers(&headers);
        assert_eq!(out.len(), 1);
        assert!(out.contains_key("content-type"));
        let back = response_headers(&headers);
        assert!(!back.contains_key("connection"));
        assert!(back.contains_key("authorization"));
    }
}
