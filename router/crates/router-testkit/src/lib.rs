use bytes::Bytes;
use http_body_util::{combinators::BoxBody, BodyExt, Full, StreamBody};
use hyper::body::{Frame, Incoming};
use hyper::service::service_fn;
use hyper::{Request, Response, StatusCode};
use hyper_util::rt::{TokioExecutor, TokioIo};
use hyper_util::server::conn::auto;
use std::convert::Infallible;
use std::net::SocketAddr;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::Duration;
use tokio::net::TcpListener;
use tokio::sync::{mpsc, watch};
use tokio_rustls::TlsAcceptor;
use tokio_stream::wrappers::ReceiverStream;

#[derive(Clone, Debug)]
pub struct MockUpstreamConfig {
    pub status: StatusCode,
    pub ttft_delay: Duration,
    pub body: Bytes,
    pub sse_chunks: Vec<String>,
    pub sse_chunk_interval: Duration,
    pub sse_first_chunk_delay: Duration,
    pub drop_connection: bool,
    pub etag: Option<String>,
}

impl Default for MockUpstreamConfig {
    fn default() -> Self {
        Self {
            status: StatusCode::OK,
            ttft_delay: Duration::ZERO,
            body: Bytes::from_static(b"{\"ok\":true}"),
            sse_chunks: Vec::new(),
            sse_chunk_interval: Duration::from_millis(5),
            sse_first_chunk_delay: Duration::ZERO,
            drop_connection: false,
            etag: None,
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

    pub fn with_sse_first_chunk_delay(mut self, delay: Duration) -> Self {
        self.sse_first_chunk_delay = delay;
        self
    }

    pub fn dropping_connections(mut self) -> Self {
        self.drop_connection = true;
        self
    }

    pub fn with_etag(mut self, etag: impl Into<String>) -> Self {
        self.etag = Some(etag.into());
        self
    }
}

pub struct MockUpstream {
    addr: SocketAddr,
    tls: bool,
    requests: Arc<AtomicUsize>,
    not_modified: Arc<AtomicUsize>,
    config_tx: watch::Sender<MockUpstreamConfig>,
    shutdown_tx: Option<watch::Sender<bool>>,
}

impl MockUpstream {
    pub async fn start(config: MockUpstreamConfig) -> std::io::Result<Self> {
        Self::start_with_tls(config, None).await
    }

    pub async fn start_tls(
        config: MockUpstreamConfig,
        cert_pem: &[u8],
        key_pem: &[u8],
    ) -> std::io::Result<Self> {
        let server_config = router_tls::server_config_from_pem(cert_pem, key_pem)
            .map_err(|error| std::io::Error::new(std::io::ErrorKind::InvalidInput, error))?;
        Self::start_with_tls(config, Some(TlsAcceptor::from(Arc::new(server_config)))).await
    }

    async fn start_with_tls(
        config: MockUpstreamConfig,
        acceptor: Option<TlsAcceptor>,
    ) -> std::io::Result<Self> {
        let listener = TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, 0)).await?;
        let addr = listener.local_addr()?;
        let requests = Arc::new(AtomicUsize::new(0));
        let not_modified = Arc::new(AtomicUsize::new(0));
        let (config_tx, config_rx) = watch::channel(config);
        let (shutdown_tx, mut shutdown_rx) = watch::channel(false);
        let counter = requests.clone();
        let not_modified_counter = not_modified.clone();
        let tls = acceptor.is_some();
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
                        let not_modified_counter = not_modified_counter.clone();
                        let acceptor = acceptor.clone();
                        tokio::spawn(async move {
                            let service = service_fn(move |req| {
                                let config = config_rx.borrow().clone();
                                let counters = (counter.clone(), not_modified_counter.clone());
                                async move { respond(req, config, counters).await }
                            });
                            match acceptor {
                                Some(acceptor) => {
                                    let Ok(stream) = acceptor.accept(stream).await else { return };
                                    let _ = auto::Builder::new(TokioExecutor::new())
                                        .serve_connection(TokioIo::new(stream), service)
                                        .await;
                                }
                                None => {
                                    let _ = auto::Builder::new(TokioExecutor::new())
                                        .serve_connection(TokioIo::new(stream), service)
                                        .await;
                                }
                            }
                        });
                    }
                    _ = shutdown_rx.changed() => break,
                }
            }
        });
        Ok(Self {
            addr,
            tls,
            requests,
            not_modified,
            config_tx,
            shutdown_tx: Some(shutdown_tx),
        })
    }

    pub fn not_modified_count(&self) -> usize {
        self.not_modified.load(Ordering::SeqCst)
    }

    pub fn addr(&self) -> SocketAddr {
        self.addr
    }

    pub fn url(&self) -> String {
        let scheme = if self.tls { "https" } else { "http" };
        format!("{scheme}://localhost:{}", self.addr.port())
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
    req: Request<Incoming>,
    config: MockUpstreamConfig,
    (counter, not_modified): (Arc<AtomicUsize>, Arc<AtomicUsize>),
) -> Result<Response<BoxBody<Bytes, Infallible>>, Infallible> {
    counter.fetch_add(1, Ordering::SeqCst);
    if config.ttft_delay > Duration::ZERO {
        tokio::time::sleep(config.ttft_delay).await;
    }
    if let Some(etag) = &config.etag {
        let matches = req
            .headers()
            .get("if-none-match")
            .and_then(|value| value.to_str().ok())
            .is_some_and(|value| value == etag);
        if matches {
            not_modified.fetch_add(1, Ordering::SeqCst);
            let response = Response::builder()
                .status(StatusCode::NOT_MODIFIED)
                .header("etag", etag)
                .body(Full::new(Bytes::new()).boxed())
                .expect("valid response");
            return Ok(response);
        }
    }
    if config.sse_chunks.is_empty() {
        let body = Full::new(config.body).boxed();
        let mut builder = Response::builder()
            .status(config.status)
            .header("content-type", "application/json");
        if let Some(etag) = &config.etag {
            builder = builder.header("etag", etag);
        }
        let response = builder.body(body).expect("valid response");
        return Ok(response);
    }
    let (tx, rx) = mpsc::channel::<Result<Frame<Bytes>, Infallible>>(16);
    let interval = config.sse_chunk_interval;
    let first_chunk_delay = config.sse_first_chunk_delay;
    let chunks = config.sse_chunks.clone();
    tokio::spawn(async move {
        tokio::time::sleep(first_chunk_delay).await;
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
