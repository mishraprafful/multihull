#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum AttemptError {
    Connect,
    Reset,
    FirstByteTimeout,
    IdleTimeout,
    Total,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Outcome {
    Success,
    Capacity,
    Transient,
    Fatal,
    ClientAbort,
}

impl Outcome {
    pub fn counts_toward_ejection(&self) -> bool {
        matches!(self, Outcome::Transient | Outcome::Fatal)
    }

    pub fn is_retryable_before_first_byte(&self) -> bool {
        matches!(self, Outcome::Capacity | Outcome::Transient)
    }

    pub fn label(&self) -> &'static str {
        match self {
            Outcome::Success => "success",
            Outcome::Capacity => "capacity",
            Outcome::Transient => "transient",
            Outcome::Fatal => "fatal",
            Outcome::ClientAbort => "client_abort",
        }
    }
}

pub fn classify(
    status: Option<u16>,
    error: Option<&AttemptError>,
    ttft_timed_out: bool,
) -> Outcome {
    if ttft_timed_out {
        return Outcome::Capacity;
    }
    if let Some(error) = error {
        return match error {
            AttemptError::FirstByteTimeout => Outcome::Capacity,
            AttemptError::Connect | AttemptError::Reset => Outcome::Transient,
            AttemptError::IdleTimeout | AttemptError::Total => Outcome::Transient,
        };
    }
    match status {
        None => Outcome::ClientAbort,
        Some(429) => Outcome::Capacity,
        Some(408) | Some(502..=504) => Outcome::Transient,
        Some(400..=499) => Outcome::Fatal,
        Some(500..=599) => Outcome::Fatal,
        Some(_) => Outcome::Success,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ttft_timeout_is_capacity_regardless_of_status() {
        assert_eq!(classify(Some(200), None, true), Outcome::Capacity);
        assert_eq!(
            classify(None, Some(&AttemptError::FirstByteTimeout), false),
            Outcome::Capacity
        );
    }

    #[test]
    fn status_mapping_follows_taxonomy() {
        assert_eq!(classify(Some(200), None, false), Outcome::Success);
        assert_eq!(classify(Some(204), None, false), Outcome::Success);
        assert_eq!(classify(Some(429), None, false), Outcome::Capacity);
        assert_eq!(classify(Some(502), None, false), Outcome::Transient);
        assert_eq!(classify(Some(503), None, false), Outcome::Transient);
        assert_eq!(classify(Some(504), None, false), Outcome::Transient);
        assert_eq!(classify(Some(500), None, false), Outcome::Fatal);
        assert_eq!(classify(Some(404), None, false), Outcome::Fatal);
        assert_eq!(classify(Some(401), None, false), Outcome::Fatal);
    }

    #[test]
    fn request_timeout_is_transient_not_fatal() {
        assert_eq!(classify(Some(408), None, false), Outcome::Transient);
        assert!(Outcome::Transient.is_retryable_before_first_byte());
    }

    #[test]
    fn transport_errors_are_transient() {
        assert_eq!(
            classify(None, Some(&AttemptError::Connect), false),
            Outcome::Transient
        );
        assert_eq!(
            classify(None, Some(&AttemptError::Reset), false),
            Outcome::Transient
        );
    }

    #[test]
    fn no_status_and_no_error_is_client_abort() {
        assert_eq!(classify(None, None, false), Outcome::ClientAbort);
    }

    #[test]
    fn capacity_never_ejects_but_is_retryable() {
        assert!(!Outcome::Capacity.counts_toward_ejection());
        assert!(Outcome::Capacity.is_retryable_before_first_byte());
        assert!(Outcome::Transient.counts_toward_ejection());
        assert!(Outcome::Fatal.counts_toward_ejection());
        assert!(!Outcome::Fatal.is_retryable_before_first_byte());
        assert!(!Outcome::ClientAbort.counts_toward_ejection());
    }
}
