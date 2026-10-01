use crate::proto::discovery_client::DiscoveryClient;
use crate::proto::{
    control_message, router_message, Ack, ControlMessage, Hello, Nack, RouterMessage,
};
use crate::source::SnapshotError;
use router_core::Snapshot;
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::{mpsc, watch};
use tokio_stream::wrappers::ReceiverStream;
use tonic::transport::Channel;

const INITIAL_RECONNECT: Duration = Duration::from_secs(1);
const MAX_RECONNECT: Duration = Duration::from_secs(30);

pub async fn connect(url: String) -> Result<DiscoveryClient<Channel>, SnapshotError> {
    let channel = Channel::from_shared(url)
        .map_err(|e| SnapshotError::InvalidSource(e.to_string()))?
        .connect_timeout(Duration::from_secs(5))
        .connect()
        .await?;
    Ok(DiscoveryClient::new(channel))
}

pub async fn run(
    url: String,
    node_id: String,
    tx: watch::Sender<Arc<Snapshot>>,
) -> Result<(), SnapshotError> {
    let mut backoff = INITIAL_RECONNECT;
    loop {
        match stream_once(&url, &node_id, &tx).await {
            Ok(()) => backoff = INITIAL_RECONNECT,
            Err(SnapshotError::ReceiverDropped) => return Err(SnapshotError::ReceiverDropped),
            Err(error) => {
                tracing::warn!(%error, retry_in = ?backoff, "discovery stream failed");
            }
        }
        tokio::time::sleep(backoff).await;
        backoff = (backoff * 2).min(MAX_RECONNECT);
    }
}

async fn stream_once(
    url: &str,
    node_id: &str,
    tx: &watch::Sender<Arc<Snapshot>>,
) -> Result<(), SnapshotError> {
    let mut client = connect(url.to_string()).await?;
    let (outbound_tx, outbound_rx) = mpsc::channel::<RouterMessage>(16);
    let last_version = tx.borrow().version;
    outbound_tx
        .send(hello(node_id, last_version))
        .await
        .map_err(|_| SnapshotError::ReceiverDropped)?;
    let mut inbound = client
        .stream(ReceiverStream::new(outbound_rx))
        .await?
        .into_inner();
    while let Some(message) = inbound.message().await? {
        if let Some(reply) = handle(message, tx)? {
            if outbound_tx.send(reply).await.is_err() {
                break;
            }
        }
    }
    Ok(())
}

fn hello(node_id: &str, last_version: u64) -> RouterMessage {
    RouterMessage {
        message: Some(router_message::Message::Hello(Hello {
            node_id: node_id.to_string(),
            last_version,
        })),
    }
}

pub fn handle(
    message: ControlMessage,
    tx: &watch::Sender<Arc<Snapshot>>,
) -> Result<Option<RouterMessage>, SnapshotError> {
    let Some(control_message::Message::Snapshot(snapshot)) = message.message else {
        return Ok(None);
    };
    let version = snapshot.version;
    let current = tx.borrow().version;
    if version < current {
        return Ok(Some(RouterMessage {
            message: Some(router_message::Message::Nack(Nack {
                version,
                reason: format!("stale: router holds version {current}"),
            })),
        }));
    }
    let converted: Snapshot = snapshot.into();
    tx.send(Arc::new(converted))
        .map_err(|_| SnapshotError::ReceiverDropped)?;
    Ok(Some(RouterMessage {
        message: Some(router_message::Message::Ack(Ack { version })),
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::proto;

    fn control(version: u64) -> ControlMessage {
        ControlMessage {
            message: Some(control_message::Message::Snapshot(proto::Snapshot {
                version,
                ..Default::default()
            })),
        }
    }

    #[test]
    fn snapshot_is_applied_and_acked() {
        let (tx, rx) = watch::channel(Arc::new(Snapshot::default()));
        let reply = handle(control(5), &tx).unwrap().unwrap();
        assert!(matches!(
            reply.message,
            Some(router_message::Message::Ack(Ack { version: 5 }))
        ));
        assert_eq!(rx.borrow().version, 5);
    }

    #[test]
    fn stale_snapshot_is_nacked_and_ignored() {
        let (tx, rx) = watch::channel(Arc::new(Snapshot {
            version: 9,
            ..Default::default()
        }));
        let reply = handle(control(5), &tx).unwrap().unwrap();
        assert!(matches!(
            reply.message,
            Some(router_message::Message::Nack(Nack { version: 5, .. }))
        ));
        assert_eq!(rx.borrow().version, 9);
    }

    #[test]
    fn empty_control_message_is_ignored() {
        let (tx, _rx) = watch::channel(Arc::new(Snapshot::default()));
        assert!(handle(ControlMessage { message: None }, &tx)
            .unwrap()
            .is_none());
    }

    #[test]
    fn hello_carries_node_and_version() {
        let message = hello("router-1", 4);
        match message.message {
            Some(router_message::Message::Hello(h)) => {
                assert_eq!(h.node_id, "router-1");
                assert_eq!(h.last_version, 4);
            }
            other => panic!("unexpected {other:?}"),
        }
    }
}
