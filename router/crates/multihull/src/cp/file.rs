use crate::core::Snapshot;
use crate::cp::source::SnapshotError;
use notify::{Event, RecursiveMode, Watcher};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::{mpsc, watch};

const SETTLE_DELAY: Duration = Duration::from_millis(50);

pub fn load(path: &Path) -> Result<Snapshot, SnapshotError> {
    let bytes = std::fs::read(path).map_err(|source| SnapshotError::Io {
        path: path.to_path_buf(),
        source,
    })?;
    serde_json::from_slice(&bytes).map_err(|source| SnapshotError::Json {
        path: path.to_path_buf(),
        source,
    })
}

pub async fn watch(path: PathBuf, tx: watch::Sender<Arc<Snapshot>>) -> Result<(), SnapshotError> {
    let initial = load(&path)?;
    tx.send(Arc::new(initial))
        .map_err(|_| SnapshotError::ReceiverDropped)?;

    let (events_tx, mut events_rx) = mpsc::unbounded_channel::<Result<Event, notify::Error>>();
    let mut watcher = notify::recommended_watcher(move |event| {
        let _ = events_tx.send(event);
    })?;
    let watched_dir = path
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .map(Path::to_path_buf)
        .unwrap_or_else(|| PathBuf::from("."));
    watcher.watch(&watched_dir, RecursiveMode::NonRecursive)?;

    let target = path.canonicalize().unwrap_or_else(|_| path.clone());
    let file_name = path.file_name().map(|n| n.to_os_string());
    while let Some(event) = events_rx.recv().await {
        let event = match event {
            Ok(event) => event,
            Err(error) => {
                tracing::warn!(%error, "snapshot watcher error");
                continue;
            }
        };
        let touches_target = event.paths.iter().any(|p| {
            p == &target
                || p == &path
                || p.file_name()
                    .is_some_and(|n| Some(n.to_os_string()) == file_name)
        });
        if !touches_target {
            continue;
        }
        tokio::time::sleep(SETTLE_DELAY).await;
        while events_rx.try_recv().is_ok() {}
        match load(&path) {
            Ok(snapshot) => {
                let (snapshot, _) = crate::cp::guard::apply(&tx.borrow(), snapshot);
                let changed = tx.borrow().version != snapshot.version || **tx.borrow() != snapshot;
                if changed {
                    tracing::info!(version = snapshot.version, "snapshot reloaded from file");
                    tx.send(Arc::new(snapshot))
                        .map_err(|_| SnapshotError::ReceiverDropped)?;
                }
            }
            Err(error) => tracing::warn!(%error, "ignoring unreadable snapshot file"),
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    fn write_snapshot(path: &Path, version: u64) {
        let json = format!(
            r#"{{"version":{version},"routes":[{{"id":"r","endpoints":[{{"id":"e{version}","url":"http://127.0.0.1:1"}}]}}]}}"#
        );
        let tmp = path.with_extension("tmp");
        let mut file = std::fs::File::create(&tmp).unwrap();
        file.write_all(json.as_bytes()).unwrap();
        file.sync_all().unwrap();
        std::fs::rename(&tmp, path).unwrap();
    }

    #[test]
    fn load_parses_json_snapshot() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("snapshot.json");
        write_snapshot(&path, 4);
        let snapshot = load(&path).unwrap();
        assert_eq!(snapshot.version, 4);
        assert_eq!(snapshot.routes[0].endpoints[0].id, "e4");
    }

    #[test]
    fn load_parses_a_control_plane_document() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("snapshot.json");
        std::fs::write(&path, CONTROL_PLANE_DOCUMENT).unwrap();
        let snapshot = load(&path).unwrap();
        assert_eq!(snapshot.version, 3);
        assert_eq!(snapshot.at.as_ref().unwrap().seconds, 1_790_938_555);
        let route = &snapshot.routes[0];
        assert_eq!(route.hostname, "llama.local");
        assert_eq!(route.protocol, crate::core::snapshot::Protocol::Http);
        assert_eq!(
            route.failover.policy,
            crate::core::snapshot::FailoverPolicy::Priority
        );
        assert!(route.auth.api_key_hashes.is_empty());
        assert!(!route.auth.required);
        let sticky = route.sticky.as_ref().unwrap();
        assert_eq!(sticky.ttl_seconds, 300);
        assert_eq!(sticky.mode, crate::core::snapshot::StickyMode::Endpoint);
        let primary = snapshot.find_endpoint("e2e-three/primary").unwrap();
        assert_eq!(primary.kind, crate::core::snapshot::EndpointType::Docker);
        assert_eq!(primary.health, crate::core::snapshot::Health::Ready);
        assert!(primary.accepts_traffic());
        let secondary = snapshot.find_endpoint("e2e-three/secondary").unwrap();
        assert_eq!(secondary.health, crate::core::snapshot::Health::Down);
        assert!(!secondary.accepts_traffic());
    }

    #[test]
    fn load_reads_whether_a_route_requires_api_keys() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("snapshot.json");
        let document = CONTROL_PLANE_DOCUMENT.replace(
            r#""auth": {"api_key_hashes": []}"#,
            r#""auth": {"api_key_hashes": [], "required": true}"#,
        );
        std::fs::write(&path, document).unwrap();
        let snapshot = load(&path).unwrap();
        assert!(snapshot.routes[0].auth.required);
        assert!(snapshot.routes[0].auth.api_key_hashes.is_empty());
    }

    const CONTROL_PLANE_DOCUMENT: &str = r#"{
  "version": 3,
  "at": {"seconds": 1790938555, "nanos": 0},
  "routes": [
    {
      "id": "e2e-three",
      "hostname": "llama.local",
      "path_prefix": "/",
      "protocol": "http",
      "failover": {"policy": "priority", "retry_on": ["5xx", "timeout", "capacity"], "max_retries": 2},
      "auth": {"api_key_hashes": []},
      "sticky": {"key": "header:X-Session-Id", "ttl_seconds": 300, "mode": "endpoint", "on_unhealthy": "rehome", "fallback_key": ""},
      "endpoints": [
        {
          "id": "e2e-three/primary",
          "provider": "primary",
          "type": "docker",
          "url": "http://127.0.0.1:62967",
          "region": "",
          "priority": 1,
          "weight": 1,
          "health": "ready",
          "ready_replicas": 1,
          "max_concurrency": 32,
          "inject_headers": {}
        },
        {
          "id": "e2e-three/secondary",
          "provider": "secondary",
          "type": "docker",
          "url": "http://127.0.0.1:62966",
          "region": "",
          "priority": 2,
          "weight": 1,
          "health": "down",
          "ready_replicas": 0,
          "max_concurrency": 32,
          "inject_headers": {}
        }
      ]
    }
  ]
}"#;

    #[test]
    fn load_reports_missing_and_invalid_files() {
        let dir = tempfile::tempdir().unwrap();
        let missing = dir.path().join("missing.json");
        assert!(matches!(load(&missing), Err(SnapshotError::Io { .. })));
        let bad = dir.path().join("bad.json");
        std::fs::write(&bad, b"{not json").unwrap();
        assert!(matches!(load(&bad), Err(SnapshotError::Json { .. })));
    }

    fn write_document(path: &Path, json: &str) {
        let tmp = path.with_extension("tmp");
        std::fs::write(&tmp, json).unwrap();
        std::fs::rename(&tmp, path).unwrap();
    }

    async fn next_snapshot(rx: &mut watch::Receiver<Arc<Snapshot>>) -> Arc<Snapshot> {
        tokio::time::timeout(Duration::from_secs(5), rx.changed())
            .await
            .expect("snapshot reloaded")
            .unwrap();
        rx.borrow_and_update().clone()
    }

    #[tokio::test]
    async fn watch_keeps_endpoints_when_a_route_loses_them_and_drops_a_removed_route() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("snapshot.json");
        write_snapshot(&path, 1);
        let (tx, mut rx) = watch::channel(Arc::new(Snapshot::default()));
        let task = tokio::spawn(watch(path.clone(), tx));
        assert_eq!(next_snapshot(&mut rx).await.version, 1);

        tokio::time::sleep(Duration::from_millis(200)).await;
        write_document(
            &path,
            r#"{"version":2,"routes":[{"id":"r","endpoints":[]}]}"#,
        );
        let kept = next_snapshot(&mut rx).await;
        assert_eq!(kept.version, 2);
        assert_eq!(kept.routes[0].endpoints[0].id, "e1");

        tokio::time::sleep(Duration::from_millis(200)).await;
        write_document(&path, r#"{"version":3,"routes":[]}"#);
        let removed = next_snapshot(&mut rx).await;
        assert_eq!(removed.version, 3);
        assert!(removed.routes.is_empty());
        task.abort();
    }

    #[tokio::test]
    async fn watch_publishes_initial_and_updated_snapshots() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("snapshot.json");
        write_snapshot(&path, 1);
        let (tx, mut rx) = watch::channel(Arc::new(Snapshot::default()));
        let task = tokio::spawn(watch(path.clone(), tx));

        tokio::time::timeout(Duration::from_secs(5), rx.changed())
            .await
            .expect("initial snapshot")
            .unwrap();
        assert_eq!(rx.borrow().version, 1);

        tokio::time::sleep(Duration::from_millis(200)).await;
        write_snapshot(&path, 2);
        tokio::time::timeout(Duration::from_secs(5), rx.changed())
            .await
            .expect("updated snapshot")
            .unwrap();
        assert_eq!(rx.borrow().version, 2);
        assert_eq!(rx.borrow().routes[0].endpoints[0].id, "e2");

        drop(rx);
        write_snapshot(&path, 3);
        let result = tokio::time::timeout(Duration::from_secs(5), task).await;
        assert!(matches!(
            result,
            Ok(Ok(Err(SnapshotError::ReceiverDropped)))
        ));
    }
}
