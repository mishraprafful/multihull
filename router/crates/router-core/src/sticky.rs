use crate::snapshot::EndpointId;
use std::collections::{BTreeMap, HashMap};
use std::time::Duration;

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub enum StickyKey {
    Header(String),
    Cookie(String),
    BodyJsonPath(String),
    ClientIp,
}

impl StickyKey {
    pub fn parse(spec: &str) -> Option<Self> {
        let spec = spec.trim();
        if spec.eq_ignore_ascii_case("client-ip") {
            return Some(StickyKey::ClientIp);
        }
        let (kind, value) = spec.split_once(':')?;
        let value = value.trim();
        if value.is_empty() {
            return None;
        }
        match kind.trim().to_ascii_lowercase().as_str() {
            "header" => Some(StickyKey::Header(value.to_string())),
            "cookie" => Some(StickyKey::Cookie(value.to_string())),
            "body" => Some(StickyKey::BodyJsonPath(value.to_string())),
            _ => None,
        }
    }
}

pub type KeyHash = [u8; 32];

pub fn hash_key(key: &[u8]) -> KeyHash {
    *blake3::hash(key).as_bytes()
}

pub fn truncated_key_hash(key: &[u8]) -> String {
    truncate_hash(&hash_key(key))
}

pub fn truncate_hash(hash: &KeyHash) -> String {
    hash[..8].iter().map(|b| format!("{b:02x}")).collect()
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Candidate<'a> {
    pub id: &'a str,
    pub provider: &'a str,
    pub weight: u32,
}

impl<'a> Candidate<'a> {
    pub fn new(id: &'a str, provider: &'a str, weight: u32) -> Self {
        Self {
            id,
            provider,
            weight,
        }
    }
}

fn rendezvous_score(key: &[u8], candidate: &Candidate<'_>) -> f64 {
    let mut hasher = blake3::Hasher::new();
    hasher.update(key);
    hasher.update(&[0]);
    hasher.update(candidate.id.as_bytes());
    let digest = hasher.finalize();
    let bits = u64::from_le_bytes(digest.as_bytes()[..8].try_into().expect("8 bytes"));
    let unit = ((bits >> 11) as f64 + 0.5) / (1u64 << 53) as f64;
    f64::from(candidate.weight.max(1)) / -unit.ln()
}

pub fn rank<'a>(key: &[u8], candidates: &[Candidate<'a>]) -> Vec<&'a str> {
    let mut scored: Vec<(f64, &'a str)> = candidates
        .iter()
        .map(|c| (rendezvous_score(key, c), c.id))
        .collect();
    scored.sort_by(|a, b| b.0.total_cmp(&a.0).then_with(|| a.1.cmp(b.1)));
    scored.into_iter().map(|(_, id)| id).collect()
}

pub fn owner(
    key: &[u8],
    candidates: &[Candidate<'_>],
    is_healthy: impl Fn(&str) -> bool,
) -> Option<(EndpointId, bool)> {
    let ranked = rank(key, candidates);
    ranked
        .iter()
        .position(|id| is_healthy(id))
        .map(|index| (ranked[index].to_string(), index != 0))
}

pub fn owner_in_provider(
    key: &[u8],
    candidates: &[Candidate<'_>],
    is_healthy: impl Fn(&str) -> bool,
) -> Option<(EndpointId, bool)> {
    let ranked = rank(key, candidates);
    let primary = ranked.first()?;
    let primary_provider = candidates
        .iter()
        .find(|c| c.id == *primary)
        .map(|c| c.provider)?;
    let same_provider: Vec<Candidate<'_>> = candidates
        .iter()
        .filter(|c| c.provider == primary_provider)
        .cloned()
        .collect();
    owner(key, &same_provider, &is_healthy).or_else(|| owner(key, candidates, &is_healthy))
}

#[derive(Clone, Debug)]
struct Pin {
    endpoint: EndpointId,
    last_seen: Duration,
    seq: u64,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PinEntry {
    pub key_hash: KeyHash,
    pub endpoint: EndpointId,
    pub age: Duration,
}

#[derive(Clone, Debug)]
pub struct PinTable {
    capacity: usize,
    ttl: Duration,
    next_seq: u64,
    pins: HashMap<KeyHash, Pin>,
    order: BTreeMap<u64, KeyHash>,
}

impl PinTable {
    pub fn new(capacity: usize, ttl: Duration) -> Self {
        Self {
            capacity: capacity.max(1),
            ttl,
            next_seq: 0,
            pins: HashMap::new(),
            order: BTreeMap::new(),
        }
    }

    pub fn len(&self) -> usize {
        self.pins.len()
    }

    pub fn is_empty(&self) -> bool {
        self.pins.is_empty()
    }

    pub fn ttl(&self) -> Duration {
        self.ttl
    }

    pub fn entries(&self, now: Duration) -> Vec<PinEntry> {
        let mut entries: Vec<PinEntry> = self
            .pins
            .iter()
            .filter(|(_, pin)| now.saturating_sub(pin.last_seen) <= self.ttl)
            .map(|(key, pin)| PinEntry {
                key_hash: *key,
                endpoint: pin.endpoint.clone(),
                age: now.saturating_sub(pin.last_seen),
            })
            .collect();
        entries.sort_by(|a, b| a.age.cmp(&b.age).then_with(|| a.key_hash.cmp(&b.key_hash)));
        entries
    }

    pub fn get(&mut self, key: &KeyHash, now: Duration) -> Option<EndpointId> {
        let pin = self.pins.get(key)?;
        if now.saturating_sub(pin.last_seen) > self.ttl {
            self.remove(key);
            return None;
        }
        let seq = self.bump_seq();
        let pin = self.pins.get_mut(key).expect("checked above");
        self.order.remove(&pin.seq);
        pin.seq = seq;
        pin.last_seen = now;
        self.order.insert(seq, *key);
        Some(pin.endpoint.clone())
    }

    pub fn pin(&mut self, key: KeyHash, endpoint: EndpointId, now: Duration) {
        self.remove(&key);
        while self.pins.len() >= self.capacity {
            let Some((_, oldest)) = self.order.pop_first() else {
                break;
            };
            self.pins.remove(&oldest);
        }
        let seq = self.bump_seq();
        self.order.insert(seq, key);
        self.pins.insert(
            key,
            Pin {
                endpoint,
                last_seen: now,
                seq,
            },
        );
    }

    pub fn remove(&mut self, key: &KeyHash) {
        if let Some(pin) = self.pins.remove(key) {
            self.order.remove(&pin.seq);
        }
    }

    pub fn expire(&mut self, now: Duration) -> usize {
        let expired: Vec<KeyHash> = self
            .pins
            .iter()
            .filter(|(_, pin)| now.saturating_sub(pin.last_seen) > self.ttl)
            .map(|(key, _)| *key)
            .collect();
        for key in &expired {
            self.remove(key);
        }
        expired.len()
    }

    fn bump_seq(&mut self) -> u64 {
        self.next_seq += 1;
        self.next_seq
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn candidates<'a>(ids: &[(&'a str, &'a str, u32)]) -> Vec<Candidate<'a>> {
        ids.iter()
            .map(|(id, provider, weight)| Candidate::new(id, provider, *weight))
            .collect()
    }

    #[test]
    fn parses_every_key_spec() {
        assert_eq!(
            StickyKey::parse("header:X-Session-Id"),
            Some(StickyKey::Header("X-Session-Id".into()))
        );
        assert_eq!(
            StickyKey::parse("cookie:hull_session"),
            Some(StickyKey::Cookie("hull_session".into()))
        );
        assert_eq!(
            StickyKey::parse("body:$.messages[0]"),
            Some(StickyKey::BodyJsonPath("$.messages[0]".into()))
        );
        assert_eq!(StickyKey::parse("client-ip"), Some(StickyKey::ClientIp));
        assert_eq!(StickyKey::parse("header:"), None);
        assert_eq!(StickyKey::parse("query:x"), None);
    }

    #[test]
    fn owner_is_deterministic() {
        let c = candidates(&[("a", "p1", 10), ("b", "p1", 10), ("c", "p2", 10)]);
        let first = owner(b"session-1", &c, |_| true);
        let second = owner(b"session-1", &c, |_| true);
        assert_eq!(first, second);
        assert!(!first.unwrap().1);
    }

    #[test]
    fn removing_an_endpoint_moves_only_its_keys() {
        let all = candidates(&[
            ("a", "p", 10),
            ("b", "p", 10),
            ("c", "p", 10),
            ("d", "p", 10),
        ]);
        let without_c = candidates(&[("a", "p", 10), ("b", "p", 10), ("d", "p", 10)]);
        let mut moved = 0;
        let mut owned_by_c = 0;
        for i in 0..2000 {
            let key = format!("key-{i}");
            let before = owner(key.as_bytes(), &all, |_| true).unwrap().0;
            let after = owner(key.as_bytes(), &without_c, |_| true).unwrap().0;
            if before == "c" {
                owned_by_c += 1;
                assert_ne!(after, "c");
            } else {
                assert_eq!(before, after);
            }
            if before != after {
                moved += 1;
            }
        }
        assert_eq!(moved, owned_by_c);
        assert!(owned_by_c > 300 && owned_by_c < 700, "{owned_by_c}");
    }

    #[test]
    fn weight_shifts_share_proportionally() {
        let c = candidates(&[("heavy", "p", 30), ("light", "p", 10)]);
        let mut heavy = 0;
        for i in 0..4000 {
            let key = format!("k{i}");
            if owner(key.as_bytes(), &c, |_| true).unwrap().0 == "heavy" {
                heavy += 1;
            }
        }
        assert!(heavy > 2800 && heavy < 3200, "{heavy}");
    }

    #[test]
    fn unhealthy_owner_rehomes_to_next_candidate() {
        let c = candidates(&[("a", "p", 10), ("b", "p", 10), ("c", "p", 10)]);
        let key = b"sticky";
        let ranked = rank(key, &c);
        let primary = ranked[0];
        let (owner_id, rehomed) = owner(key, &c, |id| id != primary).unwrap();
        assert!(rehomed);
        assert_eq!(owner_id, ranked[1]);
        assert_eq!(owner(key, &c, |_| false), None);
    }

    #[test]
    fn provider_mode_prefers_same_provider_before_crossing() {
        let c = candidates(&[
            ("a1", "pa", 10),
            ("a2", "pa", 10),
            ("b1", "pb", 10),
            ("b2", "pb", 10),
        ]);
        for i in 0..200 {
            let key = format!("s{i}");
            let primary = rank(key.as_bytes(), &c)[0];
            let primary_provider = c.iter().find(|x| x.id == primary).unwrap().provider;
            let (rehomed_to, rehomed) =
                owner_in_provider(key.as_bytes(), &c, |id| id != primary).unwrap();
            assert!(rehomed);
            let new_provider = c.iter().find(|x| x.id == rehomed_to).unwrap().provider;
            assert_eq!(new_provider, primary_provider);
        }
    }

    #[test]
    fn pin_table_expires_and_evicts_lru() {
        let mut table = PinTable::new(2, Duration::from_secs(10));
        let k1 = hash_key(b"1");
        let k2 = hash_key(b"2");
        let k3 = hash_key(b"3");
        table.pin(k1, "a".into(), Duration::from_secs(0));
        table.pin(k2, "b".into(), Duration::from_secs(1));
        assert_eq!(table.get(&k1, Duration::from_secs(2)).as_deref(), Some("a"));
        table.pin(k3, "c".into(), Duration::from_secs(3));
        assert_eq!(table.len(), 2);
        assert_eq!(table.get(&k2, Duration::from_secs(3)), None);
        assert_eq!(table.get(&k1, Duration::from_secs(3)).as_deref(), Some("a"));
        assert_eq!(table.get(&k1, Duration::from_secs(14)), None);
        assert_eq!(table.len(), 1);
        assert_eq!(table.expire(Duration::from_secs(30)), 1);
        assert!(table.is_empty());
    }

    #[test]
    fn entries_skip_expired_pins_and_report_age() {
        let mut table = PinTable::new(8, Duration::from_secs(10));
        table.pin(hash_key(b"old"), "a".into(), Duration::from_secs(0));
        table.pin(hash_key(b"new"), "b".into(), Duration::from_secs(5));
        let entries = table.entries(Duration::from_secs(8));
        assert_eq!(entries.len(), 2);
        assert_eq!(entries[0].endpoint, "b");
        assert_eq!(entries[0].age, Duration::from_secs(3));
        let entries = table.entries(Duration::from_secs(12));
        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0].endpoint, "b");
        assert_eq!(table.ttl(), Duration::from_secs(10));
    }

    #[test]
    fn truncated_hash_is_short_and_stable() {
        let a = truncated_key_hash(b"abc");
        assert_eq!(a.len(), 16);
        assert_eq!(a, truncated_key_hash(b"abc"));
        assert_ne!(a, truncated_key_hash(b"abd"));
    }
}
