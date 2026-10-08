use crate::core::snapshot::Degraded;
use crate::core::Snapshot;
use crate::cp::proto::discovery_client::DiscoveryClient;
use crate::cp::proto::{
    control_message, router_message, Ack, ControlMessage, Hello, Nack, RouterMessage,
};
use crate::cp::security::{BearerToken, SourceAuth};
use crate::cp::source::SnapshotError;
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
        .connect_with_connector(crate::tls::h2_connector(CONNECT_TIMEOUT, auth.tls.clone()))
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
    let (applied, refusal) = crate::cp::guard::apply(&tx.borrow(), snapshot.into());
    tx.send(Arc::new(applied))
        .map_err(|_| SnapshotError::ReceiverDropped)?;
    let reply = match refusal {
        Some(reason) => router_message::Message::Nack(Nack { version, reason }),
        None => router_message::Message::Ack(Ack { version }),
    };
    Ok(Some(RouterMessage {
        message: Some(reply),
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cp::proto;

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
            reason: crate::core::snapshot::DegradedReason::QueueDepth,
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

    fn routed(version: u64, routes: &[(&str, &[&str])]) -> proto::Snapshot {
        proto::Snapshot {
            version,
            routes: routes
                .iter()
                .map(|(id, endpoints)| proto::Route {
                    id: (*id).to_string(),
                    endpoints: endpoints
                        .iter()
                        .map(|endpoint| proto::Endpoint {
                            id: (*endpoint).to_string(),
                            url: "http://127.0.0.1:1".into(),
                            ..Default::default()
                        })
                        .collect(),
                    ..Default::default()
                })
                .collect(),
            ..Default::default()
        }
    }

    fn sent(snapshot: proto::Snapshot) -> ControlMessage {
        ControlMessage {
            message: Some(control_message::Message::Snapshot(snapshot)),
        }
    }

    fn holding(
        snapshot: proto::Snapshot,
    ) -> (watch::Sender<Arc<Snapshot>>, watch::Receiver<Arc<Snapshot>>) {
        watch::channel(Arc::new(snapshot.into()))
    }

    fn endpoint_ids(snapshot: &Snapshot) -> Vec<(String, Vec<String>)> {
        snapshot
            .routes
            .iter()
            .map(|route| {
                let ids = route.endpoints.iter().map(|e| e.id.clone()).collect();
                (route.id.clone(), ids)
            })
            .collect()
    }

    fn owned(routes: &[(&str, &[&str])]) -> Vec<(String, Vec<String>)> {
        routes
            .iter()
            .map(|(id, endpoints)| {
                let ids = endpoints.iter().map(|e| (*e).to_string()).collect();
                ((*id).to_string(), ids)
            })
            .collect()
    }

    #[test]
    fn a_route_sent_without_endpoints_keeps_the_endpoints_the_router_holds() {
        let (tx, _rx) = holding(routed(5, &[("llama", &["a", "b"]), ("other", &["c"])]));
        let next = routed(6, &[("llama", &[]), ("other", &["d"])]);
        let reply = handle(sent(next), &tx).unwrap().unwrap();
        match reply.message {
            Some(router_message::Message::Nack(nack)) => {
                assert_eq!(nack.version, 6);
                assert!(nack.reason.contains("route llama"), "{}", nack.reason);
                assert!(nack.reason.contains("remove the route"), "{}", nack.reason);
            }
            other => panic!("expected a nack, got {other:?}"),
        }
        let held = tx.borrow();
        assert_eq!(held.version, 6);
        assert_eq!(
            endpoint_ids(&held),
            owned(&[("llama", &["a", "b"]), ("other", &["d"])])
        );
    }

    #[test]
    fn removing_a_route_from_the_snapshot_drops_it() {
        let (tx, _rx) = holding(routed(5, &[("llama", &["a"]), ("other", &["c"])]));
        let reply = handle(sent(routed(6, &[("other", &["c"])])), &tx)
            .unwrap()
            .unwrap();
        assert!(matches!(
            reply.message,
            Some(router_message::Message::Ack(Ack { version: 6 }))
        ));
        assert_eq!(endpoint_ids(&tx.borrow()), owned(&[("other", &["c"])]));
    }

    #[test]
    fn a_route_that_had_no_endpoints_may_stay_empty() {
        let (tx, _rx) = holding(routed(5, &[("llama", &["a"]), ("fresh", &[])]));
        let next = routed(6, &[("llama", &["a"]), ("fresh", &[]), ("new", &[])]);
        let reply = handle(sent(next), &tx).unwrap().unwrap();
        assert!(matches!(
            reply.message,
            Some(router_message::Message::Ack(Ack { version: 6 }))
        ));
        assert_eq!(
            endpoint_ids(&tx.borrow()),
            owned(&[("llama", &["a"]), ("fresh", &[]), ("new", &[])])
        );
    }

    #[derive(Default)]
    struct Seen {
        hellos: Vec<u64>,
        replies: Vec<router_message::Message>,
    }

    struct Incarnation {
        script: std::sync::Mutex<Vec<proto::Snapshot>>,
        seen: Arc<std::sync::Mutex<Seen>>,
    }

    type ControlStream =
        std::pin::Pin<Box<dyn tokio_stream::Stream<Item = Result<ControlMessage, Status>> + Send>>;

    #[tonic::async_trait]
    impl proto::discovery_server::Discovery for Incarnation {
        type StreamStream = ControlStream;

        async fn stream(
            &self,
            request: Request<tonic::Streaming<RouterMessage>>,
        ) -> Result<tonic::Response<ControlStream>, Status> {
            let script = std::mem::take(&mut *self.script.lock().unwrap());
            let seen = self.seen.clone();
            let mut inbound = request.into_inner();
            let (tx, rx) = mpsc::channel(1);
            tokio::spawn(async move {
                let Ok(Some(first)) = inbound.message().await else {
                    return;
                };
                if let Some(router_message::Message::Hello(hello)) = first.message {
                    seen.lock().unwrap().hellos.push(hello.last_version);
                }
                for snapshot in script {
                    if tx.send(Ok(sent(snapshot))).await.is_err() {
                        return;
                    }
                    let Ok(Some(reply)) = inbound.message().await else {
                        return;
                    };
                    seen.lock().unwrap().replies.extend(reply.message);
                }
            });
            Ok(tonic::Response::new(Box::pin(ReceiverStream::new(rx))))
        }
    }

    async fn controller_incarnation(
        script: Vec<proto::Snapshot>,
    ) -> (String, Arc<std::sync::Mutex<Seen>>) {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let seen = Arc::new(std::sync::Mutex::new(Seen::default()));
        let service = proto::discovery_server::DiscoveryServer::new(Incarnation {
            script: std::sync::Mutex::new(script),
            seen: seen.clone(),
        });
        tokio::spawn(
            tonic::transport::Server::builder()
                .add_service(service)
                .serve_with_incoming(tokio_stream::wrappers::TcpListenerStream::new(listener)),
        );
        (format!("http://{address}"), seen)
    }

    async fn stream_until_the_controller_ends(url: &str, tx: &watch::Sender<Arc<Snapshot>>) {
        let auth = SourceAuth::anonymous().unwrap();
        tokio::time::timeout(
            Duration::from_secs(10),
            stream_once(url, "router-1", tx, &mut None, &auth),
        )
        .await
        .expect("the controller ends the stream")
        .expect("the stream ends cleanly");
    }

    #[tokio::test]
    async fn after_a_controller_restart_the_router_reports_its_version_and_refuses_stale_or_empty_updates(
    ) {
        let (tx, _rx) = watch::channel(Arc::new(Snapshot::default()));
        let (before, first) = controller_incarnation(vec![routed(5, &[("llama", &["a"])])]).await;
        stream_until_the_controller_ends(&before, &tx).await;
        assert_eq!(tx.borrow().version, 5);

        let (after, second) = controller_incarnation(vec![
            routed(1, &[("llama", &["b"])]),
            routed(6, &[("llama", &[])]),
            routed(7, &[("llama", &["c"])]),
        ])
        .await;
        stream_until_the_controller_ends(&after, &tx).await;

        assert_eq!(first.lock().unwrap().hellos, vec![0]);
        let second = second.lock().unwrap();
        assert_eq!(second.hellos, vec![5]);
        let replies: Vec<(u64, Option<String>)> = second
            .replies
            .iter()
            .map(|reply| match reply {
                router_message::Message::Ack(ack) => (ack.version, None),
                router_message::Message::Nack(nack) => (nack.version, Some(nack.reason.clone())),
                other => panic!("unexpected reply {other:?}"),
            })
            .collect();
        assert_eq!(replies.len(), 3, "{replies:?}");
        assert_eq!(
            replies[0],
            (1, Some("stale: router holds version 5".to_string()))
        );
        assert_eq!(replies[1].0, 6);
        assert!(replies[1]
            .1
            .as_deref()
            .is_some_and(|reason| reason.contains("route llama")));
        assert_eq!(replies[2], (7, None));
        let held = tx.borrow();
        assert_eq!(held.version, 7);
        assert_eq!(endpoint_ids(&held), owned(&[("llama", &["c"])]));
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
