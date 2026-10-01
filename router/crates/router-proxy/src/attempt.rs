use bytes::Bytes;
use http::{HeaderMap, Method, Request, Response, Uri};
use http_body_util::Full;
use hyper::body::Incoming;
use hyper_util::client::legacy::connect::HttpConnector;
use hyper_util::client::legacy::Client;
use router_core::outcome::AttemptError;
use router_core::snapshot::Endpoint;
use std::time::{Duration, Instant};

pub type UpstreamClient = Client<HttpConnector, Full<Bytes>>;

pub fn build_client(connect_timeout: Duration) -> UpstreamClient {
    let mut connector = HttpConnector::new();
    connector.set_connect_timeout(Some(connect_timeout));
    connector.set_nodelay(true);
    Client::builder(hyper_util::rt::TokioExecutor::new())
        .pool_idle_timeout(Duration::from_secs(90))
        .build(connector)
}

pub struct AttemptOutcome {
    pub response: Option<Response<Incoming>>,
    pub error: Option<AttemptError>,
    pub ttft: Duration,
}

impl AttemptOutcome {
    pub fn status(&self) -> Option<u16> {
        self.response.as_ref().map(|r| r.status().as_u16())
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
    let uri = match upstream_uri(endpoint, path_and_query) {
        Ok(uri) => uri,
        Err(_) => {
            return AttemptOutcome {
                response: None,
                error: Some(AttemptError::Connect),
                ttft: started.elapsed(),
            }
        }
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
        Err(_) => {
            return AttemptOutcome {
                response: None,
                error: Some(AttemptError::Connect),
                ttft: started.elapsed(),
            }
        }
    };
    match tokio::time::timeout(first_byte_timeout, client.request(request)).await {
        Ok(Ok(response)) => AttemptOutcome {
            response: Some(response),
            error: None,
            ttft: started.elapsed(),
        },
        Ok(Err(error)) => AttemptOutcome {
            response: None,
            error: Some(if error.is_connect() {
                AttemptError::Connect
            } else {
                AttemptError::Reset
            }),
            ttft: started.elapsed(),
        },
        Err(_) => AttemptOutcome {
            response: None,
            error: Some(AttemptError::FirstByteTimeout),
            ttft: started.elapsed(),
        },
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
