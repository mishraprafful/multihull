mod common;

use bytes::Bytes;
use common::{endpoint, start_proxy};
use http_body_util::{BodyExt, Empty};
use hyper::body::Frame;
use hyper::Request;
use hyper_util::client::legacy::Client;
use hyper_util::rt::TokioExecutor;
use router_proxy::body::{ERROR_HEADER, ERROR_REASON_HEADER, UPSTREAM_DISCONNECTED};
use router_testkit::{MockUpstream, MockUpstreamConfig};
use std::time::Duration;

fn chunks() -> Vec<String> {
    (1..=5)
        .map(|n| format!("{{\"id\":\"c\",\"n\":{n}}}"))
        .collect()
}

async fn collect_frames(body: hyper::body::Incoming) -> (String, Option<http::HeaderMap>) {
    let (frames, trailers) = collect_data_frames(body).await;
    (frames.concat(), trailers)
}

async fn collect_data_frames(
    body: hyper::body::Incoming,
) -> (Vec<String>, Option<http::HeaderMap>) {
    let mut body = body;
    let mut frames = Vec::new();
    let mut trailers = None;
    while let Some(frame) = tokio::time::timeout(Duration::from_secs(5), body.frame())
        .await
        .expect("frame within timeout")
    {
        let frame: Frame<Bytes> = frame.expect("body ends cleanly");
        if frame.is_data() {
            frames.push(String::from_utf8_lossy(&frame.into_data().unwrap()).into_owned());
        } else if frame.is_trailers() {
            trailers = Some(frame.into_trailers().unwrap());
        }
    }
    (frames, trailers)
}

fn raw_upstream(chunks: &[&str]) -> MockUpstreamConfig {
    MockUpstreamConfig::default()
        .with_sse_chunks(chunks.iter().map(|chunk| chunk.to_string()).collect())
        .sending_raw_chunks()
}

async fn sse_frames_through_proxy(config: MockUpstreamConfig) -> Vec<String> {
    let upstream = MockUpstream::start(config).await.unwrap();
    let proxy = start_proxy(vec![endpoint("a", "modal", upstream.url(), 1)]).await;
    let client: Client<_, Empty<Bytes>> = Client::builder(TokioExecutor::new()).build_http();
    let request = Request::get(proxy.url("/v1/chat/completions"))
        .body(Empty::new())
        .unwrap();
    let response = client.request(request).await.unwrap();
    assert_eq!(response.status(), 200);
    let (frames, trailers) = collect_data_frames(response.into_body()).await;
    assert!(trailers.is_none());
    frames
}

fn terminal_payload(event: &str) -> serde_json::Value {
    serde_json::from_str(event.strip_prefix("data: ").unwrap()).unwrap()
}

fn ends_at_event_boundary(frame: &str) -> bool {
    frame.ends_with("\n\n") || frame.ends_with("\r\n\r\n") || frame.ends_with("\r\r")
}

#[tokio::test]
async fn sse_disconnect_mid_stream_ends_with_a_terminal_event_and_done() {
    let upstream = MockUpstream::start(
        MockUpstreamConfig::default()
            .with_sse_chunks(chunks())
            .dropping_sse_after(2),
    )
    .await
    .unwrap();
    let target = endpoint("a", "modal", upstream.url(), 1);
    let proxy = start_proxy(vec![target.clone()]).await;
    let client: Client<_, Empty<Bytes>> = Client::builder(TokioExecutor::new()).build_http();
    let request = Request::get(proxy.url("/v1/chat/completions"))
        .body(Empty::new())
        .unwrap();
    let response = client.request(request).await.unwrap();
    assert_eq!(response.status(), 200);
    assert_eq!(response.headers().get("x-hull-provider").unwrap(), "modal");

    let (text, trailers) = collect_frames(response.into_body()).await;
    assert!(trailers.is_none());
    let frames: Vec<&str> = text
        .split("\n\n")
        .filter(|frame| !frame.is_empty())
        .collect();
    assert_eq!(frames.len(), 4, "{text}");
    assert_eq!(frames[0], "data: {\"id\":\"c\",\"n\":1}");
    assert_eq!(frames[1], "data: {\"id\":\"c\",\"n\":2}");
    let terminal: serde_json::Value =
        serde_json::from_str(frames[2].strip_prefix("data: ").unwrap()).unwrap();
    assert_eq!(terminal["error"]["type"], UPSTREAM_DISCONNECTED);
    assert_eq!(terminal["error"]["retryable"], true);
    assert_eq!(terminal["error"]["provider"], "modal");
    assert_eq!(terminal["error"]["reason"], "disconnect");
    assert_eq!(frames[3], "data: [DONE]");
    assert!(!text.contains("\"n\":3"));
    assert_eq!(upstream.request_count(), 1);
    assert_eq!(
        proxy.state.runtime.status(&target).unwrap().circuit,
        "closed"
    );
}

async fn http2_binary_disconnect(headers: &[(&str, &str)]) -> hyper::body::Incoming {
    let upstream = MockUpstream::start(
        MockUpstreamConfig::default()
            .with_sse_chunks(chunks())
            .dropping_sse_after(1)
            .with_stream_content_type("application/octet-stream"),
    )
    .await
    .unwrap();
    let proxy = start_proxy(vec![endpoint("a", "runpod", upstream.url(), 1)]).await;
    let client: Client<_, Empty<Bytes>> = Client::builder(TokioExecutor::new())
        .http2_only(true)
        .build_http();
    let mut request = Request::get(proxy.url("/v1/stream"));
    for (name, value) in headers {
        request = request.header(*name, *value);
    }
    let response = client
        .request(request.body(Empty::new()).unwrap())
        .await
        .unwrap();
    assert_eq!(response.version(), http::Version::HTTP_2);
    assert_eq!(response.status(), 200);
    response.into_body()
}

#[tokio::test]
async fn http2_binary_disconnect_ends_with_an_error_trailer_when_the_client_opts_in() {
    for headers in [
        [("te", "trailers")],
        [("content-type", "application/grpc+proto")],
    ] {
        let body = http2_binary_disconnect(&headers).await;
        let (text, trailers) = collect_frames(body).await;
        assert_eq!(text, "data: {\"id\":\"c\",\"n\":1}\n\n");
        let trailers = trailers.expect("error trailer");
        assert_eq!(trailers.get(ERROR_HEADER).unwrap(), UPSTREAM_DISCONNECTED);
        assert_eq!(trailers.get(ERROR_REASON_HEADER).unwrap(), "disconnect");
    }
}

#[tokio::test]
async fn http2_binary_disconnect_resets_the_stream_without_trailers_opt_in() {
    let mut body = http2_binary_disconnect(&[]).await;
    let mut text = String::new();
    let error = loop {
        let frame = tokio::time::timeout(Duration::from_secs(5), body.frame())
            .await
            .expect("frame within timeout")
            .expect("stream is reset, not ended cleanly");
        match frame {
            Ok(frame) => {
                assert!(!frame.is_trailers(), "no trailer without te: trailers");
                if let Ok(data) = frame.into_data() {
                    text.push_str(&String::from_utf8_lossy(&data));
                }
            }
            Err(error) => break error,
        }
    };
    assert_eq!(text, "data: {\"id\":\"c\",\"n\":1}\n\n");
    let described = format!("{error:?}");
    assert!(
        described.contains("Reset"),
        "expected RST_STREAM, got {described}"
    );
}

#[tokio::test]
async fn http1_binary_disconnect_still_closes_the_connection() {
    let upstream = MockUpstream::start(
        MockUpstreamConfig::default()
            .with_sse_chunks(chunks())
            .dropping_sse_after(1)
            .with_stream_content_type("application/octet-stream"),
    )
    .await
    .unwrap();
    let proxy = start_proxy(vec![endpoint("a", "runpod", upstream.url(), 1)]).await;
    let client: Client<_, Empty<Bytes>> = Client::builder(TokioExecutor::new()).build_http();
    let request = Request::get(proxy.url("/v1/stream"))
        .body(Empty::new())
        .unwrap();
    let response = client.request(request).await.unwrap();
    assert_eq!(response.status(), 200);
    let collected = tokio::time::timeout(Duration::from_secs(5), response.into_body().collect())
        .await
        .unwrap();
    assert!(
        collected.is_err(),
        "http/1 non-sse streams are cut, not decorated"
    );
}

#[tokio::test]
async fn sse_disconnect_after_half_an_event_drops_the_partial_event() {
    let frames = sse_frames_through_proxy(
        raw_upstream(&[
            "data: {\"id\":\"c\",\"n\":1}\n\ndata: {\"id\":\"c\",\"cho",
            "ices\":[]}\n\n",
        ])
        .dropping_sse_after(1),
    )
    .await;
    let text = frames.concat();
    assert!(!text.contains("cho"), "{text:?}");
    let events: Vec<&str> = text.split("\n\n").collect();
    assert_eq!(events.len(), 4, "{text:?}");
    assert_eq!(events[0], "data: {\"id\":\"c\",\"n\":1}");
    let terminal = terminal_payload(events[1]);
    assert_eq!(terminal["error"]["type"], UPSTREAM_DISCONNECTED);
    assert_eq!(terminal["error"]["reason"], "disconnect");
    assert_eq!(events[2], "data: [DONE]");
    assert_eq!(events[3], "");
}

#[tokio::test]
async fn sse_disconnect_after_a_crlf_event_resumes_at_the_boundary() {
    let frames = sse_frames_through_proxy(
        raw_upstream(&["data: {\"n\":1}\r\n\r\ndata: {\"n\":", "2}\r\n\r\n"]).dropping_sse_after(1),
    )
    .await;
    let text = frames.concat();
    let rest = text
        .strip_prefix("data: {\"n\":1}\r\n\r\n")
        .unwrap_or_else(|| panic!("{text:?}"));
    let (terminal, done) = rest.split_once("\n\n").unwrap();
    assert_eq!(
        terminal_payload(terminal)["error"]["type"],
        UPSTREAM_DISCONNECTED
    );
    assert_eq!(done, "data: [DONE]\n\n");
}

#[tokio::test]
async fn sse_events_split_across_chunks_are_forwarded_once_complete() {
    let chunks = [
        "data: {\"n\":",
        "1}\n\n",
        "data: {\"n\":2}\r\n\r\n",
        "data: {\"n\":3}\r",
        "\r",
        "data: [DONE]\n\n",
    ];
    let frames = sse_frames_through_proxy(raw_upstream(&chunks)).await;
    assert_eq!(frames.concat(), chunks.concat());
    assert!(
        frames.iter().all(|frame| ends_at_event_boundary(frame)),
        "{frames:?}"
    );
    assert!(
        frames.contains(&"data: {\"n\":1}\n\n".to_string()),
        "{frames:?}"
    );
}

#[tokio::test]
async fn sse_clean_end_flushes_a_trailing_event_without_a_blank_line() {
    let chunks = ["data: {\"n\":1}\n\ndata: tail"];
    let frames = sse_frames_through_proxy(raw_upstream(&chunks)).await;
    assert_eq!(frames.concat(), chunks.concat());
}
