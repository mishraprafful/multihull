use crate::core::rng::Rng;
use crate::core::snapshot::EndpointId;

pub const OVERPROVISION_FACTOR: f64 = 1.4;

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash)]
pub enum Preset {
    #[default]
    PrioritySpillover,
    Weighted,
    EwmaLatency,
    Locality,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Candidate {
    pub id: EndpointId,
    pub priority: u32,
    pub weight: u32,
    pub region: String,
}

impl Candidate {
    pub fn new(id: impl Into<EndpointId>, priority: u32) -> Self {
        Self {
            id: id.into(),
            priority,
            weight: 1,
            region: String::new(),
        }
    }

    pub fn with_weight(mut self, weight: u32) -> Self {
        self.weight = weight;
        self
    }

    pub fn with_region(mut self, region: impl Into<String>) -> Self {
        self.region = region.into();
        self
    }
}

pub trait EndpointStateView {
    fn available(&self, id: &str) -> bool;
    fn outstanding(&self, id: &str) -> u32;
    fn ewma_ttft_secs(&self, id: &str) -> Option<f64>;
    fn local_region(&self) -> Option<&str> {
        None
    }
}

pub fn select(
    candidates: &[Candidate],
    preset: Preset,
    state: &impl EndpointStateView,
    rng: &mut impl Rng,
) -> Option<EndpointId> {
    match preset {
        Preset::PrioritySpillover => priority_spillover(candidates, state, rng),
        Preset::Weighted => weighted(candidates, state, rng),
        Preset::EwmaLatency => ewma_latency(candidates, state, rng),
        Preset::Locality => locality(candidates, state, rng),
    }
}

pub fn priority_level_loads(
    candidates: &[Candidate],
    state: &impl EndpointStateView,
) -> Vec<(u32, f64)> {
    let mut levels: Vec<u32> = candidates.iter().map(|c| c.priority).collect();
    levels.sort_unstable();
    levels.dedup();
    let mut remaining = 1.0;
    let mut loads = Vec::with_capacity(levels.len());
    for level in levels {
        let in_level: Vec<&Candidate> = candidates.iter().filter(|c| c.priority == level).collect();
        let available = in_level.iter().filter(|c| state.available(&c.id)).count();
        let health = available as f64 / in_level.len() as f64;
        let adjusted = (health * OVERPROVISION_FACTOR).min(1.0);
        let load = adjusted.min(remaining);
        remaining -= load;
        loads.push((level, load));
        if remaining <= f64::EPSILON {
            break;
        }
    }
    loads
}

fn priority_spillover(
    candidates: &[Candidate],
    state: &impl EndpointStateView,
    rng: &mut impl Rng,
) -> Option<EndpointId> {
    let loads = priority_level_loads(candidates, state);
    let total: f64 = loads.iter().map(|(_, load)| load).sum();
    if total <= 0.0 {
        return None;
    }
    let mut pick = rng.next_f64() * total;
    let mut chosen = loads.last().map(|(level, _)| *level)?;
    for (level, load) in &loads {
        if *load <= 0.0 {
            continue;
        }
        if pick < *load {
            chosen = *level;
            break;
        }
        pick -= load;
    }
    let pool: Vec<&Candidate> = candidates
        .iter()
        .filter(|c| c.priority == chosen && state.available(&c.id))
        .collect();
    if pool.is_empty() {
        return best_available_level(candidates, state, rng);
    }
    power_of_two_choices(&pool, rng, |c| f64::from(state.outstanding(&c.id)))
}

fn best_available_level(
    candidates: &[Candidate],
    state: &impl EndpointStateView,
    rng: &mut impl Rng,
) -> Option<EndpointId> {
    let available: Vec<&Candidate> = candidates
        .iter()
        .filter(|c| state.available(&c.id))
        .collect();
    let level = available.iter().map(|c| c.priority).min()?;
    let pool: Vec<&Candidate> = available
        .into_iter()
        .filter(|c| c.priority == level)
        .collect();
    power_of_two_choices(&pool, rng, |c| f64::from(state.outstanding(&c.id)))
}

fn weighted(
    candidates: &[Candidate],
    state: &impl EndpointStateView,
    rng: &mut impl Rng,
) -> Option<EndpointId> {
    let pool: Vec<&Candidate> = candidates
        .iter()
        .filter(|c| state.available(&c.id))
        .collect();
    let total: u64 = pool.iter().map(|c| u64::from(c.weight.max(1))).sum();
    if total == 0 {
        return None;
    }
    let mut pick = (rng.next_f64() * total as f64) as u64;
    for candidate in &pool {
        let weight = u64::from(candidate.weight.max(1));
        if pick < weight {
            return Some(candidate.id.clone());
        }
        pick -= weight;
    }
    pool.last().map(|c| c.id.clone())
}

fn ewma_latency(
    candidates: &[Candidate],
    state: &impl EndpointStateView,
    rng: &mut impl Rng,
) -> Option<EndpointId> {
    let pool: Vec<&Candidate> = candidates
        .iter()
        .filter(|c| state.available(&c.id))
        .collect();
    power_of_two_choices(&pool, rng, |c| {
        let latency = state.ewma_ttft_secs(&c.id).unwrap_or(0.0);
        latency * (1.0 + f64::from(state.outstanding(&c.id)))
    })
}

fn locality(
    candidates: &[Candidate],
    state: &impl EndpointStateView,
    rng: &mut impl Rng,
) -> Option<EndpointId> {
    if let Some(region) = state.local_region() {
        let local: Vec<Candidate> = candidates
            .iter()
            .filter(|c| c.region == region)
            .cloned()
            .collect();
        if let Some(id) = priority_spillover(&local, state, rng) {
            return Some(id);
        }
    }
    priority_spillover(candidates, state, rng)
}

fn power_of_two_choices(
    pool: &[&Candidate],
    rng: &mut impl Rng,
    cost: impl Fn(&Candidate) -> f64,
) -> Option<EndpointId> {
    match pool.len() {
        0 => None,
        1 => Some(pool[0].id.clone()),
        n => {
            let first = rng.next_below(n);
            let mut second = rng.next_below(n - 1);
            if second >= first {
                second += 1;
            }
            let a = pool[first];
            let b = pool[second];
            let chosen = if cost(b) < cost(a) { b } else { a };
            Some(chosen.id.clone())
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::core::rng::SeededRng;
    use std::collections::{HashMap, HashSet};

    #[derive(Default)]
    struct TestState {
        unavailable: HashSet<String>,
        outstanding: HashMap<String, u32>,
        ttft: HashMap<String, f64>,
        region: Option<String>,
    }

    impl EndpointStateView for TestState {
        fn available(&self, id: &str) -> bool {
            !self.unavailable.contains(id)
        }
        fn outstanding(&self, id: &str) -> u32 {
            self.outstanding.get(id).copied().unwrap_or(0)
        }
        fn ewma_ttft_secs(&self, id: &str) -> Option<f64> {
            self.ttft.get(id).copied()
        }
        fn local_region(&self) -> Option<&str> {
            self.region.as_deref()
        }
    }

    struct FlappingState {
        calls: std::cell::Cell<u32>,
        flips_after: u32,
        flapping: String,
    }

    impl EndpointStateView for FlappingState {
        fn available(&self, id: &str) -> bool {
            self.calls.set(self.calls.get() + 1);
            id != self.flapping || self.calls.get() <= self.flips_after
        }
        fn outstanding(&self, _id: &str) -> u32 {
            0
        }
        fn ewma_ttft_secs(&self, _id: &str) -> Option<f64> {
            None
        }
        fn local_region(&self) -> Option<&str> {
            None
        }
    }

    #[test]
    fn priority_spillover_falls_back_when_the_chosen_level_loses_headroom_mid_selection() {
        let candidates = vec![
            Candidate::new("p1".to_string(), 1),
            Candidate::new("p2".to_string(), 2),
            Candidate::new("p3".to_string(), 3),
        ];
        let state = FlappingState {
            calls: std::cell::Cell::new(0),
            flips_after: 3,
            flapping: "p1".to_string(),
        };
        let mut rng = SeededRng::new(1);
        for _ in 0..20 {
            let chosen = select(&candidates, Preset::PrioritySpillover, &state, &mut rng);
            assert!(
                matches!(chosen.as_deref(), Some("p2") | Some("p1")),
                "{chosen:?}"
            );
        }
    }

    fn tally(
        candidates: &[Candidate],
        preset: Preset,
        state: &TestState,
        n: usize,
    ) -> HashMap<String, usize> {
        let mut rng = SeededRng::new(99);
        let mut counts = HashMap::new();
        for _ in 0..n {
            if let Some(id) = select(candidates, preset, state, &mut rng) {
                *counts.entry(id).or_insert(0) += 1;
            }
        }
        counts
    }

    fn two_tier() -> Vec<Candidate> {
        vec![
            Candidate::new("p1-a", 1),
            Candidate::new("p1-b", 1),
            Candidate::new("p2-a", 2),
            Candidate::new("p2-b", 2),
        ]
    }

    #[test]
    fn healthy_primary_tier_takes_all_traffic() {
        let counts = tally(
            &two_tier(),
            Preset::PrioritySpillover,
            &TestState::default(),
            1000,
        );
        assert_eq!(counts.get("p2-a"), None);
        assert_eq!(counts.get("p2-b"), None);
        assert_eq!(counts.values().sum::<usize>(), 1000);
    }

    #[test]
    fn half_healthy_primary_spills_thirty_percent_with_overprovision() {
        let state = TestState {
            unavailable: HashSet::from(["p1-b".to_string()]),
            ..Default::default()
        };
        let loads = priority_level_loads(&two_tier(), &state);
        assert_eq!(loads.len(), 2);
        assert!((loads[0].1 - 0.7).abs() < 1e-9);
        assert!((loads[1].1 - 0.3).abs() < 1e-9);
        let counts = tally(&two_tier(), Preset::PrioritySpillover, &state, 10_000);
        let secondary = counts.get("p2-a").unwrap_or(&0) + counts.get("p2-b").unwrap_or(&0);
        assert!((2500..3500).contains(&secondary), "{secondary}");
        assert_eq!(counts.get("p1-b"), None);
    }

    #[test]
    fn dead_primary_sends_everything_to_secondary() {
        let state = TestState {
            unavailable: HashSet::from(["p1-a".to_string(), "p1-b".to_string()]),
            ..Default::default()
        };
        let counts = tally(&two_tier(), Preset::PrioritySpillover, &state, 500);
        assert_eq!(counts.get("p1-a"), None);
        assert_eq!(counts.get("p1-b"), None);
        assert_eq!(counts.values().sum::<usize>(), 500);
    }

    #[test]
    fn nothing_available_returns_none() {
        let state = TestState {
            unavailable: HashSet::from(["a".to_string()]),
            ..Default::default()
        };
        let mut rng = SeededRng::new(1);
        assert_eq!(
            select(
                &[Candidate::new("a", 1)],
                Preset::PrioritySpillover,
                &state,
                &mut rng
            ),
            None
        );
        assert_eq!(select(&[], Preset::Weighted, &state, &mut rng), None);
    }

    #[test]
    fn p2c_prefers_less_loaded_endpoint() {
        let state = TestState {
            outstanding: HashMap::from([("p1-a".to_string(), 50), ("p1-b".to_string(), 1)]),
            ..Default::default()
        };
        let counts = tally(&two_tier(), Preset::PrioritySpillover, &state, 1000);
        assert_eq!(counts.get("p1-b"), Some(&1000));
    }

    #[test]
    fn weighted_splits_by_weight() {
        let candidates = vec![
            Candidate::new("a", 1).with_weight(3),
            Candidate::new("b", 1).with_weight(1),
        ];
        let counts = tally(&candidates, Preset::Weighted, &TestState::default(), 4000);
        let a = *counts.get("a").unwrap();
        assert!((2800..3200).contains(&a), "{a}");
    }

    #[test]
    fn ewma_prefers_faster_endpoint() {
        let state = TestState {
            ttft: HashMap::from([("p1-a".to_string(), 2.0), ("p1-b".to_string(), 0.2)]),
            ..Default::default()
        };
        let candidates = vec![Candidate::new("p1-a", 1), Candidate::new("p1-b", 1)];
        let counts = tally(&candidates, Preset::EwmaLatency, &state, 500);
        assert_eq!(counts.get("p1-b"), Some(&500));
    }

    #[test]
    fn locality_prefers_local_region_then_falls_back() {
        let candidates = vec![
            Candidate::new("eu", 1).with_region("eu"),
            Candidate::new("us", 1).with_region("us"),
        ];
        let mut state = TestState {
            region: Some("eu".into()),
            ..Default::default()
        };
        let counts = tally(&candidates, Preset::Locality, &state, 200);
        assert_eq!(counts.get("eu"), Some(&200));
        state.unavailable.insert("eu".into());
        let counts = tally(&candidates, Preset::Locality, &state, 200);
        assert_eq!(counts.get("us"), Some(&200));
    }
}
