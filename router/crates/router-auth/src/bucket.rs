use std::time::Duration;

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct TokenBucketConfig {
    pub capacity: f64,
    pub refill_per_second: f64,
}

impl TokenBucketConfig {
    pub fn per_minute(requests: u32) -> Self {
        let per_second = f64::from(requests) / 60.0;
        Self {
            capacity: f64::from(requests.max(1)),
            refill_per_second: per_second,
        }
    }
}

#[derive(Clone, Debug)]
pub struct TokenBucket {
    config: TokenBucketConfig,
    tokens: f64,
    last_refill: Duration,
}

impl TokenBucket {
    pub fn new(config: TokenBucketConfig, now: Duration) -> Self {
        Self {
            config,
            tokens: config.capacity,
            last_refill: now,
        }
    }

    pub fn try_take(&mut self, now: Duration) -> bool {
        self.refill(now);
        if self.tokens >= 1.0 {
            self.tokens -= 1.0;
            true
        } else {
            false
        }
    }

    pub fn available(&mut self, now: Duration) -> f64 {
        self.refill(now);
        self.tokens
    }

    pub fn retry_after(&mut self, now: Duration) -> Duration {
        self.refill(now);
        if self.tokens >= 1.0 || self.config.refill_per_second <= 0.0 {
            return Duration::ZERO;
        }
        Duration::from_secs_f64((1.0 - self.tokens) / self.config.refill_per_second)
    }

    fn refill(&mut self, now: Duration) {
        let elapsed = now.saturating_sub(self.last_refill).as_secs_f64();
        self.tokens =
            (self.tokens + elapsed * self.config.refill_per_second).min(self.config.capacity);
        self.last_refill = now;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn bucket() -> TokenBucket {
        TokenBucket::new(
            TokenBucketConfig {
                capacity: 3.0,
                refill_per_second: 1.0,
            },
            Duration::ZERO,
        )
    }

    #[test]
    fn starts_full_and_drains() {
        let mut bucket = bucket();
        assert!(bucket.try_take(Duration::ZERO));
        assert!(bucket.try_take(Duration::ZERO));
        assert!(bucket.try_take(Duration::ZERO));
        assert!(!bucket.try_take(Duration::ZERO));
    }

    #[test]
    fn refills_over_time_and_caps_at_capacity() {
        let mut bucket = bucket();
        for _ in 0..3 {
            bucket.try_take(Duration::ZERO);
        }
        assert!(!bucket.try_take(Duration::from_millis(500)));
        assert!(bucket.try_take(Duration::from_secs(1)));
        assert_eq!(bucket.available(Duration::from_secs(100)), 3.0);
    }

    #[test]
    fn retry_after_reports_time_to_next_token() {
        let mut bucket = bucket();
        for _ in 0..3 {
            bucket.try_take(Duration::ZERO);
        }
        assert_eq!(bucket.retry_after(Duration::ZERO), Duration::from_secs(1));
        assert_eq!(bucket.retry_after(Duration::from_secs(1)), Duration::ZERO);
    }

    #[test]
    fn per_minute_config_scales_refill() {
        let config = TokenBucketConfig::per_minute(120);
        assert_eq!(config.capacity, 120.0);
        assert!((config.refill_per_second - 2.0).abs() < 1e-9);
    }
}
