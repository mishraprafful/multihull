mod common;

use common::{endpoint, get, post, start_proxy_with, RunningProxy};
use hyper::StatusCode;
use multihull::core::snapshot::{
    Endpoint, Health, Route, Snapshot, Sticky, StickyMode, StickyOnUnhealthy,
};
use router_testkit::{MockUpstream, MockUpstreamConfig};

fn sticky_snapshot(endpoints: Vec<Endpoint>, sticky: Sticky) -> Snapshot {
    Snapshot {
        version: 1,
        at: None,
        routes: vec![Route {
            id: "llama".into(),
            sticky: Some(sticky),
            endpoints,
            ..Default::default()
        }],
    }
}

fn header_sticky(on_unhealthy: StickyOnUnhealthy) -> Sticky {
    Sticky {
        key: "header:X-Session-Id".into(),
        ttl_seconds: 60,
        mode: StickyMode::Endpoint,
        on_unhealthy,
        fallback_key: "body:$.session_id".into(),
    }
}

async fn two_upstreams() -> (MockUpstream, MockUpstream) {
    let a = MockUpstream::start(MockUpstreamConfig::default())
        .await
        .unwrap();
    let b = MockUpstream::start(MockUpstreamConfig::default())
        .await
        .unwrap();
    (a, b)
}

async fn start_sticky(
    a: &MockUpstream,
    b: &MockUpstream,
    on_unhealthy: StickyOnUnhealthy,
) -> RunningProxy {
    start_proxy_with(sticky_snapshot(
        vec![
            endpoint("a", "p1", a.url(), 1),
            endpoint("b", "p2", b.url(), 1),
        ],
        header_sticky(on_unhealthy),
    ))
    .await
}

#[tokio::test]
async fn sticky_key_always_lands_on_the_same_endpoint() {
    let (a, b) = two_upstreams().await;
    let proxy = start_sticky(&a, &b, StickyOnUnhealthy::Rehome).await;
    let first = get(&proxy, "/v1/x", &[("x-session-id", "s-1")]).await;
    assert_eq!(first.status, 200);
    assert!(first.header("x-hull-rehomed").is_none());
    assert!(first.header("x-hull-session").is_none());
    for _ in 0..10 {
        let reply = get(&proxy, "/v1/x", &[("x-session-id", "s-1")]).await;
        assert_eq!(reply.endpoint(), first.endpoint());
        assert!(reply.header("x-hull-rehomed").is_none());
    }
    assert_eq!(a.request_count() + b.request_count(), 11);
    assert!(a.request_count() == 0 || b.request_count() == 0);
    assert_eq!(proxy.state.runtime.active_sessions(), 1);

    let via_body = post(
        &proxy,
        "/v1/x",
        &[("content-type", "application/json")],
        br#"{"session_id":"s-1"}"#,
    )
    .await;
    assert_eq!(via_body.endpoint(), first.endpoint());
    assert_eq!(proxy.state.runtime.active_sessions(), 1);
}

#[tokio::test]
async fn missing_key_mints_a_session_and_cookie() {
    let (a, b) = two_upstreams().await;
    let sticky = Sticky {
        key: "cookie:hull_session".into(),
        ..header_sticky(StickyOnUnhealthy::Rehome)
    };
    let proxy = start_proxy_with(sticky_snapshot(
        vec![
            endpoint("a", "p1", a.url(), 1),
            endpoint("b", "p2", b.url(), 1),
        ],
        Sticky {
            fallback_key: String::new(),
            ..sticky
        },
    ))
    .await;
    let minted = get(&proxy, "/v1/x", &[]).await;
    assert_eq!(minted.status, 200);
    let session = minted.header("x-hull-session").unwrap().to_string();
    assert_eq!(session.len(), 32);
    let cookie = minted.header("set-cookie").unwrap().to_string();
    assert!(cookie.starts_with(&format!("hull_session={session}; Path=/; Max-Age=60")));
    assert_eq!(proxy.state.runtime.active_sessions(), 1);

    let cookie_header = format!("hull_session={session}");
    for _ in 0..5 {
        let reply = get(&proxy, "/v1/x", &[("cookie", cookie_header.as_str())]).await;
        assert_eq!(reply.endpoint(), minted.endpoint());
        assert!(reply.header("x-hull-session").is_none());
        assert!(reply.header("x-hull-rehomed").is_none());
    }
    assert_eq!(proxy.state.runtime.active_sessions(), 1);
}

#[tokio::test]
async fn owner_failure_rehomes_and_pins_the_new_owner() {
    let (a, b) = two_upstreams().await;
    let proxy = start_sticky(&a, &b, StickyOnUnhealthy::Rehome).await;
    let first = get(&proxy, "/v1/x", &[("x-session-id", "s-2")]).await;
    let owner = first.endpoint().to_string();
    let (owner_upstream, other) = if owner == "a" { (&a, "b") } else { (&b, "a") };
    owner_upstream.reconfigure(MockUpstreamConfig::default().with_status(StatusCode::BAD_GATEWAY));

    let rehomed = get(&proxy, "/v1/x", &[("x-session-id", "s-2")]).await;
    assert_eq!(rehomed.status, 200);
    assert_eq!(rehomed.endpoint(), other);
    assert_eq!(rehomed.header("x-hull-attempts"), Some("2"));
    assert_eq!(
        rehomed.header("x-hull-rehomed"),
        Some(format!("{owner}->{other}").as_str())
    );

    let pinned = get(&proxy, "/v1/x", &[("x-session-id", "s-2")]).await;
    assert_eq!(pinned.endpoint(), other);
    assert_eq!(pinned.header("x-hull-attempts"), Some("1"));
    assert!(pinned.header("x-hull-rehomed").is_none());

    owner_upstream.reconfigure(MockUpstreamConfig::default());
    let still_pinned = get(&proxy, "/v1/x", &[("x-session-id", "s-2")]).await;
    assert_eq!(still_pinned.endpoint(), other);
}

#[tokio::test]
async fn owner_removed_from_snapshot_rehomes_with_header() {
    let (a, b) = two_upstreams().await;
    let proxy = start_sticky(&a, &b, StickyOnUnhealthy::Rehome).await;
    let first = get(&proxy, "/v1/x", &[("x-session-id", "s-3")]).await;
    let owner = first.endpoint().to_string();
    let other = if owner == "a" { "b" } else { "a" };

    let mut endpoints = vec![
        endpoint("a", "p1", a.url(), 1),
        endpoint("b", "p2", b.url(), 1),
    ];
    endpoints.iter_mut().find(|e| e.id == owner).unwrap().health = Health::Down;
    let mut snapshot = sticky_snapshot(endpoints, header_sticky(StickyOnUnhealthy::Rehome));
    snapshot.version = 2;
    proxy.swap_snapshot(snapshot);

    let rehomed = get(&proxy, "/v1/x", &[("x-session-id", "s-3")]).await;
    assert_eq!(rehomed.status, 200);
    assert_eq!(rehomed.endpoint(), other);
    assert_eq!(rehomed.header("x-hull-attempts"), Some("1"));
    assert_eq!(
        rehomed.header("x-hull-rehomed"),
        Some(format!("{owner}->{other}").as_str())
    );
}

#[tokio::test]
async fn fail_mode_returns_session_lost() {
    let (a, b) = two_upstreams().await;
    let proxy = start_sticky(&a, &b, StickyOnUnhealthy::Fail).await;
    let first = get(&proxy, "/v1/x", &[("x-session-id", "s-4")]).await;
    assert_eq!(first.status, 200);
    let owner = first.endpoint().to_string();

    let mut endpoints = vec![
        endpoint("a", "p1", a.url(), 1),
        endpoint("b", "p2", b.url(), 1),
    ];
    endpoints.iter_mut().find(|e| e.id == owner).unwrap().health = Health::Down;
    let mut snapshot = sticky_snapshot(endpoints, header_sticky(StickyOnUnhealthy::Fail));
    snapshot.version = 2;
    proxy.swap_snapshot(snapshot);

    let lost = get(&proxy, "/v1/x", &[("x-session-id", "s-4")]).await;
    assert_eq!(lost.status, 503);
    assert_eq!(lost.json()["error"]["type"], "session_lost");
    assert!(lost.header("x-hull-endpoint").is_none());

    let fresh = get(&proxy, "/v1/x", &[("x-session-id", "brand-new")]).await;
    assert_eq!(fresh.status, 200);
}

#[tokio::test]
async fn draining_endpoint_keeps_sessions_but_takes_no_new_ones() {
    let (a, b) = two_upstreams().await;
    let proxy = start_sticky(&a, &b, StickyOnUnhealthy::Rehome).await;
    let mut keys = Vec::new();
    let mut owner_of = std::collections::HashMap::new();
    for i in 0..20 {
        let key = format!("drain-{i}");
        let reply = get(&proxy, "/v1/x", &[("x-session-id", key.as_str())]).await;
        owner_of.insert(key.clone(), reply.endpoint().to_string());
        keys.push(key);
    }
    let draining = owner_of.values().next().unwrap().clone();
    let mut endpoints = vec![
        endpoint("a", "p1", a.url(), 1),
        endpoint("b", "p2", b.url(), 1),
    ];
    endpoints
        .iter_mut()
        .find(|e| e.id == draining)
        .unwrap()
        .health = Health::Draining;
    let mut snapshot = sticky_snapshot(endpoints, header_sticky(StickyOnUnhealthy::Rehome));
    snapshot.version = 2;
    proxy.swap_snapshot(snapshot);

    for key in &keys {
        let reply = get(&proxy, "/v1/x", &[("x-session-id", key.as_str())]).await;
        assert_eq!(reply.endpoint(), owner_of[key]);
        assert!(reply.header("x-hull-rehomed").is_none());
    }
    for i in 0..30 {
        let key = format!("new-{i}");
        let reply = get(&proxy, "/v1/x", &[("x-session-id", key.as_str())]).await;
        assert_ne!(reply.endpoint(), draining);
        assert!(reply.header("x-hull-rehomed").is_none());
    }
}
