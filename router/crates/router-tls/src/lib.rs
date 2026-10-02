use arc_swap::ArcSwap;
use bytes::Bytes;
use http_body_util::Full;
use hyper_rustls::{HttpsConnector, HttpsConnectorBuilder};
use hyper_util::client::legacy::connect::HttpConnector;
use hyper_util::client::legacy::Client;
use rustls::crypto::CryptoProvider;
use rustls::{ClientConfig, RootCertStore, ServerConfig};
use rustls_pki_types::pem::PemObject;
use rustls_pki_types::{CertificateDer, PrivateKeyDer};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;
use tokio_rustls::TlsAcceptor;

pub type HttpsClient = Client<HttpsConnector<HttpConnector>, Full<Bytes>>;

#[derive(Debug, thiserror::Error)]
pub enum TlsError {
    #[error("reading {path}: {source}")]
    Io {
        path: PathBuf,
        #[source]
        source: std::io::Error,
    },
    #[error("invalid pem in {path}: {source}")]
    Pem {
        path: PathBuf,
        #[source]
        source: rustls_pki_types::pem::Error,
    },
    #[error("no certificates found in {0}")]
    NoCertificates(PathBuf),
    #[error("tls configuration rejected: {0}")]
    Rustls(#[from] rustls::Error),
}

pub fn crypto_provider() -> Arc<CryptoProvider> {
    Arc::new(rustls::crypto::ring::default_provider())
}

pub fn root_store(extra_ca: Option<&Path>) -> Result<RootCertStore, TlsError> {
    let mut roots = RootCertStore {
        roots: webpki_roots::TLS_SERVER_ROOTS.to_vec(),
    };
    if let Some(path) = extra_ca {
        for cert in certificates_from_file(path)? {
            roots.add(cert)?;
        }
    }
    Ok(roots)
}

pub fn client_config(extra_ca: Option<&Path>) -> Result<ClientConfig, TlsError> {
    Ok(ClientConfig::builder_with_provider(crypto_provider())
        .with_safe_default_protocol_versions()?
        .with_root_certificates(root_store(extra_ca)?)
        .with_no_client_auth())
}

pub fn https_client(
    connect_timeout: Duration,
    extra_ca: Option<&Path>,
) -> Result<HttpsClient, TlsError> {
    let mut http = HttpConnector::new();
    http.set_connect_timeout(Some(connect_timeout));
    http.set_nodelay(true);
    http.enforce_http(false);
    let connector = HttpsConnectorBuilder::new()
        .with_tls_config(client_config(extra_ca)?)
        .https_or_http()
        .enable_all_versions()
        .wrap_connector(http);
    Ok(Client::builder(hyper_util::rt::TokioExecutor::new())
        .pool_idle_timeout(Duration::from_secs(90))
        .build(connector))
}

pub fn certificates_from_file(path: &Path) -> Result<Vec<CertificateDer<'static>>, TlsError> {
    let bytes = std::fs::read(path).map_err(|source| TlsError::Io {
        path: path.to_path_buf(),
        source,
    })?;
    certificates_from_pem(&bytes).map_err(|source| TlsError::Pem {
        path: path.to_path_buf(),
        source,
    })
}

pub fn certificates_from_pem(
    pem: &[u8],
) -> Result<Vec<CertificateDer<'static>>, rustls_pki_types::pem::Error> {
    CertificateDer::pem_slice_iter(pem).collect()
}

pub fn private_key_from_pem(
    pem: &[u8],
) -> Result<PrivateKeyDer<'static>, rustls_pki_types::pem::Error> {
    PrivateKeyDer::from_pem_slice(pem)
}

pub fn server_config_from_pem(cert_pem: &[u8], key_pem: &[u8]) -> Result<ServerConfig, TlsError> {
    let certs = certificates_from_pem(cert_pem).map_err(|source| TlsError::Pem {
        path: PathBuf::from("<inline cert>"),
        source,
    })?;
    let key = private_key_from_pem(key_pem).map_err(|source| TlsError::Pem {
        path: PathBuf::from("<inline key>"),
        source,
    })?;
    build_server_config(certs, key)
}

pub fn server_config(cert: &Path, key: &Path) -> Result<ServerConfig, TlsError> {
    let certs = certificates_from_file(cert)?;
    if certs.is_empty() {
        return Err(TlsError::NoCertificates(cert.to_path_buf()));
    }
    let key_bytes = std::fs::read(key).map_err(|source| TlsError::Io {
        path: key.to_path_buf(),
        source,
    })?;
    let key = private_key_from_pem(&key_bytes).map_err(|source| TlsError::Pem {
        path: key.to_path_buf(),
        source,
    })?;
    build_server_config(certs, key)
}

fn build_server_config(
    certs: Vec<CertificateDer<'static>>,
    key: PrivateKeyDer<'static>,
) -> Result<ServerConfig, TlsError> {
    let mut config = ServerConfig::builder_with_provider(crypto_provider())
        .with_safe_default_protocol_versions()?
        .with_no_client_auth()
        .with_single_cert(certs, key)?;
    config.alpn_protocols = vec![b"h2".to_vec(), b"http/1.1".to_vec()];
    Ok(config)
}

pub struct TlsReloader {
    cert: PathBuf,
    key: PathBuf,
    config: ArcSwap<ServerConfig>,
}

impl TlsReloader {
    pub fn new(cert: PathBuf, key: PathBuf) -> Result<Self, TlsError> {
        let config = server_config(&cert, &key)?;
        Ok(Self {
            cert,
            key,
            config: ArcSwap::from_pointee(config),
        })
    }

    pub fn from_config(config: ServerConfig) -> Self {
        Self {
            cert: PathBuf::new(),
            key: PathBuf::new(),
            config: ArcSwap::from_pointee(config),
        }
    }

    pub fn reload(&self) -> Result<(), TlsError> {
        let config = server_config(&self.cert, &self.key)?;
        self.config.store(Arc::new(config));
        tracing::info!(cert = %self.cert.display(), "tls certificate reloaded");
        Ok(())
    }

    pub fn replace(&self, config: ServerConfig) {
        self.config.store(Arc::new(config));
    }

    pub fn acceptor(&self) -> TlsAcceptor {
        TlsAcceptor::from(self.config.load_full())
    }

    pub fn config(&self) -> Arc<ServerConfig> {
        self.config.load_full()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn self_signed() -> (String, String) {
        let certified = rcgen::generate_simple_self_signed(vec!["localhost".to_string()]).unwrap();
        (certified.cert.pem(), certified.signing_key.serialize_pem())
    }

    #[test]
    fn client_config_accepts_an_extra_ca_file() {
        let dir = tempfile::tempdir().unwrap();
        let (cert, _) = self_signed();
        let ca = dir.path().join("ca.pem");
        std::fs::write(&ca, cert).unwrap();
        let baseline = root_store(None).unwrap().len();
        let with_extra = root_store(Some(&ca)).unwrap().len();
        assert_eq!(with_extra, baseline + 1);
        assert!(client_config(Some(&ca)).is_ok());
        assert!(matches!(
            client_config(Some(&dir.path().join("missing.pem"))),
            Err(TlsError::Io { .. })
        ));
        std::fs::write(&ca, b"not pem").unwrap();
        assert!(root_store(Some(&ca)).unwrap().len() == baseline);
    }

    #[test]
    fn server_config_loads_pem_files_and_reloads() {
        let dir = tempfile::tempdir().unwrap();
        let (cert, key) = self_signed();
        let cert_path = dir.path().join("tls.crt");
        let key_path = dir.path().join("tls.key");
        std::fs::write(&cert_path, &cert).unwrap();
        std::fs::write(&key_path, &key).unwrap();
        let reloader = TlsReloader::new(cert_path.clone(), key_path.clone()).unwrap();
        let before = reloader.config();
        assert_eq!(before.alpn_protocols.len(), 2);

        let (new_cert, new_key) = self_signed();
        std::fs::write(&cert_path, new_cert).unwrap();
        std::fs::write(&key_path, new_key).unwrap();
        reloader.reload().unwrap();
        assert!(!Arc::ptr_eq(&before, &reloader.config()));

        std::fs::write(&cert_path, b"garbage").unwrap();
        let kept = reloader.config();
        assert!(matches!(
            reloader.reload(),
            Err(TlsError::NoCertificates(_))
        ));
        assert!(Arc::ptr_eq(&kept, &reloader.config()));
        std::fs::remove_file(&key_path).unwrap();
        std::fs::write(&cert_path, &cert).unwrap();
        assert!(matches!(reloader.reload(), Err(TlsError::Io { .. })));
    }

    #[test]
    fn inline_pem_builds_a_server_config() {
        let (cert, key) = self_signed();
        assert!(server_config_from_pem(cert.as_bytes(), key.as_bytes()).is_ok());
        assert!(server_config_from_pem(b"nope", key.as_bytes()).is_err());
    }

    #[test]
    fn https_client_builds_with_default_roots() {
        assert!(https_client(Duration::from_secs(1), None).is_ok());
    }
}
