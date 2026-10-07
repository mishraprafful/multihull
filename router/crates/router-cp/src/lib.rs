pub mod convert;
pub mod file;
pub mod grpc;
pub mod http;
pub mod security;
pub mod source;

pub mod proto {
    tonic::include_proto!("multihull.discovery.v1");
}

pub use security::{BearerToken, SourceAuth, SourceSecurity, DEFAULT_TOKEN_ENV};
pub use source::{SnapshotError, SnapshotSource, Transport};
