mod config;

use anyhow::Context;
use arc_swap::ArcSwap;
use clap::Parser;
use config::{Config, LogFormat};
use router_core::Snapshot;
use router_cp::SnapshotSource;
use router_obs::{TracingConfig, TracingFormat};
use router_proxy::ProxyState;
use std::path::PathBuf;
use std::sync::Arc;
use tokio::net::TcpListener;
use tokio::sync::watch;

#[derive(Parser, Debug)]
#[command(
    name = "multihull",
    version,
    about = "Stateless request router for multi-provider GPU inference"
)]
struct Args {
    #[arg(long, default_value = "router.toml")]
    config: PathBuf,
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let args = Args::parse();
    let config = Config::load(&args.config)?;
    router_obs::init_tracing(&TracingConfig {
        format: match config.log.format {
            LogFormat::Text => TracingFormat::Text,
            LogFormat::Json => TracingFormat::Json,
        },
        default_filter: config.log.filter.clone(),
    });
    router_obs::metrics::describe_all();
    if let Some(tls) = &config.tls {
        tracing::warn!(
            cert = %tls.cert.display(),
            key = %tls.key.display(),
            "tls paths configured but TLS termination is not wired yet; serving plain HTTP"
        );
    }

    let source = SnapshotSource::parse(&config.snapshot.source)?;
    let snapshot = Arc::new(ArcSwap::from_pointee(Snapshot::default()));
    let (snapshot_tx, mut snapshot_rx) = watch::channel(Arc::new(Snapshot::default()));

    let proxy_state = ProxyState::new(config.proxy_config(), snapshot.clone());
    let admin_state =
        router_admin::AdminState::new(snapshot.clone()).with_proxy(proxy_state.clone());

    let source_task = {
        let node_id = config.node_id.clone();
        tokio::spawn(async move { source.run(node_id, snapshot_tx).await })
    };

    let swap_task = {
        let snapshot = snapshot.clone();
        let proxy_state = proxy_state.clone();
        tokio::spawn(async move {
            while snapshot_rx.changed().await.is_ok() {
                let next = snapshot_rx.borrow_and_update().clone();
                tracing::info!(
                    version = next.version,
                    routes = next.routes.len(),
                    "snapshot applied"
                );
                snapshot.store(next);
                proxy_state.refresh();
            }
        })
    };

    let admin_listen = config.admin_listen;
    let admin_task =
        tokio::spawn(async move { router_admin::serve(admin_listen, admin_state).await });

    let listener = TcpListener::bind(config.listen)
        .await
        .with_context(|| format!("binding {}", config.listen))?;
    tracing::info!(listen = %config.listen, admin = %config.admin_listen, source = config.snapshot.source, "multihull router started");

    let proxy_task = tokio::spawn(router_proxy::serve(
        proxy_state,
        listener,
        shutdown_signal(),
    ));

    tokio::select! {
        result = proxy_task => result??,
        result = admin_task => result??,
        result = source_task => {
            result??;
            anyhow::bail!("snapshot source ended");
        }
        _ = swap_task => anyhow::bail!("snapshot swap task ended"),
    }
    Ok(())
}

async fn shutdown_signal() {
    let ctrl_c = tokio::signal::ctrl_c();
    #[cfg(unix)]
    {
        let mut term = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
            .expect("install SIGTERM handler");
        tokio::select! {
            _ = ctrl_c => {}
            _ = term.recv() => {}
        }
    }
    #[cfg(not(unix))]
    {
        let _ = ctrl_c.await;
    }
    tracing::info!("shutdown requested, draining");
}
