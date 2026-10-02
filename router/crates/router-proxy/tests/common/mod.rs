#![allow(dead_code)]

use arc_swap::ArcSwap;
use bytes::Bytes;
use http_body_util::{BodyExt, Full};
use hyper::Request;
use hyper_util::client::legacy::Client;
use hyper_util::rt::TokioExecutor;
use router_core::snapshot::{Endpoint, Health, Route, Snapshot};
use router_proxy::{ProxyConfig, ProxyState};
use std::net::SocketAddr;
use std::sync::Arc;
use tokio::net::TcpListener;
use tokio::sync::oneshot;

pub struct RunningProxy {
    pub addr: SocketAddr,
    pub state: Arc<ProxyState>,
    _stop: oneshot::Sender<()>,
}

impl RunningProxy {
    pub fn url(&self, path: &str) -> String {
        format!("http://{}{}", self.addr, path)
    }

    pub fn swap_snapshot(&self, snapshot: Snapshot) {
        self.state.snapshot.store(Arc::new(snapshot));
        self.state.refresh();
    }
}

pub async fn start_proxy(endpoints: Vec<Endpoint>) -> RunningProxy {
    start_proxy_with(single_route(endpoints)).await
}

pub fn single_route(endpoints: Vec<Endpoint>) -> Snapshot {
    Snapshot {
        version: 1,
        at: None,
        routes: vec![Route {
            id: "llama".into(),
            endpoints,
            ..Default::default()
        }],
    }
}

pub async fn start_proxy_with(snapshot: Snapshot) -> RunningProxy {
    start_proxy_configured(snapshot, ProxyConfig::default()).await
}

pub async fn start_proxy_configured(snapshot: Snapshot, config: ProxyConfig) -> RunningProxy {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let config = ProxyConfig {
        listen: addr,
        ..config
    };
    let state = ProxyState::new(config, Arc::new(ArcSwap::from_pointee(snapshot)));
    let (stop, stopped) = oneshot::channel();
    let serving = state.clone();
    tokio::spawn(async move {
        router_proxy::serve(serving, listener, async {
            let _ = stopped.await;
        })
        .await
        .unwrap();
    });
    RunningProxy {
        addr,
        state,
        _stop: stop,
    }
}

pub fn endpoint(id: &str, provider: &str, url: String, priority: u32) -> Endpoint {
    Endpoint {
        id: id.into(),
        provider: provider.into(),
        url,
        priority,
        health: Health::Ready,
        max_concurrency: 8,
        ..Default::default()
    }
}

pub async fn closed_port_url() -> String {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    drop(listener);
    format!("http://{addr}")
}

pub fn client() -> Client<hyper_util::client::legacy::connect::HttpConnector, Full<Bytes>> {
    Client::builder(TokioExecutor::new()).build_http()
}

pub struct Reply {
    pub status: u16,
    pub headers: http::HeaderMap,
    pub body: Bytes,
}

impl Reply {
    pub fn header(&self, name: &str) -> Option<&str> {
        self.headers.get(name).and_then(|v| v.to_str().ok())
    }

    pub fn endpoint(&self) -> &str {
        self.header("x-hull-endpoint").unwrap_or("")
    }

    pub fn json(&self) -> serde_json::Value {
        serde_json::from_slice(&self.body).unwrap()
    }
}

pub async fn send(request: Request<Full<Bytes>>) -> Reply {
    let response = client().request(request).await.unwrap();
    let status = response.status().as_u16();
    let headers = response.headers().clone();
    let body = response.into_body().collect().await.unwrap().to_bytes();
    Reply {
        status,
        headers,
        body,
    }
}

pub async fn get(proxy: &RunningProxy, path: &str, headers: &[(&str, &str)]) -> Reply {
    let mut builder = Request::get(proxy.url(path));
    for (name, value) in headers {
        builder = builder.header(*name, *value);
    }
    send(builder.body(Full::new(Bytes::new())).unwrap()).await
}

pub async fn post(
    proxy: &RunningProxy,
    path: &str,
    headers: &[(&str, &str)],
    body: &'static [u8],
) -> Reply {
    let mut builder = Request::post(proxy.url(path));
    for (name, value) in headers {
        builder = builder.header(*name, *value);
    }
    send(builder.body(Full::new(Bytes::from_static(body))).unwrap()).await
}
