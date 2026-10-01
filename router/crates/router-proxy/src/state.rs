use arc_swap::ArcSwap;
use router_core::Snapshot;
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
        let table = RouteTable::build(&snapshot.load());
        let client = build_client(config.timeouts.connect);
        Arc::new(Self {
            config,
            snapshot,
            table: ArcSwap::from_pointee(table),
            runtime: Runtime::new(),
            client,
        })
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
