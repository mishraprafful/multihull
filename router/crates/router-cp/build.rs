use std::path::PathBuf;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let manifest_dir = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR")?);
    let proto_dir = manifest_dir
        .join("../../../proto")
        .canonicalize()
        .unwrap_or_else(|_| manifest_dir.join("../../../proto"));
    let proto_file = proto_dir.join("discovery.proto");
    println!("cargo:rerun-if-changed={}", proto_file.display());
    println!("cargo:rerun-if-changed=build.rs");

    let protoc = protoc_bin_vendored::protoc_bin_path()?;
    let well_known = protoc_bin_vendored::include_path()?;
    std::env::set_var("PROTOC", &protoc);

    tonic_prost_build::configure()
        .build_server(false)
        .build_client(true)
        .compile_protos(
            &[proto_file.as_path()],
            &[proto_dir.as_path(), well_known.as_path()],
        )?;
    Ok(())
}
