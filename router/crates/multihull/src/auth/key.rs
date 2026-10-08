use std::fmt;

pub const KEY_PREFIX: &str = "hull_";

#[derive(Debug, Clone, Copy, PartialEq, Eq, thiserror::Error)]
pub enum KeyError {
    #[error("missing hull_ prefix")]
    MissingPrefix,
    #[error("malformed key: expected hull_<id>_<secret>")]
    Malformed,
    #[error("key id must be alphanumeric")]
    InvalidId,
}

#[derive(Clone, PartialEq, Eq)]
pub struct ApiKey {
    id: String,
    secret: String,
}

impl fmt::Debug for ApiKey {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("ApiKey")
            .field("id", &self.id)
            .field("secret", &"<redacted>")
            .finish()
    }
}

impl ApiKey {
    pub fn parse(raw: &str) -> Result<Self, KeyError> {
        let raw = raw.trim();
        let rest = raw
            .strip_prefix(KEY_PREFIX)
            .ok_or(KeyError::MissingPrefix)?;
        let (id, secret) = rest.split_once('_').ok_or(KeyError::Malformed)?;
        if id.is_empty() || secret.is_empty() {
            return Err(KeyError::Malformed);
        }
        if !id.chars().all(|c| c.is_ascii_alphanumeric()) {
            return Err(KeyError::InvalidId);
        }
        Ok(Self {
            id: id.to_string(),
            secret: secret.to_string(),
        })
    }

    pub fn from_bearer(header_value: &str) -> Result<Self, KeyError> {
        let token = header_value
            .strip_prefix("Bearer ")
            .or_else(|| header_value.strip_prefix("bearer "))
            .unwrap_or(header_value);
        Self::parse(token)
    }

    pub fn id(&self) -> &str {
        &self.id
    }

    pub fn hash(&self) -> String {
        hash_secret(&self.id, &self.secret)
    }

    pub fn matches_any<'a>(&self, hashes: impl IntoIterator<Item = &'a str>) -> bool {
        let computed = self.hash();
        let mut matched = false;
        for candidate in hashes {
            matched |= verify(&computed, candidate);
        }
        matched
    }
}

pub fn hash_secret(id: &str, secret: &str) -> String {
    let mut hasher = blake3::Hasher::new();
    hasher.update(id.as_bytes());
    hasher.update(b"_");
    hasher.update(secret.as_bytes());
    hasher.finalize().to_hex().to_string()
}

pub fn verify(computed_hex: &str, expected_hex: &str) -> bool {
    let Ok(computed) = blake3::Hash::from_hex(computed_hex) else {
        return false;
    };
    let Ok(expected) = blake3::Hash::from_hex(expected_hex) else {
        return false;
    };
    computed == expected
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_well_formed_key() {
        let key = ApiKey::parse("hull_abc123_s3cr3t_with_underscores").unwrap();
        assert_eq!(key.id(), "abc123");
        assert_eq!(key.hash(), hash_secret("abc123", "s3cr3t_with_underscores"));
    }

    #[test]
    fn hash_matches_the_cross_language_vector() {
        let vector: serde_json::Value = serde_json::from_str(include_str!(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../../../proto/testdata/api-key-hash.json"
        )))
        .unwrap();
        let expected = vector["blake3"].as_str().unwrap();
        let key = ApiKey::parse(vector["key"].as_str().unwrap()).unwrap();
        assert_eq!(key.hash(), expected);
        let input = vector["hash_input"].as_str().unwrap();
        assert_eq!(blake3::hash(input.as_bytes()).to_hex().as_str(), expected);
        assert!(key.matches_any([expected]));
    }

    #[test]
    fn rejects_malformed_keys() {
        assert_eq!(ApiKey::parse("sk_abc_def"), Err(KeyError::MissingPrefix));
        assert_eq!(ApiKey::parse("hull_abc"), Err(KeyError::Malformed));
        assert_eq!(ApiKey::parse("hull__abc"), Err(KeyError::Malformed));
        assert_eq!(ApiKey::parse("hull_abc_"), Err(KeyError::Malformed));
        assert_eq!(ApiKey::parse("hull_a-b_abc"), Err(KeyError::InvalidId));
    }

    #[test]
    fn bearer_prefix_is_stripped() {
        let key = ApiKey::from_bearer("Bearer hull_id_secret").unwrap();
        assert_eq!(key.id(), "id");
    }

    #[test]
    fn verify_compares_hashes_and_rejects_garbage() {
        let a = hash_secret("id", "one");
        let b = hash_secret("id", "two");
        assert!(verify(&a, &a));
        assert!(!verify(&a, &b));
        assert!(!verify(&a, "not-hex"));
        assert!(!verify("short", &a));
    }

    #[test]
    fn matches_any_scans_all_hashes() {
        let key = ApiKey::parse("hull_id_secret").unwrap();
        let other = hash_secret("id", "other");
        assert!(key.matches_any([other.as_str(), key.hash().as_str()]));
        assert!(!key.matches_any([other.as_str()]));
        assert!(!key.matches_any(std::iter::empty()));
    }

    #[test]
    fn debug_output_redacts_secret() {
        let key = ApiKey::parse("hull_id_topsecret").unwrap();
        let rendered = format!("{key:?}");
        assert!(!rendered.contains("topsecret"));
        assert!(rendered.contains("redacted"));
    }
}
