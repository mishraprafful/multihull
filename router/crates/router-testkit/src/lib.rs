use bytes::Bytes;
use http_body_util::{combinators::BoxBody, BodyExt, Full, StreamBody};
use hyper::body::{Frame, Incoming};
use hyper::server::conn::http1;
use hyper::service::service_fn;
use hyper::{Request, Response, StatusCode};
use hyper_util::rt::TokioIo;
use std::convert::Infallible;
use std::net::SocketAddr;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::Duration;
use tokio::net::TcpListener;
use tokio::sync::{mpsc, watch};
use tokio_stream::wrappers::ReceiverStream;

#[derive(Clone, Debug)]
pub struct MockUpstreamConfig {
    pub status: StatusCode,
    pub ttft_delay: Duration,
    pub body: Bytes,
    pub sse_chunks: Vec<String>,
    pub sse_chunk_interval: Duration,
    pub drop_connection: bool,
}

impl Default for MockUpstreamConfig {
    fn default() -> Self {
        Self {
            status: StatusCode::OK,
            ttft_delay: Duration::ZERO,
            body: Bytes::from_static(b"{\"ok\":true}"),
            sse_chunks: Vec::new(),
            sse_chunk_interval: Duration::from_millis(5),
            drop_connection: false,
        }
    }
}

impl MockUpstreamConfig {
    pub fn with_status(mut self, status: StatusCode) -> Self {
        self.status = status;
        self
    }

    pub fn with_ttft_delay(mut self, delay: Duration) -> Self {
        self.ttft_delay = delay;
        self
    }

    pub fn with_body(mut self, body: impl Into<Bytes>) -> Self {
        self.body = body.into();
        self
    }

    pub fn with_sse_chunks(mut self, chunks: Vec<String>) -> Self {
        self.sse_chunks = chunks;
        self
    }

    pub fn dropping_connections(mut self) -> Self {
        self.drop_connection = true;
        self
    }
}

pub struct MockUpstream {
    addr: SocketAddr,
    requests: Arc<AtomicUsize>,
    config_tx: watch::Sender<MockUpstreamConfig>,
    shutdown_tx: Option<watch::Sender<bool>>,
}

impl MockUpstream {
    pub async fn start(config: MockUpstreamConfig) -> std::io::Result<Self> {
        let listener = TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, 0)).await?;
        let addr = listener.local_addr()?;
        let requests = Arc::new(AtomicUsize::new(0));
        let (config_tx, config_rx) = watch::channel(config);
        let (shutdown_tx, mut shutdown_rx) = watch::channel(false);
        let counter = requests.clone();
        tokio::spawn(async move {
            loop {
                tokio::select! {
                    accepted = listener.accept() => {
                        let Ok((stream, _)) = accepted else { break };
                        if config_rx.borrow().drop_connection {
                            counter.fetch_add(1, Ordering::SeqCst);
                            drop(stream);
                            continue;
                        }
                        let config_rx = config_rx.clone();
                        let counter = counter.clone();
                        tokio::spawn(async move {
                            let service = service_fn(move |req| {
                                let config = config_rx.borrow().clone();
                                let counter = counter.clone();
                                async move { respond(req, config, counter).await }
                            });
                            let _ = http1::Builder::new()
                                .serve_connection(TokioIo::new(stream), service)
                                .await;
                        });
                    }
                    _ = shutdown_rx.changed() => break,
                }
            }
        });
        Ok(Self {
            addr,
            requests,
            config_tx,
            shutdown_tx: Some(shutdown_tx),
        })
    }

    pub fn addr(&self) -> SocketAddr {
        self.addr
    }

    pub fn url(&self) -> String {
        format!("http://{}", self.addr)
    }

    pub fn request_count(&self) -> usize {
        self.requests.load(Ordering::SeqCst)
    }

    pub fn reconfigure(&self, config: MockUpstreamConfig) {
        let _ = self.config_tx.send(config);
    }
}

impl Drop for MockUpstream {
    fn drop(&mut self) {
        if let Some(tx) = self.shutdown_tx.take() {
            let _ = tx.send(true);
        }
    }
}

async fn respond(
    _req: Request<Incoming>,
    config: MockUpstreamConfig,
    counter: Arc<AtomicUsize>,
) -> Result<Response<BoxBody<Bytes, Infallible>>, Infallible> {
    counter.fetch_add(1, Ordering::SeqCst);
    if config.ttft_delay > Duration::ZERO {
        tokio::time::sleep(config.ttft_delay).await;
    }
    if config.sse_chunks.is_empty() {
        let body = Full::new(config.body).boxed();
        let response = Response::builder()
            .status(config.status)
            .header("content-type", "application/json")
            .body(body)
            .expect("valid response");
        return Ok(response);
    }
    let (tx, rx) = mpsc::channel::<Result<Frame<Bytes>, Infallible>>(16);
    let interval = config.sse_chunk_interval;
    let chunks = config.sse_chunks.clone();
    tokio::spawn(async move {
        for chunk in chunks {
            let frame = Frame::data(Bytes::from(format!("data: {chunk}\n\n")));
            if tx.send(Ok(frame)).await.is_err() {
                return;
            }
            tokio::time::sleep(interval).await;
        }
        let _ = tx
            .send(Ok(Frame::data(Bytes::from_static(b"data: [DONE]\n\n"))))
            .await;
    });
    let body = StreamBody::new(ReceiverStream::new(rx)).boxed();
    let response = Response::builder()
        .status(config.status)
        .header("content-type", "text/event-stream")
        .body(body)
        .expect("valid response");
    Ok(response)
}
