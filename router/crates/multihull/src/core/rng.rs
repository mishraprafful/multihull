pub trait Rng {
    fn next_f64(&mut self) -> f64;

    fn next_below(&mut self, bound: usize) -> usize {
        if bound == 0 {
            return 0;
        }
        let picked = (self.next_f64() * bound as f64) as usize;
        picked.min(bound - 1)
    }
}

impl<F: FnMut() -> f64> Rng for F {
    fn next_f64(&mut self) -> f64 {
        self()
    }
}

#[derive(Clone, Debug)]
pub struct SeededRng {
    state: u64,
}

impl SeededRng {
    pub fn new(seed: u64) -> Self {
        Self { state: seed.max(1) }
    }

    fn next_u64(&mut self) -> u64 {
        let mut x = self.state;
        x ^= x >> 12;
        x ^= x << 25;
        x ^= x >> 27;
        self.state = x;
        x.wrapping_mul(0x2545_F491_4F6C_DD1D)
    }
}

impl Rng for SeededRng {
    fn next_f64(&mut self) -> f64 {
        (self.next_u64() >> 11) as f64 / (1u64 << 53) as f64
    }
}

#[derive(Clone, Copy, Debug, Default)]
pub struct ZeroRng;

impl Rng for ZeroRng {
    fn next_f64(&mut self) -> f64 {
        0.0
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn seeded_rng_stays_in_unit_interval() {
        let mut rng = SeededRng::new(42);
        for _ in 0..10_000 {
            let value = rng.next_f64();
            assert!((0.0..1.0).contains(&value));
        }
    }

    #[test]
    fn next_below_never_reaches_bound() {
        let mut rng = SeededRng::new(7);
        for _ in 0..10_000 {
            assert!(rng.next_below(3) < 3);
        }
        assert_eq!(rng.next_below(0), 0);
    }

    #[test]
    fn closures_act_as_rng() {
        let mut fixed = || 0.75;
        assert_eq!(fixed.next_below(4), 3);
    }
}
