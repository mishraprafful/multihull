use crate::proto::discovery_client::DiscoveryClient;
use crate::proto::{
    control_message, router_message, Ack, ControlMessage, Hello, Nack, RouterMessage,
};
use crate::security::{BearerToken, SourceAuth};
use crate::source::SnapshotError;
use router_core::snapshot::Degraded;
use router_core::Snapshot;
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::{mpsc, watch};
use tokio_stream::wrappers::ReceiverStream;
use tonic::metadata::{Ascii, MetadataValue};
use tonic::service::interceptor::InterceptedService;
use tonic::service::Interceptor;
use tonic::transport::{Channel, Endpoint};
use tonic::{Request, Status};

const INITIAL_RECONNECT: Duration = Duration::from_secs(1);
const MAX_RECONNECT: Duration = Duration::from_secs(30);
const CONNECT_TIMEOUT: Duration = Duration::from_secs(5);

pub type DegradedReceiver = mpsc::Receiver<Degraded>;
pub type AuthorizedChannel = InterceptedService<Channel, Authorize>;

#[derive(Clone, Default)]
pub struct Authorize(Option<MetadataValue<Ascii>>);

impl Authorize {
    pub fn new(token: Option<&BearerToken>) -> Result<Self, SnapshotError> {
        let Some(token) = token else {
            return Ok(Self(None));
        };
        let mut value = MetadataValue::try_from(token.header_value().as_bytes())
            .map_err(|_| SnapshotError::Token("the token is not valid gRPC metadata".into()))?;
        value.set_sensitive(true);
        Ok(Self(Some(value)))
    }
}

impl Interceptor for Authorize {
    fn call(&mut self, mut request: Request<()>) -> Result<Request<()>, Status> {
        if let Some(value) = &self.0 {
            request
                .metadata_mut()
                .insert("authorization", value.clone());
        }
        Ok(request)
    }
}

pub async fn connect(
    url: String,
    auth: &SourceAuth,
) -> Result<DiscoveryClient<AuthorizedChannel>, SnapshotError> {
    let authorize = Authorize::new(auth.token.as_ref())?;
    let channel = Endpoint::from_shared(url)
        .map_err(|e| SnapshotError::InvalidSource(e.to_string()))?
        .connect_timeout(CONNECT_TIMEOUT)
        .connect_with_connector(router_tls::h2_connector(CONNECT_TIMEOUT, auth.tls.clone()))
        .await?;
    Ok(DiscoveryClient::with_interceptor(channel, authorize))
}

pub async fn run(
    url: String,
    node_id: String,
    tx: watch::Sender<Arc<Snapshot>>,
    mut degraded: Option<DegradedReceiver>,
    auth: SourceAuth,
) -> Result<(), SnapshotError> {
    let mut backoff = INITIAL_RECONNECT;
    loop {
        match stream_once(&url, &node_id, &tx, &mut degraded, &auth).await {
            Ok(()) => backoff = INITIAL_RECONNECT,
            Err(SnapshotError::ReceiverDropped) => return Err(SnapshotError::ReceiverDropped),
            Err(error) => {
                tracing::warn!(
                    error = %error_chain(&error),
                    retry_in = ?backoff,
                    "discovery stream failed"
                );
            }
        }
        tokio::time::sleep(backoff).await;
        backoff = (backoff * 2).min(MAX_RECONNECT);
    }
}

pub fn error_chain(error: &dyn std::error::Error) -> String {
    let mut text = error.to_string();
    let mut source = error.source();
    while let Some(cause) = source {
        let cause_text = cause.to_string();
        if !text.ends_with(&cause_text) {
            text.push_str(": ");
            text.push_str(&cause_text);
        }
        source = cause.source();
    }
    text
}

pub async fn stream_once(
    url: &str,
    node_id: &str,
    tx: &watch::Sender<Arc<Snapshot>>,
    degraded: &mut Option<DegradedReceiver>,
    auth: &SourceAuth,
) -> Result<(), SnapshotError> {
    let mut client = connect(url.to_string(), auth).await?;
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
    loop {
        tokio::select! {
            message = inbound.message() => {
                let Some(message) = message? else { break };
                if let Some(reply) = handle(message, tx)? {
                    if outbound_tx.send(reply).await.is_err() {
                        break;
                    }
                }
            }
            signal = next_degraded(degraded) => {
                match signal {
                    Some(signal) => {
                        if outbound_tx.send(degraded_message(signal)).await.is_err() {
                            break;
                        }
                    }
                    None => *degraded = None,
                }
            }
        }
    }
    Ok(())
}

async fn next_degraded(receiver: &mut Option<DegradedReceiver>) -> Option<Degraded> {
    match receiver {
        Some(receiver) => receiver.recv().await,
        None => std::future::pending().await,
    }
}

fn hello(node_id: &str, last_version: u64) -> RouterMessage {
    RouterMessage {
        message: Some(router_message::Message::Hello(Hello {
            node_id: node_id.to_string(),
            last_version,
        })),
    }
}

pub fn degraded_message(signal: Degraded) -> RouterMessage {
    RouterMessage {
        message: Some(router_message::Message::Degraded(signal.into())),
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
    fn degraded_signal_becomes_a_router_message() {
        let message = degraded_message(Degraded {
            service: "llama".into(),
            provider: "modal".into(),
            reason: router_core::snapshot::DegradedReason::QueueDepth,
            observed_concurrency: 12,
        });
        match message.message {
            Some(router_message::Message::Degraded(d)) => {
                assert_eq!(d.service, "llama");
                assert_eq!(d.provider, "modal");
                assert_eq!(d.reason, proto::DegradedReason::QueueDepth as i32);
                assert_eq!(d.observed_concurrency, 12);
            }
            other => panic!("unexpected {other:?}"),
        }
    }

    #[tokio::test]
    async fn next_degraded_pends_without_a_receiver_and_drains_with_one() {
        let mut none: Option<DegradedReceiver> = None;
        assert!(
            tokio::time::timeout(Duration::from_millis(20), next_degraded(&mut none))
                .await
                .is_err()
        );
        let (tx, rx) = mpsc::channel(1);
        let mut some = Some(rx);
        tx.send(Degraded::default()).await.unwrap();
        assert!(next_degraded(&mut some).await.is_some());
        drop(tx);
        assert!(next_degraded(&mut some).await.is_none());
    }

    #[test]
    fn authorize_adds_a_sensitive_bearer_header_only_with_a_token() {
        let token = BearerToken::new("abc").unwrap();
        let mut with = Authorize::new(Some(&token)).unwrap();
        let request = with.call(Request::new(())).unwrap();
        let value = request.metadata().get("authorization").unwrap();
        assert_eq!(value.to_str().unwrap(), "Bearer abc");
        assert!(value.is_sensitive());
        let mut without = Authorize::new(None).unwrap();
        let request = without.call(Request::new(())).unwrap();
        assert!(request.metadata().get("authorization").is_none());
    }

    #[test]
    fn error_chain_joins_sources_once() {
        let inner = std::io::Error::other("certificate unknown");
        let outer = SnapshotError::Io {
            path: "/ca.pem".into(),
            source: inner,
        };
        assert_eq!(
            error_chain(&outer),
            "io error reading /ca.pem: certificate unknown"
        );
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
