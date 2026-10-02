use crate::source::SnapshotError;
use bytes::Bytes;
use http::header::{ETAG, IF_NONE_MATCH};
use http::{HeaderValue, Request, StatusCode};
use http_body_util::{BodyExt, Full};
use router_core::Snapshot;
use router_tls::HttpsClient;
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::watch;

pub const DEFAULT_POLL_INTERVAL: Duration = Duration::from_secs(10);
const CONNECT_TIMEOUT: Duration = Duration::from_secs(5);
const REQUEST_TIMEOUT: Duration = Duration::from_secs(30);

pub fn client() -> Result<HttpsClient, SnapshotError> {
    Ok(router_tls::https_client(CONNECT_TIMEOUT, None)?)
}

pub async fn poll(
    url: String,
    interval: Duration,
    client: HttpsClient,
    tx: watch::Sender<Arc<Snapshot>>,
) -> Result<(), SnapshotError> {
    let mut poller = Poller::new(url, client);
    let mut ticker = tokio::time::interval(interval.max(Duration::from_millis(10)));
    ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    loop {
        ticker.tick().await;
        match poller.fetch().await {
            Ok(Some(snapshot)) => {
                let changed = tx.borrow().version != snapshot.version || **tx.borrow() != snapshot;
                if changed {
                    tracing::info!(version = snapshot.version, url = %poller.url, "snapshot fetched");
                    tx.send(Arc::new(snapshot))
                        .map_err(|_| SnapshotError::ReceiverDropped)?;
                }
            }
            Ok(None) => {}
            Err(error) => tracing::warn!(%error, url = %poller.url, "snapshot poll failed"),
        }
        if tx.is_closed() {
            return Err(SnapshotError::ReceiverDropped);
        }
    }
}

pub struct Poller {
    url: String,
    client: HttpsClient,
    etag: Option<HeaderValue>,
}

impl Poller {
    pub fn new(url: String, client: HttpsClient) -> Self {
        Self {
            url,
            client,
            etag: None,
        }
    }

    pub fn etag(&self) -> Option<&HeaderValue> {
        self.etag.as_ref()
    }

    pub async fn fetch(&mut self) -> Result<Option<Snapshot>, SnapshotError> {
        let mut request = Request::get(&self.url);
        if let Some(etag) = &self.etag {
            request = request.header(IF_NONE_MATCH, etag.clone());
        }
        let request = request
            .body(Full::new(Bytes::new()))
            .map_err(|error| SnapshotError::Http(error.to_string()))?;
        let response = tokio::time::timeout(REQUEST_TIMEOUT, self.client.request(request))
            .await
            .map_err(|_| SnapshotError::Http("request timed out".into()))?
            .map_err(|error| SnapshotError::Http(error.to_string()))?;
        match response.status() {
            StatusCode::NOT_MODIFIED => Ok(None),
            status if status.is_success() => {
                let etag = response.headers().get(ETAG).cloned();
                let body = response
                    .into_body()
                    .collect()
                    .await
                    .map_err(|error| SnapshotError::Http(error.to_string()))?
                    .to_bytes();
                let snapshot: Snapshot = serde_json::from_slice(&body)
                    .map_err(|error| SnapshotError::Http(format!("invalid json: {error}")))?;
                self.etag = etag;
                Ok(Some(snapshot))
            }
            status => Err(SnapshotError::Http(format!("unexpected status {status}"))),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use router_testkit::{MockUpstream, MockUpstreamConfig};

    fn snapshot_json(version: u64) -> String {
        format!(
            r#"{{"version":{version},"routes":[{{"id":"r","endpoints":[{{"id":"e{version}","url":"http://127.0.0.1:1"}}]}}]}}"#
        )
    }

    fn served(version: u64) -> MockUpstreamConfig {
        MockUpstreamConfig::default()
            .with_body(snapshot_json(version))
            .with_etag(format!("\"v{version}\""))
    }

    #[tokio::test]
    async fn poll_publishes_snapshots_and_honours_etags() {
        let upstream = MockUpstream::start(served(1)).await.unwrap();
        let (tx, mut rx) = watch::channel(Arc::new(Snapshot::default()));
        let url = format!("{}/snapshot.json", upstream.url());
        let task = tokio::spawn(poll(url, Duration::from_millis(30), client().unwrap(), tx));

        tokio::time::timeout(Duration::from_secs(5), rx.changed())
            .await
            .expect("initial snapshot")
            .unwrap();
        assert_eq!(rx.borrow_and_update().version, 1);

        tokio::time::sleep(Duration::from_millis(200)).await;
        assert!(upstream.request_count() >= 3);
        assert_eq!(upstream.not_modified_count(), upstream.request_count() - 1);
        assert!(!rx.has_changed().unwrap());

        upstream.reconfigure(served(2));
        tokio::time::timeout(Duration::from_secs(5), rx.changed())
            .await
            .expect("updated snapshot")
            .unwrap();
        assert_eq!(rx.borrow_and_update().routes[0].endpoints[0].id, "e2");

        drop(rx);
        let result = tokio::time::timeout(Duration::from_secs(5), task).await;
        assert!(matches!(
            result,
            Ok(Ok(Err(SnapshotError::ReceiverDropped)))
        ));
    }

    #[tokio::test]
    async fn fetch_reports_bad_status_and_bad_json() {
        let upstream = MockUpstream::start(
            MockUpstreamConfig::default().with_status(StatusCode::INTERNAL_SERVER_ERROR),
        )
        .await
        .unwrap();
        let mut poller = Poller::new(upstream.url(), client().unwrap());
        assert!(matches!(poller.fetch().await, Err(SnapshotError::Http(_))));
        upstream.reconfigure(MockUpstreamConfig::default().with_body("{not json"));
        assert!(matches!(poller.fetch().await, Err(SnapshotError::Http(_))));
        assert!(poller.etag().is_none());
        upstream.reconfigure(served(3));
        let snapshot = poller.fetch().await.unwrap().unwrap();
        assert_eq!(snapshot.version, 3);
        assert_eq!(poller.etag().unwrap(), "\"v3\"");
        assert!(poller.fetch().await.unwrap().is_none());
    }
}
