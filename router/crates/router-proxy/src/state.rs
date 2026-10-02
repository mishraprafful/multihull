use arc_swap::ArcSwap;
use router_core::Snapshot;
use router_tls::TlsError;
use std::sync::Arc;

use crate::attempt::{build_client, UpstreamClient};
use crate::config::ProxyConfig;
use crate::route_table::RouteTable;
use crate::runtime::Runtime;

pub struct ProxyState {
    pub config: ProxyConfig,
    pub snapshot: Arc<ArcSwap<Snapshot>>,
    table: ArcSwap<RouteTable>,
    pub runtime: Runtime,
    pub client: UpstreamClient,
}

impl ProxyState {
    pub fn new(config: ProxyConfig, snapshot: Arc<ArcSwap<Snapshot>>) -> Arc<Self> {
        Self::try_new(config, snapshot).expect("upstream tls configuration is valid")
    }

    pub fn try_new(
        config: ProxyConfig,
        snapshot: Arc<ArcSwap<Snapshot>>,
    ) -> Result<Arc<Self>, TlsError> {
        let table = RouteTable::build(&snapshot.load());
        let client = build_client(config.timeouts.connect, config.upstream_ca.as_deref())?;
        let runtime = Runtime::new(
            config.circuit.clone(),
            config.admission.clone(),
            config.pressure.clone(),
            config.probe.clone(),
            config.retry.clone(),
        );
        Ok(Arc::new(Self {
            config,
            snapshot,
            table: ArcSwap::from_pointee(table),
            runtime,
            client,
        }))
    }

    pub fn refresh(&self) {
        let snapshot = self.snapshot.load();
        self.table.store(Arc::new(RouteTable::build(&snapshot)));
        self.runtime.retain_snapshot(&snapshot);
    }

    pub fn table(&self) -> arc_swap::Guard<Arc<RouteTable>> {
        self.table.load()
    }
}
