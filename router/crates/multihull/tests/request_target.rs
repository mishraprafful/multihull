mod common;

use common::{endpoint, start_proxy, RunningProxy};
use router_testkit::{MockUpstream, MockUpstreamConfig};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;

async fn raw_status(proxy: &RunningProxy, method: &str, target: &str) -> u16 {
    let mut stream = TcpStream::connect(proxy.addr).await.unwrap();
    let request = format!(
        "{method} {target} HTTP/1.1\r\nHost: echo.local\r\nContent-Type: application/json\r\n\
         Content-Length: 2\r\nConnection: close\r\n\r\n{{}}"
    );
    stream.write_all(request.as_bytes()).await.unwrap();
    let mut reply = Vec::new();
    stream.read_to_end(&mut reply).await.unwrap();
    let text = String::from_utf8_lossy(&reply);
    text.split_whitespace().nth(1).unwrap().parse().unwrap()
}

#[tokio::test]
async fn targets_that_escape_the_endpoint_path_never_reach_the_upstream() {
    let upstream = MockUpstream::start(MockUpstreamConfig::default())
        .await
        .unwrap();
    let deployment = format!("{}/v1/deployments/acme/echo", upstream.url());
    let proxy = start_proxy(vec![endpoint("a", "pa", deployment, 1)]).await;

    for (method, target) in [
        ("PATCH", "http://echo.local?x"),
        ("DELETE", "http://echo.local?x"),
        ("GET", "/../../../account"),
        ("GET", "/%2e%2e/%2e%2e/%2e%2e/account"),
        ("GET", "/x/..%2f..%2f..%2faccount"),
        ("GET", "/..;/..;/..;/account"),
    ] {
        assert_eq!(
            raw_status(&proxy, method, target).await,
            400,
            "{method} {target}"
        );
    }
    assert_eq!(upstream.request_count(), 0);

    assert_eq!(raw_status(&proxy, "GET", "/v1/models").await, 200);
    assert_eq!(
        raw_status(&proxy, "GET", "http://echo.local/v1/models").await,
        200
    );
    assert_eq!(upstream.request_count(), 2);
}
