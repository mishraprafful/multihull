use crate::outcome::{AttemptError, Outcome};
use std::collections::VecDeque;
use std::time::Duration;

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
struct Bucket {
    second: u64,
    requests: u32,
    retries: u32,
}

#[derive(Clone, Debug)]
pub struct RetryBudget {
    pub ratio: f64,
    pub window: Duration,
    pub min_retries: u32,
    buckets: VecDeque<Bucket>,
}

impl Default for RetryBudget {
    fn default() -> Self {
        Self::new(0.2, Duration::from_secs(10), 3)
    }
}

impl RetryBudget {
    pub fn new(ratio: f64, window: Duration, min_retries: u32) -> Self {
        Self {
            ratio,
            window,
            min_retries,
            buckets: VecDeque::new(),
        }
    }

    pub fn record_request(&mut self, now: Duration) {
        self.bucket_mut(now).requests += 1;
    }

    pub fn remaining(&mut self, now: Duration) -> u32 {
        self.prune(now);
        let (requests, retries) = self.totals();
        let allowed = ((f64::from(requests) * self.ratio).floor() as u32).max(self.min_retries);
        allowed.saturating_sub(retries)
    }

    pub fn try_acquire(&mut self, now: Duration) -> bool {
        if self.remaining(now) == 0 {
            return false;
        }
        self.bucket_mut(now).retries += 1;
        true
    }

    fn totals(&self) -> (u32, u32) {
        self.buckets
            .iter()
            .fold((0, 0), |(req, ret), b| (req + b.requests, ret + b.retries))
    }

    fn bucket_mut(&mut self, now: Duration) -> &mut Bucket {
        self.prune(now);
        let second = now.as_secs();
        if self
            .buckets
            .back()
            .map(|b| b.second != second)
            .unwrap_or(true)
        {
            self.buckets.push_back(Bucket {
                second,
                ..Bucket::default()
            });
        }
        self.buckets.back_mut().expect("bucket pushed above")
    }

    fn prune(&mut self, now: Duration) {
        let oldest_kept = now
            .as_secs()
            .saturating_sub(self.window.as_secs().saturating_sub(1));
        while self
            .buckets
            .front()
            .map(|b| b.second < oldest_kept)
            .unwrap_or(false)
        {
            self.buckets.pop_front();
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum StopReason {
    Succeeded,
    BytesCommitted,
    BodyNotBuffered,
    NotRetryable,
    NotIdempotent,
    MaxRetries,
    BudgetExhausted,
}

impl StopReason {
    pub fn label(&self) -> &'static str {
        match self {
            StopReason::Succeeded => "succeeded",
            StopReason::BytesCommitted => "bytes_committed",
            StopReason::BodyNotBuffered => "body_not_buffered",
            StopReason::NotRetryable => "not_retryable",
            StopReason::NotIdempotent => "not_idempotent",
            StopReason::MaxRetries => "max_retries",
            StopReason::BudgetExhausted => "budget_exhausted",
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RetryDecision {
    Retry { exclude_provider: bool },
    Stop(StopReason),
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct RetryContext {
    pub bytes_committed: bool,
    pub body_buffered: bool,
    pub idempotent: bool,
    pub server_error: bool,
    pub retries_used: u32,
    pub max_retries: u32,
}

pub fn decide(
    outcome: Outcome,
    error: Option<&AttemptError>,
    ctx: &RetryContext,
    budget: &mut RetryBudget,
    now: Duration,
) -> RetryDecision {
    if outcome == Outcome::Success {
        return RetryDecision::Stop(StopReason::Succeeded);
    }
    if ctx.bytes_committed {
        return RetryDecision::Stop(StopReason::BytesCommitted);
    }
    if !ctx.body_buffered {
        return RetryDecision::Stop(StopReason::BodyNotBuffered);
    }
    let retryable_server_error = outcome == Outcome::Fatal && ctx.server_error;
    if !(outcome.is_retryable_before_first_byte() || retryable_server_error) {
        return RetryDecision::Stop(StopReason::NotRetryable);
    }
    let connect_failure = matches!(error, Some(AttemptError::Connect));
    if !(ctx.idempotent || outcome == Outcome::Capacity || connect_failure) {
        return RetryDecision::Stop(StopReason::NotIdempotent);
    }
    if ctx.retries_used >= ctx.max_retries {
        return RetryDecision::Stop(StopReason::MaxRetries);
    }
    if !budget.try_acquire(now) {
        return RetryDecision::Stop(StopReason::BudgetExhausted);
    }
    RetryDecision::Retry {
        exclude_provider: true,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ctx() -> RetryContext {
        RetryContext {
            bytes_committed: false,
            body_buffered: true,
            idempotent: false,
            server_error: false,
            retries_used: 0,
            max_retries: 2,
        }
    }

    fn now() -> Duration {
        Duration::from_secs(100)
    }

    #[test]
    fn budget_is_twenty_percent_with_a_floor() {
        let mut budget = RetryBudget::default();
        assert_eq!(budget.remaining(now()), 3);
        for _ in 0..100 {
            budget.record_request(now());
        }
        assert_eq!(budget.remaining(now()), 20);
        for _ in 0..20 {
            assert!(budget.try_acquire(now()));
        }
        assert!(!budget.try_acquire(now()));
        assert_eq!(budget.remaining(now()), 0);
    }

    #[test]
    fn budget_window_slides() {
        let mut budget = RetryBudget::default();
        for _ in 0..100 {
            budget.record_request(Duration::from_secs(0));
        }
        assert_eq!(budget.remaining(Duration::from_secs(9)), 20);
        assert_eq!(budget.remaining(Duration::from_secs(10)), 3);
    }

    #[test]
    fn capacity_retries_without_idempotency() {
        let mut budget = RetryBudget::default();
        assert_eq!(
            decide(Outcome::Capacity, None, &ctx(), &mut budget, now()),
            RetryDecision::Retry {
                exclude_provider: true
            }
        );
    }

    #[test]
    fn connect_failure_retries_without_idempotency() {
        let mut budget = RetryBudget::default();
        assert_eq!(
            decide(
                Outcome::Transient,
                Some(&AttemptError::Connect),
                &ctx(),
                &mut budget,
                now()
            ),
            RetryDecision::Retry {
                exclude_provider: true
            }
        );
    }

    #[test]
    fn non_idempotent_transient_status_does_not_retry() {
        let mut budget = RetryBudget::default();
        assert_eq!(
            decide(Outcome::Transient, None, &ctx(), &mut budget, now()),
            RetryDecision::Stop(StopReason::NotIdempotent)
        );
        let idempotent = RetryContext {
            idempotent: true,
            ..ctx()
        };
        assert!(matches!(
            decide(Outcome::Transient, None, &idempotent, &mut budget, now()),
            RetryDecision::Retry { .. }
        ));
    }

    #[test]
    fn fatal_server_error_retries_only_when_idempotent() {
        let mut budget = RetryBudget::default();
        let server_error = RetryContext {
            server_error: true,
            ..ctx()
        };
        assert_eq!(
            decide(Outcome::Fatal, None, &server_error, &mut budget, now()),
            RetryDecision::Stop(StopReason::NotIdempotent)
        );
        let idempotent = RetryContext {
            idempotent: true,
            ..server_error
        };
        assert!(matches!(
            decide(Outcome::Fatal, None, &idempotent, &mut budget, now()),
            RetryDecision::Retry { .. }
        ));
        let client_error = RetryContext {
            idempotent: true,
            ..ctx()
        };
        assert_eq!(
            decide(Outcome::Fatal, None, &client_error, &mut budget, now()),
            RetryDecision::Stop(StopReason::NotRetryable)
        );
    }

    #[test]
    fn committed_bytes_and_unbuffered_body_stop() {
        let mut budget = RetryBudget::default();
        let committed = RetryContext {
            bytes_committed: true,
            ..ctx()
        };
        assert_eq!(
            decide(Outcome::Capacity, None, &committed, &mut budget, now()),
            RetryDecision::Stop(StopReason::BytesCommitted)
        );
        let streaming = RetryContext {
            body_buffered: false,
            ..ctx()
        };
        assert_eq!(
            decide(Outcome::Capacity, None, &streaming, &mut budget, now()),
            RetryDecision::Stop(StopReason::BodyNotBuffered)
        );
    }

    #[test]
    fn fatal_and_success_stop() {
        let mut budget = RetryBudget::default();
        assert_eq!(
            decide(Outcome::Fatal, None, &ctx(), &mut budget, now()),
            RetryDecision::Stop(StopReason::NotRetryable)
        );
        assert_eq!(
            decide(Outcome::Success, None, &ctx(), &mut budget, now()),
            RetryDecision::Stop(StopReason::Succeeded)
        );
    }

    #[test]
    fn max_retries_and_budget_exhaustion_stop() {
        let mut budget = RetryBudget::default();
        let exhausted = RetryContext {
            retries_used: 2,
            ..ctx()
        };
        assert_eq!(
            decide(Outcome::Capacity, None, &exhausted, &mut budget, now()),
            RetryDecision::Stop(StopReason::MaxRetries)
        );
        for _ in 0..3 {
            assert!(budget.try_acquire(now()));
        }
        assert_eq!(
            decide(Outcome::Capacity, None, &ctx(), &mut budget, now()),
            RetryDecision::Stop(StopReason::BudgetExhausted)
        );
    }
}
