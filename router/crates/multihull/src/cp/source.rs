use crate::core::Snapshot;
use crate::cp::security::SourceAuth;
use std::path::PathBuf;
use std::sync::Arc;
use tokio::sync::watch;

#[derive(Debug, thiserror::Error)]
pub enum SnapshotError {
    #[error("invalid snapshot source: {0}")]
    InvalidSource(String),
    #[error("http snapshot fetch failed: {0}")]
    Http(String),
    #[error("snapshot source token: {0}")]
    Token(String),
    #[error("tls client setup failed: {0}")]
    Tls(#[from] crate::tls::TlsError),
    #[error("io error reading {path}: {source}")]
    Io {
        path: PathBuf,
        #[source]
        source: std::io::Error,
    },
    #[error("invalid snapshot json in {path}: {source}")]
    Json {
        path: PathBuf,
        #[source]
        source: serde_json::Error,
    },
    #[error("file watch failed: {0}")]
    Watch(#[from] notify::Error),
    #[error("grpc transport error: {0}")]
    Transport(#[from] tonic::transport::Error),
    #[error("grpc status: {0}")]
    Status(#[from] tonic::Status),
    #[error("snapshot receiver dropped")]
    ReceiverDropped,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Transport {
    File,
    Plaintext,
    Tls,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum SnapshotSource {
    Grpc(String),
    File(PathBuf),
    Http(String),
}

impl SnapshotSource {
    pub fn parse(spec: &str) -> Result<Self, SnapshotError> {
        let spec = spec.trim();
        if spec.is_empty() {
            return Err(SnapshotError::InvalidSource("empty".into()));
        }
        if let Some(rest) = spec.strip_prefix("grpc://") {
            return Ok(Self::Grpc(format!("http://{rest}")));
        }
        if let Some(rest) = spec.strip_prefix("grpcs://") {
            return Ok(Self::Grpc(format!("https://{rest}")));
        }
        if let Some(rest) = spec.strip_prefix("file://") {
            return Ok(Self::File(PathBuf::from(rest)));
        }
        if spec.starts_with("http://") || spec.starts_with("https://") {
            return Ok(Self::Http(spec.to_string()));
        }
        if spec.contains("://") {
            return Err(SnapshotError::InvalidSource(spec.to_string()));
        }
        Ok(Self::File(PathBuf::from(spec)))
    }

    pub fn label(&self) -> &'static str {
        match self {
            Self::Grpc(_) => "grpc",
            Self::File(_) => "file",
            Self::Http(_) => "http",
        }
    }

    pub fn location(&self) -> String {
        match self {
            Self::Grpc(url) | Self::Http(url) => url.clone(),
            Self::File(path) => path.display().to_string(),
        }
    }

    pub fn transport(&self) -> Transport {
        match self {
            Self::File(_) => Transport::File,
            Self::Grpc(url) | Self::Http(url) if url.starts_with("https://") => Transport::Tls,
            Self::Grpc(_) | Self::Http(_) => Transport::Plaintext,
        }
    }

    pub async fn run(
        &self,
        node_id: String,
        tx: watch::Sender<Arc<Snapshot>>,
        degraded: Option<crate::cp::grpc::DegradedReceiver>,
        auth: SourceAuth,
    ) -> Result<(), SnapshotError> {
        match self {
            Self::File(path) => crate::cp::file::watch(path.clone(), tx).await,
            Self::Grpc(url) => crate::cp::grpc::run(url.clone(), node_id, tx, degraded, auth).await,
            Self::Http(url) => {
                let poller =
                    crate::cp::http::Poller::new(url.clone(), crate::cp::http::client(auth.tls))
                        .with_token(auth.token);
                crate::cp::http::poll(poller, crate::cp::http::DEFAULT_POLL_INTERVAL, tx).await
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_each_scheme() {
        assert_eq!(
            SnapshotSource::parse("grpc://controller:7777").unwrap(),
            SnapshotSource::Grpc("http://controller:7777".into())
        );
        assert_eq!(
            SnapshotSource::parse("grpcs://controller:7777").unwrap(),
            SnapshotSource::Grpc("https://controller:7777".into())
        );
        assert_eq!(
            SnapshotSource::parse("file:///tmp/snapshot.json").unwrap(),
            SnapshotSource::File("/tmp/snapshot.json".into())
        );
        assert_eq!(
            SnapshotSource::parse("./snapshot.json").unwrap(),
            SnapshotSource::File("./snapshot.json".into())
        );
        assert_eq!(
            SnapshotSource::parse("https://bucket/snapshot.json").unwrap(),
            SnapshotSource::Http("https://bucket/snapshot.json".into())
        );
        assert!(SnapshotSource::parse("ftp://x").is_err());
        assert!(SnapshotSource::parse("").is_err());
    }

    #[test]
    fn transport_follows_the_scheme() {
        let transport = |spec: &str| SnapshotSource::parse(spec).unwrap().transport();
        assert_eq!(transport("grpc://controller:7700"), Transport::Plaintext);
        assert_eq!(transport("grpcs://controller:7700"), Transport::Tls);
        assert_eq!(
            transport("http://bucket/snapshot.json"),
            Transport::Plaintext
        );
        assert_eq!(transport("https://bucket/snapshot.json"), Transport::Tls);
        assert_eq!(transport("./snapshot.json"), Transport::File);
    }

    #[tokio::test]
    async fn http_source_stops_when_the_receiver_is_dropped() {
        let (tx, rx) = watch::channel(Arc::new(Snapshot::default()));
        drop(rx);
        let err = tokio::time::timeout(
            std::time::Duration::from_secs(5),
            SnapshotSource::Http("http://127.0.0.1:1/snapshot.json".into()).run(
                "node".into(),
                tx,
                None,
                SourceAuth::anonymous().unwrap(),
            ),
        )
        .await
        .expect("poll loop exits")
        .unwrap_err();
        assert!(matches!(err, SnapshotError::ReceiverDropped));
    }
}
