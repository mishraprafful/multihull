use crate::proto;
use router_core::snapshot as core;
use std::collections::BTreeMap;

impl From<proto::Snapshot> for core::Snapshot {
    fn from(value: proto::Snapshot) -> Self {
        Self {
            version: value.version,
            at: value.at.map(|ts| core::Timestamp {
                seconds: ts.seconds,
                nanos: ts.nanos,
            }),
            routes: value.routes.into_iter().map(Into::into).collect(),
        }
    }
}

impl From<core::Snapshot> for proto::Snapshot {
    fn from(value: core::Snapshot) -> Self {
        Self {
            version: value.version,
            at: value.at.map(|ts| prost_types::Timestamp {
                seconds: ts.seconds,
                nanos: ts.nanos,
            }),
            routes: value.routes.into_iter().map(Into::into).collect(),
        }
    }
}

impl From<proto::Route> for core::Route {
    fn from(value: proto::Route) -> Self {
        Self {
            id: value.id,
            hostname: value.hostname,
            path_prefix: if value.path_prefix.is_empty() {
                "/".to_string()
            } else {
                value.path_prefix
            },
            protocol: proto::Protocol::try_from(value.protocol)
                .unwrap_or_default()
                .into(),
            failover: value.failover.map(Into::into).unwrap_or_default(),
            auth: value.auth.map(Into::into).unwrap_or_default(),
            sticky: value.sticky.map(Into::into),
            endpoints: value.endpoints.into_iter().map(Into::into).collect(),
        }
    }
}

impl From<core::Route> for proto::Route {
    fn from(value: core::Route) -> Self {
        Self {
            id: value.id,
            hostname: value.hostname,
            path_prefix: value.path_prefix,
            protocol: proto::Protocol::from(value.protocol).into(),
            failover: Some(value.failover.into()),
            auth: Some(value.auth.into()),
            sticky: value.sticky.map(Into::into),
            endpoints: value.endpoints.into_iter().map(Into::into).collect(),
        }
    }
}

impl From<proto::Failover> for core::Failover {
    fn from(value: proto::Failover) -> Self {
        Self {
            policy: proto::FailoverPolicy::try_from(value.policy)
                .unwrap_or_default()
                .into(),
            retry_on: value.retry_on,
            max_retries: value.max_retries,
        }
    }
}

impl From<core::Failover> for proto::Failover {
    fn from(value: core::Failover) -> Self {
        Self {
            policy: proto::FailoverPolicy::from(value.policy).into(),
            retry_on: value.retry_on,
            max_retries: value.max_retries,
        }
    }
}

impl From<proto::Auth> for core::Auth {
    fn from(value: proto::Auth) -> Self {
        Self {
            api_key_hashes: value.api_key_hashes,
        }
    }
}

impl From<core::Auth> for proto::Auth {
    fn from(value: core::Auth) -> Self {
        Self {
            api_key_hashes: value.api_key_hashes,
        }
    }
}

impl From<proto::Sticky> for core::Sticky {
    fn from(value: proto::Sticky) -> Self {
        Self {
            key: value.key,
            ttl_seconds: value.ttl_seconds,
            mode: proto::StickyMode::try_from(value.mode)
                .unwrap_or_default()
                .into(),
            on_unhealthy: proto::StickyOnUnhealthy::try_from(value.on_unhealthy)
                .unwrap_or_default()
                .into(),
            fallback_key: value.fallback_key,
        }
    }
}

impl From<core::Sticky> for proto::Sticky {
    fn from(value: core::Sticky) -> Self {
        Self {
            key: value.key,
            ttl_seconds: value.ttl_seconds,
            mode: proto::StickyMode::from(value.mode).into(),
            on_unhealthy: proto::StickyOnUnhealthy::from(value.on_unhealthy).into(),
            fallback_key: value.fallback_key,
        }
    }
}

impl From<proto::Endpoint> for core::Endpoint {
    fn from(value: proto::Endpoint) -> Self {
        Self {
            id: value.id,
            provider: value.provider,
            kind: proto::EndpointType::try_from(value.r#type)
                .unwrap_or_default()
                .into(),
            url: value.url,
            region: value.region,
            priority: value.priority,
            weight: value.weight,
            health: proto::Health::try_from(value.health)
                .unwrap_or_default()
                .into(),
            ready_replicas: value.ready_replicas,
            max_concurrency: value.max_concurrency,
            inject_headers: value.inject_headers.into_iter().collect::<BTreeMap<_, _>>(),
        }
    }
}

impl From<core::Endpoint> for proto::Endpoint {
    fn from(value: core::Endpoint) -> Self {
        Self {
            id: value.id,
            provider: value.provider,
            r#type: proto::EndpointType::from(value.kind).into(),
            url: value.url,
            region: value.region,
            priority: value.priority,
            weight: value.weight,
            health: proto::Health::from(value.health).into(),
            ready_replicas: value.ready_replicas,
            max_concurrency: value.max_concurrency,
            inject_headers: value.inject_headers.into_iter().collect(),
        }
    }
}

impl From<proto::Degraded> for core::Degraded {
    fn from(value: proto::Degraded) -> Self {
        Self {
            service: value.service,
            provider: value.provider,
            reason: proto::DegradedReason::try_from(value.reason)
                .unwrap_or_default()
                .into(),
            observed_concurrency: value.observed_concurrency,
        }
    }
}

impl From<core::Degraded> for proto::Degraded {
    fn from(value: core::Degraded) -> Self {
        Self {
            service: value.service,
            provider: value.provider,
            reason: proto::DegradedReason::from(value.reason).into(),
            observed_concurrency: value.observed_concurrency,
        }
    }
}

macro_rules! enum_bridge {
    ($proto:ty, $core:ty, { $($p:ident => $c:ident),+ $(,)? }) => {
        impl From<$proto> for $core {
            fn from(value: $proto) -> Self {
                match value {
                    $(<$proto>::$p => <$core>::$c,)+
                }
            }
        }

        impl From<$core> for $proto {
            fn from(value: $core) -> Self {
                match value {
                    $(<$core>::$c => <$proto>::$p,)+
                }
            }
        }
    };
}

enum_bridge!(proto::Protocol, core::Protocol, {
    Unspecified => Unspecified,
    Http => Http,
    Sse => Sse,
    Websocket => Websocket,
    Grpc => Grpc,
});

enum_bridge!(proto::FailoverPolicy, core::FailoverPolicy, {
    Unspecified => Unspecified,
    Priority => Priority,
    Weighted => Weighted,
    Latency => Latency,
});

enum_bridge!(proto::StickyMode, core::StickyMode, {
    Unspecified => Unspecified,
    Endpoint => Endpoint,
    Provider => Provider,
});

enum_bridge!(proto::StickyOnUnhealthy, core::StickyOnUnhealthy, {
    Unspecified => Unspecified,
    Rehome => Rehome,
    Fail => Fail,
});

enum_bridge!(proto::EndpointType, core::EndpointType, {
    Unspecified => Unspecified,
    Kubernetes => Kubernetes,
    Modal => Modal,
    Runpod => Runpod,
    Baseten => Baseten,
    Replicate => Replicate,
});

enum_bridge!(proto::Health, core::Health, {
    Unspecified => Unspecified,
    Ready => Ready,
    Degraded => Degraded,
    Draining => Draining,
    Down => Down,
});

enum_bridge!(proto::DegradedReason, core::DegradedReason, {
    Unspecified => Unspecified,
    QueueDepth => QueueDepth,
    TtftP95 => TtftP95,
    CapacityErrors => CapacityErrors,
});

#[cfg(test)]
mod tests {
    use super::*;

    fn sample() -> core::Snapshot {
        core::Snapshot {
            version: 3,
            at: Some(core::Timestamp {
                seconds: 10,
                nanos: 20,
            }),
            routes: vec![core::Route {
                id: "llama".into(),
                hostname: "api.example.com".into(),
                path_prefix: "/v1".into(),
                protocol: core::Protocol::Sse,
                failover: core::Failover {
                    policy: core::FailoverPolicy::Priority,
                    retry_on: vec!["capacity".into(), "connect".into()],
                    max_retries: 2,
                },
                auth: core::Auth {
                    api_key_hashes: vec!["h1".into()],
                },
                sticky: Some(core::Sticky {
                    key: "header:X-Session-Id".into(),
                    ttl_seconds: 1800,
                    mode: core::StickyMode::Provider,
                    on_unhealthy: core::StickyOnUnhealthy::Fail,
                    fallback_key: "client-ip".into(),
                }),
                endpoints: vec![core::Endpoint {
                    id: "modal-main".into(),
                    provider: "modal-main".into(),
                    kind: core::EndpointType::Modal,
                    url: "https://acme--multihull-llama.modal.run".into(),
                    region: "us".into(),
                    priority: 2,
                    weight: 5,
                    health: core::Health::Degraded,
                    ready_replicas: 1,
                    max_concurrency: 8,
                    inject_headers: BTreeMap::from([(
                        "Modal-Key".to_string(),
                        "from-env".to_string(),
                    )]),
                }],
            }],
        }
    }

    #[test]
    fn core_to_proto_and_back_is_lossless() {
        let original = sample();
        let proto: proto::Snapshot = original.clone().into();
        assert_eq!(proto.routes[0].protocol, proto::Protocol::Sse as i32);
        assert_eq!(
            proto.routes[0].endpoints[0].r#type,
            proto::EndpointType::Modal as i32
        );
        let back: core::Snapshot = proto.into();
        assert_eq!(back, original);
    }

    #[test]
    fn unknown_enum_values_fall_back_to_unspecified() {
        let endpoint = proto::Endpoint {
            r#type: 99,
            health: -1,
            ..Default::default()
        };
        let converted: core::Endpoint = endpoint.into();
        assert_eq!(converted.kind, core::EndpointType::Unspecified);
        assert_eq!(converted.health, core::Health::Unspecified);
    }

    #[test]
    fn empty_path_prefix_defaults_to_root() {
        let route: core::Route = proto::Route::default().into();
        assert_eq!(route.path_prefix, "/");
        assert_eq!(route.failover, core::Failover::default());
    }

    #[test]
    fn degraded_round_trips() {
        let degraded = core::Degraded {
            service: "llama".into(),
            provider: "gke".into(),
            reason: core::DegradedReason::TtftP95,
            observed_concurrency: 12,
        };
        let proto: proto::Degraded = degraded.clone().into();
        assert_eq!(proto.reason, proto::DegradedReason::TtftP95 as i32);
        let back: core::Degraded = proto.into();
        assert_eq!(back, degraded);
    }
}
