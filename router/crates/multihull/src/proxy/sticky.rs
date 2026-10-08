use crate::core::sticky::StickyKey;
use http::header::{HeaderMap, HeaderValue, COOKIE, SET_COOKIE};
use std::net::IpAddr;
use std::time::Duration;

pub const SESSION_HEADER: &str = "x-hull-session";
pub const REHOMED_HEADER: &str = "x-hull-rehomed";

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StickyPlan {
    pub primary: StickyKey,
    pub fallback: Option<StickyKey>,
}

impl StickyPlan {
    pub fn parse(key: &str, fallback_key: &str) -> Option<Self> {
        Some(Self {
            primary: StickyKey::parse(key)?,
            fallback: StickyKey::parse(fallback_key),
        })
    }

    pub fn extract(&self, headers: &HeaderMap, body: &[u8], peer: IpAddr) -> Option<Vec<u8>> {
        extract(&self.primary, headers, body, peer).or_else(|| {
            self.fallback
                .as_ref()
                .and_then(|key| extract(key, headers, body, peer))
        })
    }

    pub fn cookie_name(&self) -> Option<&str> {
        match &self.primary {
            StickyKey::Cookie(name) => Some(name),
            _ => None,
        }
    }
}

fn extract(key: &StickyKey, headers: &HeaderMap, body: &[u8], peer: IpAddr) -> Option<Vec<u8>> {
    match key {
        StickyKey::Header(name) => headers
            .get(name.as_str())
            .map(|value| value.as_bytes().to_vec())
            .filter(|value| !value.is_empty()),
        StickyKey::Cookie(name) => cookie_value(headers, name).map(|value| value.into_bytes()),
        StickyKey::BodyJsonPath(path) => {
            let value: serde_json::Value = serde_json::from_slice(body).ok()?;
            json_path(&value, path).map(|value| value.into_bytes())
        }
        StickyKey::ClientIp => Some(client_ip(headers, peer).to_string().into_bytes()),
    }
}

pub fn client_ip(headers: &HeaderMap, peer: IpAddr) -> IpAddr {
    headers
        .get("x-forwarded-for")
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.split(',').next())
        .and_then(|first| first.trim().parse().ok())
        .unwrap_or(peer)
}

pub fn cookie_value(headers: &HeaderMap, name: &str) -> Option<String> {
    headers
        .get_all(COOKIE)
        .iter()
        .filter_map(|value| value.to_str().ok())
        .flat_map(|value| value.split(';'))
        .filter_map(|pair| pair.trim().split_once('='))
        .find(|(key, _)| key.trim() == name)
        .map(|(_, value)| value.trim().to_string())
        .filter(|value| !value.is_empty())
}

pub fn json_path(root: &serde_json::Value, path: &str) -> Option<String> {
    let mut current = root;
    for segment in parse_json_path(path)? {
        current = match segment {
            PathSegment::Key(key) => current.get(key.as_str())?,
            PathSegment::Index(index) => current.get(index)?,
        };
    }
    match current {
        serde_json::Value::Null => None,
        serde_json::Value::String(text) if text.is_empty() => None,
        serde_json::Value::String(text) => Some(text.clone()),
        other => Some(other.to_string()),
    }
}

#[derive(Debug, PartialEq, Eq)]
enum PathSegment {
    Key(String),
    Index(usize),
}

fn parse_json_path(path: &str) -> Option<Vec<PathSegment>> {
    let rest = path.strip_prefix('$')?;
    let mut segments = Vec::new();
    let mut chars = rest.chars().peekable();
    while let Some(c) = chars.next() {
        match c {
            '.' => {
                let mut key = String::new();
                while let Some(&next) = chars.peek() {
                    if next == '.' || next == '[' {
                        break;
                    }
                    key.push(next);
                    chars.next();
                }
                if key.is_empty() {
                    return None;
                }
                segments.push(PathSegment::Key(key));
            }
            '[' => {
                let mut inner = String::new();
                loop {
                    let next = chars.next()?;
                    if next == ']' {
                        break;
                    }
                    inner.push(next);
                }
                let inner = inner.trim();
                let quoted = inner
                    .strip_prefix('\'')
                    .and_then(|s| s.strip_suffix('\''))
                    .or_else(|| inner.strip_prefix('"').and_then(|s| s.strip_suffix('"')));
                match quoted {
                    Some(key) => segments.push(PathSegment::Key(key.to_string())),
                    None => segments.push(PathSegment::Index(inner.parse().ok()?)),
                }
            }
            _ => return None,
        }
    }
    Some(segments)
}

pub fn mint_session_id() -> String {
    let bytes: [u8; 16] = rand::random();
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

pub fn session_cookie(
    name: &str,
    value: &str,
    ttl: Duration,
) -> Option<(http::HeaderName, HeaderValue)> {
    let cookie = format!(
        "{name}={value}; Path=/; Max-Age={}; HttpOnly; SameSite=Lax",
        ttl.as_secs()
    );
    HeaderValue::from_str(&cookie)
        .ok()
        .map(|value| (SET_COOKIE, value))
}

pub fn rehomed_header(from: &str, to: &str) -> Option<HeaderValue> {
    HeaderValue::from_str(&format!("{from}->{to}")).ok()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::Ipv4Addr;

    fn headers(pairs: &[(&'static str, &str)]) -> HeaderMap {
        let mut map = HeaderMap::new();
        for (name, value) in pairs {
            map.append(*name, HeaderValue::from_str(value).unwrap());
        }
        map
    }

    const PEER: IpAddr = IpAddr::V4(Ipv4Addr::new(10, 1, 2, 3));

    #[test]
    fn header_key_with_body_fallback() {
        let plan = StickyPlan::parse("header:X-Session-Id", "body:$.messages[0]").unwrap();
        let with_header = headers(&[("x-session-id", "abc")]);
        assert_eq!(
            plan.extract(&with_header, b"{}", PEER).as_deref(),
            Some(&b"abc"[..])
        );
        let body = br#"{"messages":[{"role":"system","content":"hi"}]}"#;
        let extracted = plan.extract(&HeaderMap::new(), body, PEER).unwrap();
        assert_eq!(
            String::from_utf8(extracted).unwrap(),
            r#"{"content":"hi","role":"system"}"#
        );
        assert_eq!(plan.extract(&HeaderMap::new(), b"not json", PEER), None);
    }

    #[test]
    fn cookie_key_reads_the_named_cookie() {
        let plan = StickyPlan::parse("cookie:hull_session", "").unwrap();
        assert_eq!(plan.cookie_name(), Some("hull_session"));
        let map = headers(&[("cookie", "theme=dark; hull_session=s1 ; other=x")]);
        assert_eq!(plan.extract(&map, b"", PEER).as_deref(), Some(&b"s1"[..]));
        assert_eq!(
            plan.extract(&headers(&[("cookie", "theme=dark")]), b"", PEER),
            None
        );
        assert!(plan.fallback.is_none());
    }

    #[test]
    fn client_ip_prefers_forwarded_for() {
        let plan = StickyPlan::parse("client-ip", "").unwrap();
        assert_eq!(
            plan.extract(&HeaderMap::new(), b"", PEER).as_deref(),
            Some(&b"10.1.2.3"[..])
        );
        let forwarded = headers(&[("x-forwarded-for", "203.0.113.9, 10.0.0.1")]);
        assert_eq!(
            plan.extract(&forwarded, b"", PEER).as_deref(),
            Some(&b"203.0.113.9"[..])
        );
        let junk = headers(&[("x-forwarded-for", "not-an-ip")]);
        assert_eq!(client_ip(&junk, PEER), PEER);
    }

    #[test]
    fn json_path_supports_dots_brackets_and_quotes() {
        let value: serde_json::Value = serde_json::from_str(
            r#"{"session_id":"s-9","user":{"id":42,"tags":["a","b"]},"empty":"","flag":true}"#,
        )
        .unwrap();
        assert_eq!(json_path(&value, "$.session_id").as_deref(), Some("s-9"));
        assert_eq!(json_path(&value, "$.user.id").as_deref(), Some("42"));
        assert_eq!(json_path(&value, "$.user.tags[1]").as_deref(), Some("b"));
        assert_eq!(
            json_path(&value, "$['user']['tags'][0]").as_deref(),
            Some("a")
        );
        assert_eq!(json_path(&value, "$.flag").as_deref(), Some("true"));
        assert_eq!(json_path(&value, "$.empty"), None);
        assert_eq!(json_path(&value, "$.missing"), None);
        assert_eq!(json_path(&value, "$.user.tags[9]"), None);
        assert_eq!(json_path(&value, "user.id"), None);
        assert_eq!(json_path(&value, "$.user.tags[x]"), None);
        assert_eq!(json_path(&value, "$..id"), None);
    }

    #[test]
    fn invalid_primary_key_disables_the_plan() {
        assert!(StickyPlan::parse("query:x", "header:y").is_none());
        assert!(StickyPlan::parse("", "").is_none());
    }

    #[test]
    fn minted_session_ids_are_hex_and_unique() {
        let a = mint_session_id();
        let b = mint_session_id();
        assert_eq!(a.len(), 32);
        assert!(a.chars().all(|c| c.is_ascii_hexdigit()));
        assert_ne!(a, b);
    }

    #[test]
    fn cookie_and_rehomed_headers_render() {
        let (name, value) = session_cookie("hull_session", "abc", Duration::from_secs(60)).unwrap();
        assert_eq!(name, SET_COOKIE);
        assert_eq!(
            value,
            "hull_session=abc; Path=/; Max-Age=60; HttpOnly; SameSite=Lax"
        );
        assert_eq!(rehomed_header("a", "b").unwrap(), "a->b");
    }
}
