pub mod convert;
pub mod file;
pub mod grpc;
pub mod http;
pub mod source;

pub mod proto {
    tonic::include_proto!("multihull.discovery.v1");
}

pub use source::{SnapshotError, SnapshotSource};
