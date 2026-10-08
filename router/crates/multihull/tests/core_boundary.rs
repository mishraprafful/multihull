use std::path::Path;

const IO_CRATES: &[&str] = &[
    "axum",
    "http_body_util",
    "hyper",
    "hyper_rustls",
    "hyper_util",
    "metrics",
    "notify",
    "rustls",
    "tokio",
    "tokio_rustls",
    "tokio_stream",
    "tonic",
    "tracing",
];
const IO_STD_MODULES: &[&str] = &["env", "fs", "io", "net", "process", "thread"];
const CLOCKS: &[&str] = &["Instant", "SystemTime"];

fn identifiers(text: &str) -> impl Iterator<Item = &str> {
    text.split(|c: char| !(c.is_ascii_alphanumeric() || c == '_'))
        .filter(|word| !word.is_empty())
}

fn std_path_after(text: &str) -> &str {
    let end = text
        .find(|c: char| !(c.is_ascii_alphanumeric() || "_:{}, \n".contains(c)))
        .unwrap_or(text.len());
    &text[..end]
}

fn violations(source: &str) -> Vec<String> {
    let mut found = Vec::new();
    for word in identifiers(source) {
        if IO_CRATES.contains(&word) || CLOCKS.contains(&word) {
            found.push(word.to_string());
        }
    }
    for (index, _) in source.match_indices("std::") {
        let path = std_path_after(&source[index + "std::".len()..]);
        for word in identifiers(path) {
            if IO_STD_MODULES.contains(&word) {
                found.push(format!("std::{word}"));
            }
        }
    }
    for (index, _) in source.match_indices("crate::") {
        let rest = &source[index..];
        if !rest["crate::".len()..].starts_with("core::") {
            found.push(rest.lines().next().unwrap_or(rest).to_string());
        }
    }
    if source.contains("super::super") {
        found.push("super::super".to_string());
    }
    found
}

#[test]
fn core_module_does_no_io_and_depends_on_nothing_else_in_the_crate() {
    assert!(violations("use std::{collections::HashMap, time::Duration};").is_empty());
    assert!(violations("use crate::core::outcome::Outcome;").is_empty());
    assert_eq!(
        violations("use std::{fs, io::Read};"),
        ["std::fs", "std::io"]
    );
    assert_eq!(violations("let now = Instant::now();"), ["Instant"]);
    assert_eq!(violations("tokio::spawn(task);"), ["tokio"]);
    assert_eq!(
        violations("use crate::proxy::ProxyState;"),
        ["crate::proxy::ProxyState;"]
    );

    let core = Path::new(env!("CARGO_MANIFEST_DIR")).join("src/core");
    let mut files = 0;
    for entry in std::fs::read_dir(&core).unwrap() {
        let path = entry.unwrap().path();
        if path.extension().is_some_and(|ext| ext == "rs") {
            files += 1;
            let found = violations(&std::fs::read_to_string(&path).unwrap());
            assert!(found.is_empty(), "{}: {found:?}", path.display());
        }
    }
    assert!(files > 1, "no sources found in {}", core.display());
}
