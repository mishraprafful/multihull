use dashmap::DashMap;
use router_core::limit::AdmissionQueue;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::sync::Notify;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AdmissionError {
    QueueFull,
    Timeout,
}

pub struct Admission {
    config: AdmissionQueue,
    waiting: DashMap<String, Arc<AtomicUsize>>,
    released: Arc<Notify>,
}

impl Admission {
    pub fn new(config: AdmissionQueue, released: Arc<Notify>) -> Self {
        Self {
            config,
            waiting: DashMap::new(),
            released,
        }
    }

    pub fn config(&self) -> &AdmissionQueue {
        &self.config
    }

    pub fn waiting(&self, route_id: &str) -> usize {
        self.waiting
            .get(route_id)
            .map(|count| count.load(Ordering::Relaxed))
            .unwrap_or(0)
    }

    pub async fn wait_for_slot(
        &self,
        route_id: &str,
        has_headroom: impl FnMut() -> bool,
    ) -> Result<Duration, AdmissionError> {
        let deadline = Instant::now() + self.config.max_wait;
        self.wait_for_slot_until(route_id, deadline, has_headroom)
            .await
    }

    pub async fn wait_for_slot_until(
        &self,
        route_id: &str,
        deadline: Instant,
        mut has_headroom: impl FnMut() -> bool,
    ) -> Result<Duration, AdmissionError> {
        if has_headroom() {
            return Ok(Duration::ZERO);
        }
        let counter = self
            .waiting
            .entry(route_id.to_string())
            .or_insert_with(|| Arc::new(AtomicUsize::new(0)))
            .clone();
        if counter.fetch_add(1, Ordering::SeqCst) >= self.config.bound {
            counter.fetch_sub(1, Ordering::SeqCst);
            return Err(AdmissionError::QueueFull);
        }
        let _slot = QueueSlot(counter);
        let started = Instant::now();
        let deadline = tokio::time::Instant::from_std(deadline);
        loop {
            let notified = self.released.notified();
            tokio::pin!(notified);
            notified.as_mut().enable();
            if has_headroom() {
                return Ok(started.elapsed());
            }
            tokio::select! {
                _ = notified => {}
                _ = tokio::time::sleep_until(deadline) => return Err(AdmissionError::Timeout),
            }
        }
    }

    pub fn retain_routes(&self, keep: impl Fn(&str) -> bool) {
        self.waiting.retain(|route, _| keep(route));
    }
}

struct QueueSlot(Arc<AtomicUsize>);

impl Drop for QueueSlot {
    fn drop(&mut self) {
        self.0.fetch_sub(1, Ordering::SeqCst);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::AtomicBool;

    fn admission(max_wait: Duration, bound: usize) -> Arc<Admission> {
        Arc::new(Admission::new(
            AdmissionQueue { max_wait, bound },
            Arc::new(Notify::new()),
        ))
    }

    #[tokio::test]
    async fn immediate_headroom_does_not_wait() {
        let admission = admission(Duration::from_secs(1), 4);
        let waited = admission.wait_for_slot("r", || true).await.unwrap();
        assert_eq!(waited, Duration::ZERO);
        assert_eq!(admission.waiting("r"), 0);
    }

    #[tokio::test]
    async fn waiter_wakes_when_a_slot_is_released() {
        let admission = admission(Duration::from_secs(5), 4);
        let free = Arc::new(AtomicBool::new(false));
        let waiter = {
            let admission = admission.clone();
            let free = free.clone();
            tokio::spawn(async move {
                admission
                    .wait_for_slot("r", || free.load(Ordering::SeqCst))
                    .await
            })
        };
        tokio::time::sleep(Duration::from_millis(20)).await;
        assert_eq!(admission.waiting("r"), 1);
        free.store(true, Ordering::SeqCst);
        admission.released.notify_waiters();
        let waited = waiter.await.unwrap().unwrap();
        assert!(waited >= Duration::from_millis(15));
        assert_eq!(admission.waiting("r"), 0);
    }

    #[tokio::test]
    async fn timeout_and_overflow_are_distinguished() {
        let admission = admission(Duration::from_millis(30), 1);
        let first = {
            let admission = admission.clone();
            tokio::spawn(async move { admission.wait_for_slot("r", || false).await })
        };
        tokio::time::sleep(Duration::from_millis(5)).await;
        assert_eq!(
            admission.wait_for_slot("r", || false).await,
            Err(AdmissionError::QueueFull)
        );
        assert_eq!(first.await.unwrap(), Err(AdmissionError::Timeout));
        assert_eq!(admission.waiting("r"), 0);
        admission.retain_routes(|_| false);
        assert_eq!(admission.waiting("r"), 0);
    }
}
