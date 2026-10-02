mod config;

use anyhow::Context;
use arc_swap::ArcSwap;
use clap::Parser;
use config::{Config, LogFormat};
use router_core::Snapshot;
use router_cp::SnapshotSource;
use router_obs::{TracingConfig, TracingFormat};
use router_proxy::ProxyState;
use router_tls::TlsReloader;
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
    let metrics_handle = router_obs::install_prometheus()
        .map_err(|error| anyhow::anyhow!("installing prometheus recorder: {error}"))?;
    let tls = match &config.tls {
        Some(tls) => Some(Arc::new(
            TlsReloader::new(tls.cert.clone(), tls.key.clone())
                .with_context(|| format!("loading tls cert {}", tls.cert.display()))?,
        )),
        None => None,
    };

    let source = SnapshotSource::parse(&config.snapshot.source)?;
    let snapshot = Arc::new(ArcSwap::from_pointee(Snapshot::default()));
    let (snapshot_tx, mut snapshot_rx) = watch::channel(Arc::new(Snapshot::default()));

    let proxy_state = ProxyState::try_new(config.proxy_config(), snapshot.clone())
        .context("building upstream tls client")?;
    let admin_state = router_admin::AdminState::new(snapshot.clone())
        .with_proxy(proxy_state.clone())
        .with_metrics(metrics_handle);

    let (degraded_tx, degraded_rx) = tokio::sync::mpsc::channel(64);
    let source_task = {
        let node_id = config.node_id.clone();
        tokio::spawn(async move { source.run(node_id, snapshot_tx, Some(degraded_rx)).await })
    };

    let housekeeping_task = {
        let proxy_state = proxy_state.clone();
        tokio::spawn(async move {
            let mut ticker = tokio::time::interval(HOUSEKEEPING_INTERVAL);
            loop {
                ticker.tick().await;
                proxy_state.runtime.expire_sessions();
                for signal in proxy_state.runtime.poll_degraded() {
                    tracing::warn!(
                        service = %signal.service,
                        provider = %signal.provider,
                        reason = ?signal.reason,
                        observed_concurrency = signal.observed_concurrency,
                        "degraded signal"
                    );
                    if degraded_tx.try_send(signal).is_err() {
                        tracing::debug!("degraded channel full or closed, dropping signal");
                    }
                }
            }
        })
    };

    let probe_task = tokio::spawn(router_proxy::probe::run(proxy_state.clone()));

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
    tracing::info!(
        listen = %config.listen,
        admin = %config.admin_listen,
        source = config.snapshot.source,
        tls = tls.is_some(),
        "multihull router started"
    );

    let proxy_task = match tls {
        Some(tls) => {
            spawn_tls_reload_on_sighup(tls.clone());
            tokio::spawn(router_proxy::serve_tls(
                proxy_state,
                listener,
                tls,
                shutdown_signal(),
            ))
        }
        None => tokio::spawn(router_proxy::serve(
            proxy_state,
            listener,
            shutdown_signal(),
        )),
    };

    tokio::select! {
        result = proxy_task => result??,
        result = admin_task => result??,
        result = source_task => {
            result??;
            anyhow::bail!("snapshot source ended");
        }
        _ = swap_task => anyhow::bail!("snapshot swap task ended"),
        _ = housekeeping_task => anyhow::bail!("housekeeping task ended"),
        _ = probe_task => anyhow::bail!("probe task ended"),
    }
    Ok(())
}

const HOUSEKEEPING_INTERVAL: std::time::Duration = std::time::Duration::from_millis(500);

fn spawn_tls_reload_on_sighup(tls: Arc<TlsReloader>) {
    #[cfg(unix)]
    tokio::spawn(async move {
        let mut hangup = match tokio::signal::unix::signal(tokio::signal::unix::SignalKind::hangup())
        {
            Ok(signal) => signal,
            Err(error) => {
                tracing::warn!(%error, "cannot install SIGHUP handler, tls reload disabled");
                return;
            }
        };
        while hangup.recv().await.is_some() {
            if let Err(error) = tls.reload() {
                tracing::error!(%error, "tls reload failed, keeping previous certificate");
            }
        }
    });
    #[cfg(not(unix))]
    drop(tls);
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
