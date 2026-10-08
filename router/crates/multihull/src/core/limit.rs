use std::time::Duration;

#[derive(Clone, Debug, PartialEq)]
pub struct Gradient2Config {
    pub min_limit: u32,
    pub max_limit: u32,
    pub initial_limit: u32,
    pub smoothing: f64,
    pub tolerance: f64,
    pub long_window: u32,
    pub short_window: u32,
    pub capacity_backoff: f64,
}

impl Gradient2Config {
    pub fn for_max_concurrency(max_concurrency: u32) -> Self {
        let max_limit = max_concurrency.max(1);
        Self {
            min_limit: 1,
            max_limit,
            initial_limit: max_limit.min(4.max(max_limit / 4)),
            smoothing: 0.2,
            tolerance: 1.5,
            long_window: 600,
            short_window: 10,
            capacity_backoff: 0.7,
        }
    }
}

#[derive(Clone, Debug)]
pub struct Gradient2 {
    config: Gradient2Config,
    estimated: f64,
    long_ttft: Option<f64>,
    short_ttft: Option<f64>,
}

impl Gradient2 {
    pub fn new(config: Gradient2Config) -> Self {
        let estimated = f64::from(
            config
                .initial_limit
                .clamp(config.min_limit, config.max_limit),
        );
        Self {
            config,
            estimated,
            long_ttft: None,
            short_ttft: None,
        }
    }

    pub fn limit(&self) -> u32 {
        self.estimated.floor() as u32
    }

    pub fn has_headroom(&self, in_flight: u32) -> bool {
        in_flight < self.limit()
    }

    pub fn long_ttft(&self) -> Option<Duration> {
        self.long_ttft.map(Duration::from_secs_f64)
    }

    pub fn on_sample(&mut self, ttft: Duration, in_flight: u32) {
        let sample = ttft.as_secs_f64().max(1e-6);
        let short = ewma(self.short_ttft, sample, self.config.short_window);
        let long = ewma(self.long_ttft, sample, self.config.long_window);
        self.short_ttft = Some(short);
        self.long_ttft = Some(long);

        let gradient = (self.config.tolerance * long / short).clamp(0.5, 1.0);
        let queue_size = self.estimated.sqrt();
        let proposed = self.estimated * gradient + queue_size;
        let underutilised = f64::from(in_flight) * 2.0 < self.estimated;
        if underutilised && proposed > self.estimated {
            return;
        }
        let smoothed =
            self.estimated * (1.0 - self.config.smoothing) + proposed * self.config.smoothing;
        self.estimated = self.clamp(smoothed);
    }

    pub fn on_capacity(&mut self) {
        self.estimated = self.clamp(self.estimated * self.config.capacity_backoff);
    }

    fn clamp(&self, value: f64) -> f64 {
        value.clamp(
            f64::from(self.config.min_limit),
            f64::from(self.config.max_limit),
        )
    }
}

fn ewma(previous: Option<f64>, sample: f64, window: u32) -> f64 {
    match previous {
        None => sample,
        Some(previous) => {
            let alpha = 2.0 / (f64::from(window.max(1)) + 1.0);
            previous + alpha * (sample - previous)
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct AdmissionQueue {
    #[serde(with = "crate::core::serde_secs")]
    pub max_wait: Duration,
    pub bound: usize,
}

impl Default for AdmissionQueue {
    fn default() -> Self {
        Self {
            max_wait: Duration::from_secs(5),
            bound: 1024,
        }
    }
}

impl AdmissionQueue {
    pub fn validate(&self) -> Result<(), String> {
        if self.max_wait.is_zero() {
            return Err("admission.max_wait must be positive".into());
        }
        if self.bound == 0 {
            return Err("admission.bound must be at least 1".into());
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn limiter(max: u32) -> Gradient2 {
        Gradient2::new(Gradient2Config::for_max_concurrency(max))
    }

    #[test]
    fn starts_within_bounds() {
        let wide = limiter(64);
        assert!(wide.limit() >= 1 && wide.limit() <= 64);
        assert_eq!(limiter(1).limit(), 1);
    }

    #[test]
    fn stable_latency_at_full_utilisation_grows_the_limit() {
        let mut limiter = limiter(64);
        let start = limiter.limit();
        for _ in 0..200 {
            let in_flight = limiter.limit();
            limiter.on_sample(Duration::from_millis(100), in_flight);
        }
        assert!(limiter.limit() > start);
        assert!(limiter.limit() <= 64);
    }

    #[test]
    fn rising_latency_shrinks_the_limit() {
        let mut limiter = limiter(64);
        for _ in 0..200 {
            let in_flight = limiter.limit();
            limiter.on_sample(Duration::from_millis(100), in_flight);
        }
        let grown = limiter.limit();
        for _ in 0..50 {
            let in_flight = limiter.limit();
            limiter.on_sample(Duration::from_millis(2000), in_flight);
        }
        assert!(limiter.limit() < grown);
        assert!(limiter.limit() >= 1);
    }

    #[test]
    fn underutilised_limiter_does_not_grow() {
        let mut limiter = limiter(64);
        let start = limiter.limit();
        for _ in 0..200 {
            limiter.on_sample(Duration::from_millis(50), 0);
        }
        assert_eq!(limiter.limit(), start);
    }

    #[test]
    fn capacity_multiplies_by_point_seven_and_respects_floor() {
        let mut limiter = Gradient2::new(Gradient2Config {
            initial_limit: 10,
            ..Gradient2Config::for_max_concurrency(10)
        });
        assert_eq!(limiter.limit(), 10);
        limiter.on_capacity();
        assert_eq!(limiter.limit(), 7);
        for _ in 0..20 {
            limiter.on_capacity();
        }
        assert_eq!(limiter.limit(), 1);
        assert!(!limiter.has_headroom(1));
        assert!(limiter.has_headroom(0));
    }

    #[test]
    fn admission_queue_defaults_match_plan() {
        let queue = AdmissionQueue::default();
        assert_eq!(queue.max_wait, Duration::from_secs(5));
        assert!(queue.bound > 0);
        assert!(queue.validate().is_ok());
    }

    #[test]
    fn admission_queue_rejects_zero_wait_and_bound() {
        let zero_wait = AdmissionQueue {
            max_wait: Duration::ZERO,
            ..AdmissionQueue::default()
        };
        assert!(zero_wait.validate().unwrap_err().contains("max_wait"));
        let zero_bound = AdmissionQueue {
            bound: 0,
            ..AdmissionQueue::default()
        };
        assert!(zero_bound.validate().unwrap_err().contains("bound"));
        let parsed: AdmissionQueue = serde_json::from_str(r#"{"max_wait":2.5}"#).unwrap();
        assert_eq!(parsed.max_wait, Duration::from_millis(2500));
    }
}
