use crate::cp::source::{SnapshotError, SnapshotSource, Transport};
use crate::tls::ClientIdentity;
use http::HeaderValue;
use rustls::ClientConfig;
use std::fmt;
use std::path::PathBuf;

pub const DEFAULT_TOKEN_ENV: &str = "MULTIHULL_DISCOVERY_TOKEN";

#[derive(Clone)]
pub struct BearerToken(HeaderValue);

impl BearerToken {
    pub fn new(token: &str) -> Result<Self, SnapshotError> {
        let token = token.trim();
        if token.is_empty() {
            return Err(SnapshotError::Token("the token is empty".into()));
        }
        let mut value = HeaderValue::from_str(&format!("Bearer {token}")).map_err(|_| {
            SnapshotError::Token("the token has characters not allowed in a header".into())
        })?;
        value.set_sensitive(true);
        Ok(Self(value))
    }

    pub fn from_env(name: &str) -> Result<Self, SnapshotError> {
        let token = std::env::var(name)
            .map_err(|_| SnapshotError::Token(format!("environment variable {name} is not set")))?;
        Self::new(&token).map_err(|error| match error {
            SnapshotError::Token(reason) => {
                SnapshotError::Token(format!("environment variable {name}: {reason}"))
            }
            other => other,
        })
    }

    pub fn header_value(&self) -> &HeaderValue {
        &self.0
    }
}

impl fmt::Debug for BearerToken {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("BearerToken(<redacted>)")
    }
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct SourceSecurity {
    pub ca: Option<PathBuf>,
    pub client_cert: Option<PathBuf>,
    pub client_key: Option<PathBuf>,
    pub token_env: Option<String>,
    pub insecure: bool,
}

#[derive(Clone, Debug)]
pub struct SourceAuth {
    pub tls: ClientConfig,
    pub token: Option<BearerToken>,
}

impl SourceAuth {
    pub fn anonymous() -> Result<Self, SnapshotError> {
        Ok(Self {
            tls: crate::tls::client_config(None)?,
            token: None,
        })
    }
}

impl SourceSecurity {
    pub fn check(&self, source: &SnapshotSource) -> Result<(), SnapshotError> {
        let tls_files =
            self.ca.is_some() || self.client_cert.is_some() || self.client_key.is_some();
        match source.transport() {
            Transport::File => {
                if tls_files || self.token_env.is_some() || self.insecure {
                    return Err(invalid(
                        "ca, client_cert, client_key, token_env and insecure apply to grpc and http \
                         sources, not to a snapshot file",
                    ));
                }
            }
            Transport::Plaintext => {
                if !self.insecure {
                    return Err(invalid(format!(
                        "{} source {} is plaintext; use grpcs:// or https://, or set \
                         insecure = true under [snapshot] for local development only",
                        source.label(),
                        source.location()
                    )));
                }
                if tls_files {
                    return Err(invalid(
                        "ca, client_cert and client_key need a grpcs:// or https:// source",
                    ));
                }
            }
            Transport::Tls => {
                if self.insecure {
                    return Err(invalid(
                        "insecure = true only permits plaintext grpc:// or http:// sources and \
                         never disables certificate checks; remove it for a TLS source",
                    ));
                }
            }
        }
        if self.client_cert.is_some() != self.client_key.is_some() {
            return Err(invalid("client_cert and client_key must be set together"));
        }
        if self
            .token_env
            .as_deref()
            .is_some_and(|name| name.trim().is_empty())
        {
            return Err(invalid("token_env names no environment variable"));
        }
        Ok(())
    }

    pub fn resolve(&self, source: &SnapshotSource) -> Result<SourceAuth, SnapshotError> {
        self.check(source)?;
        let identity = match (&self.client_cert, &self.client_key) {
            (Some(cert), Some(key)) => Some(ClientIdentity { cert, key }),
            _ => None,
        };
        let tls = crate::tls::authenticated_client_config(self.ca.as_deref(), identity)?;
        let token = self
            .token_env
            .as_deref()
            .map(BearerToken::from_env)
            .transpose()?;
        Ok(SourceAuth { tls, token })
    }
}

fn invalid(message: impl Into<String>) -> SnapshotError {
    SnapshotError::InvalidSource(message.into())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(spec: &str) -> SnapshotSource {
        SnapshotSource::parse(spec).unwrap()
    }

    fn message(result: Result<(), SnapshotError>) -> String {
        result.unwrap_err().to_string()
    }

    #[test]
    fn plaintext_sources_need_the_insecure_opt_in() {
        let open = SourceSecurity::default();
        let error = message(open.check(&parse("grpc://controller:7700")));
        assert!(error.contains("plaintext"), "{error}");
        assert!(error.contains("insecure = true"), "{error}");
        assert!(open.check(&parse("http://bucket/snapshot.json")).is_err());

        let insecure = SourceSecurity {
            insecure: true,
            token_env: Some("TOKEN".into()),
            ..Default::default()
        };
        assert!(insecure.check(&parse("grpc://controller:7700")).is_ok());
        assert!(insecure
            .check(&parse("http://bucket/snapshot.json"))
            .is_ok());

        let with_ca = SourceSecurity {
            insecure: true,
            ca: Some("/ca.pem".into()),
            ..Default::default()
        };
        assert!(message(with_ca.check(&parse("grpc://controller:7700"))).contains("grpcs://"));
    }

    #[test]
    fn tls_sources_reject_insecure_and_half_an_identity() {
        let source = parse("grpcs://controller:7700");
        assert!(SourceSecurity::default().check(&source).is_ok());
        assert!(SourceSecurity::default()
            .check(&parse("https://bucket/snapshot.json"))
            .is_ok());
        let insecure = SourceSecurity {
            insecure: true,
            ..Default::default()
        };
        assert!(message(insecure.check(&source)).contains("never disables"));
        let half = SourceSecurity {
            client_cert: Some("/tls.crt".into()),
            ..Default::default()
        };
        assert!(message(half.check(&source)).contains("together"));
        let blank = SourceSecurity {
            token_env: Some(" ".into()),
            ..Default::default()
        };
        assert!(message(blank.check(&source)).contains("token_env"));
    }

    #[test]
    fn file_sources_take_no_transport_settings() {
        let source = parse("file:///var/lib/multihull/snapshot.json");
        assert!(SourceSecurity::default().check(&source).is_ok());
        for security in [
            SourceSecurity {
                insecure: true,
                ..Default::default()
            },
            SourceSecurity {
                token_env: Some("TOKEN".into()),
                ..Default::default()
            },
            SourceSecurity {
                ca: Some("/ca.pem".into()),
                ..Default::default()
            },
        ] {
            assert!(message(security.check(&source)).contains("not to a snapshot file"));
        }
    }

    #[test]
    fn bearer_token_is_sensitive_and_never_printed() {
        let token = BearerToken::new("  s3cr3t-value \n").unwrap();
        assert_eq!(
            token.header_value().to_str().unwrap(),
            "Bearer s3cr3t-value"
        );
        assert!(token.header_value().is_sensitive());
        assert!(!format!("{token:?}").contains("s3cr3t"));
        assert!(matches!(
            BearerToken::new(" "),
            Err(SnapshotError::Token(_))
        ));
        assert!(matches!(
            BearerToken::new("bad\u{7f}value"),
            Err(SnapshotError::Token(_))
        ));
    }

    #[test]
    fn resolve_reads_the_token_from_the_named_variable() {
        let name = "ROUTER_CP_TEST_RESOLVE_TOKEN";
        let security = SourceSecurity {
            token_env: Some(name.into()),
            ..Default::default()
        };
        let source = parse("grpcs://controller:7700");
        std::env::remove_var(name);
        let error = security.resolve(&source).unwrap_err().to_string();
        assert!(error.contains(name), "{error}");
        std::env::set_var(name, "from-env");
        let auth = security.resolve(&source).unwrap();
        assert_eq!(
            auth.token.unwrap().header_value().to_str().unwrap(),
            "Bearer from-env"
        );
        std::env::set_var(name, "");
        let error = security.resolve(&source).unwrap_err().to_string();
        assert!(error.contains("empty"), "{error}");
        assert!(!error.contains("from-env"), "{error}");
        std::env::remove_var(name);
    }
}
