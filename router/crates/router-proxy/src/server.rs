use hyper::service::service_fn;
use hyper_util::rt::{TokioExecutor, TokioIo};
use hyper_util::server::conn::auto;
use hyper_util::server::graceful::GracefulShutdown;
use router_tls::TlsReloader;
use std::future::Future;
use std::net::SocketAddr;
use std::pin::pin;
use std::sync::Arc;
use std::time::Duration;
use tokio::io::{AsyncRead, AsyncWrite};
use tokio::net::TcpListener;

use crate::handler::handle;
use crate::state::ProxyState;

const DRAIN_TIMEOUT: Duration = Duration::from_secs(600);
const TLS_HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(10);

pub async fn serve(
    state: Arc<ProxyState>,
    listener: TcpListener,
    shutdown: impl Future<Output = ()>,
) -> std::io::Result<()> {
    let graceful = GracefulShutdown::new();
    let builder = Arc::new(auto::Builder::new(TokioExecutor::new()));
    let mut shutdown = pin!(shutdown);
    loop {
        tokio::select! {
            accepted = listener.accept() => {
                let (stream, peer) = accepted?;
                spawn_connection(&graceful, builder.clone(), state.clone(), stream, peer);
            }
            _ = &mut shutdown => break,
        }
    }
    drain(graceful).await;
    Ok(())
}

pub async fn serve_tls(
    state: Arc<ProxyState>,
    listener: TcpListener,
    tls: Arc<TlsReloader>,
    shutdown: impl Future<Output = ()>,
) -> std::io::Result<()> {
    let graceful = Arc::new(GracefulShutdown::new());
    let builder = Arc::new(auto::Builder::new(TokioExecutor::new()));
    let mut shutdown = pin!(shutdown);
    loop {
        tokio::select! {
            accepted = listener.accept() => {
                let (stream, peer) = accepted?;
                let acceptor = tls.acceptor();
                let graceful = graceful.clone();
                let builder = builder.clone();
                let state = state.clone();
                tokio::spawn(async move {
                    match tokio::time::timeout(TLS_HANDSHAKE_TIMEOUT, acceptor.accept(stream)).await {
                        Ok(Ok(stream)) => {
                            spawn_connection(&graceful, builder, state, stream, peer);
                        }
                        Ok(Err(error)) => tracing::debug!(%peer, %error, "tls handshake failed"),
                        Err(_) => tracing::debug!(%peer, "tls handshake timed out"),
                    }
                });
            }
            _ = &mut shutdown => break,
        }
    }
    match Arc::try_unwrap(graceful) {
        Ok(graceful) => drain(graceful).await,
        Err(_) => tokio::time::sleep(DRAIN_TIMEOUT).await,
    }
    Ok(())
}

fn spawn_connection<IO>(
    graceful: &GracefulShutdown,
    builder: Arc<auto::Builder<TokioExecutor>>,
    state: Arc<ProxyState>,
    stream: IO,
    peer: SocketAddr,
) where
    IO: AsyncRead + AsyncWrite + Unpin + Send + 'static,
{
    let service = service_fn(move |request| {
        let state = state.clone();
        async move { Ok::<_, std::convert::Infallible>(handle(state, request, peer).await) }
    });
    let connection = builder.serve_connection_with_upgrades(TokioIo::new(stream), service);
    let watched = graceful.watch(connection.into_owned());
    tokio::spawn(async move {
        if let Err(error) = watched.await {
            tracing::debug!(%peer, %error, "connection closed with error");
        }
    });
}

async fn drain(graceful: GracefulShutdown) {
    tokio::select! {
        _ = graceful.shutdown() => {}
        _ = tokio::time::sleep(DRAIN_TIMEOUT) => {
            tracing::warn!("drain timeout reached, closing remaining connections");
        }
    }
}
