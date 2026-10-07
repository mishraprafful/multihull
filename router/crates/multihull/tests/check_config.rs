use std::path::Path;
use std::process::{Command, Output};

fn check_config(path: &Path) -> Output {
    Command::new(env!("CARGO_BIN_EXE_multihull"))
        .arg("--config")
        .arg(path)
        .arg("--check-config")
        .output()
        .expect("run multihull")
}

#[test]
fn valid_config_exits_zero_without_starting_the_router() {
    let example = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../router.example.toml");
    let output = check_config(&example);
    assert!(output.status.success(), "{output:?}");
    assert!(String::from_utf8_lossy(&output.stdout).starts_with("config ok"));
}

#[test]
fn invalid_config_exits_non_zero_and_names_the_problem() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("router.toml");
    std::fs::write(
        &path,
        "[snapshot]\nsource = \"x\"\n\n[admission]\nbound = 1e+06\n",
    )
    .unwrap();
    let output = check_config(&path);
    assert!(!output.status.success(), "{output:?}");
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(stderr.contains("bound"), "{stderr}");
}
