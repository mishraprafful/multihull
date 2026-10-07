use serde::{Deserialize, Deserializer, Serializer};
use std::time::Duration;

pub fn serialize<S: Serializer>(value: &Duration, serializer: S) -> Result<S::Ok, S::Error> {
    serializer.serialize_f64(value.as_secs_f64())
}

pub fn deserialize<'de, D: Deserializer<'de>>(deserializer: D) -> Result<Duration, D::Error> {
    let secs = f64::deserialize(deserializer)?;
    if !secs.is_finite() || secs < 0.0 {
        return Err(serde::de::Error::custom(
            "duration must be a non-negative number of seconds",
        ));
    }
    Duration::try_from_secs_f64(secs)
        .map_err(|_| serde::de::Error::custom(format!("duration of {secs} seconds is too large")))
}

pub fn is_whole_seconds_at_least_one(value: Duration) -> bool {
    value.as_secs() >= 1 && value.subsec_nanos() == 0
}

#[cfg(test)]
mod tests {
    use serde::{Deserialize, Serialize};
    use std::time::Duration;

    #[derive(Debug, PartialEq, Serialize, Deserialize)]
    struct Holder {
        #[serde(with = "super")]
        wait: Duration,
    }

    #[test]
    fn seconds_round_trip_as_floats_and_accept_integers() {
        let holder: Holder = serde_json::from_str(r#"{"wait":2}"#).unwrap();
        assert_eq!(holder.wait, Duration::from_secs(2));
        let holder: Holder = serde_json::from_str(r#"{"wait":1.5}"#).unwrap();
        assert_eq!(holder.wait, Duration::from_millis(1500));
        assert_eq!(serde_json::to_string(&holder).unwrap(), r#"{"wait":1.5}"#);
        assert!(serde_json::from_str::<Holder>(r#"{"wait":-1}"#).is_err());
    }

    #[test]
    fn durations_too_large_for_duration_are_errors_not_panics() {
        let error = serde_json::from_str::<Holder>(r#"{"wait":1e20}"#).unwrap_err();
        assert!(error.to_string().contains("too large"), "{error}");
        assert!(serde_json::from_str::<Holder>(r#"{"wait":1.8446744073709552e19}"#).is_err());
    }
}
