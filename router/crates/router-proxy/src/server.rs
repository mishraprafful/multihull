use hyper::service::service_fn;
use hyper_util::rt::{TokioExecutor, TokioIo};
use hyper_util::server::conn::auto;
use hyper_util::server::graceful::GracefulShutdown;
use std::future::Future;
use std::pin::pin;
use std::sync::Arc;
use std::time::Duration;
use tokio::net::TcpListener;

use crate::handler::handle;
use crate::state::ProxyState;

const DRAIN_TIMEOUT: Duration = Duration::from_secs(600);

pub async fn serve(
    state: Arc<ProxyState>,
    listener: TcpListener,
    shutdown: impl Future<Output = ()>,
) -> std::io::Result<()> {
    let graceful = GracefulShutdown::new();
    let builder = auto::Builder::new(TokioExecutor::new());
    let mut shutdown = pin!(shutdown);
    loop {
        tokio::select! {
            accepted = listener.accept() => {
                let (stream, peer) = accepted?;
                let state = state.clone();
                let service = service_fn(move |request| {
                    let state = state.clone();
                    async move { Ok::<_, std::convert::Infallible>(handle(state, request).await) }
                });
                let connection = builder.serve_connection_with_upgrades(TokioIo::new(stream), service);
                let watched = graceful.watch(connection.into_owned());
                tokio::spawn(async move {
                    if let Err(error) = watched.await {
                        tracing::debug!(%peer, %error, "connection closed with error");
                    }
                });
            }
            _ = &mut shutdown => break,
        }
    }
    tokio::select! {
        _ = graceful.shutdown() => {}
        _ = tokio::time::sleep(DRAIN_TIMEOUT) => {
            tracing::warn!("drain timeout reached, closing remaining connections");
        }
    }
    Ok(())
}
