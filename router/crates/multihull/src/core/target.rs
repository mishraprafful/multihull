pub fn safe_request_target(path_and_query: &str) -> bool {
    if !path_and_query.starts_with('/') {
        return false;
    }
    let path = path_and_query.split(['?', '#']).next().unwrap_or_default();
    !path.split('/').any(dot_segment)
}

fn dot_segment(segment: &str) -> bool {
    percent_decode(segment)
        .split(['/', '\\'])
        .any(|piece| piece == "." || piece == "..")
}

fn percent_decode(raw: &str) -> String {
    let bytes = raw.as_bytes();
    let mut decoded = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        let escaped = match (bytes[index], bytes.get(index + 1), bytes.get(index + 2)) {
            (b'%', Some(&high), Some(&low)) => hex(high).zip(hex(low)),
            _ => None,
        };
        match escaped {
            Some((high, low)) => {
                decoded.push(high << 4 | low);
                index += 3;
            }
            None => {
                decoded.push(bytes[index]);
                index += 1;
            }
        }
    }
    String::from_utf8_lossy(&decoded).into_owned()
}

fn hex(digit: u8) -> Option<u8> {
    match digit {
        b'0'..=b'9' => Some(digit - b'0'),
        b'a'..=b'f' => Some(digit - b'a' + 10),
        b'A'..=b'F' => Some(digit - b'A' + 10),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn origin_form_paths_without_dot_segments_are_safe() {
        for target in [
            "/",
            "/predictions",
            "/v1/chat/completions?stream=true",
            "/v1/models/meta-llama%2FLlama-3.1",
            "/a/.hidden/b",
            "/a/..b/c...",
            "/search?q=../..",
        ] {
            assert!(safe_request_target(target), "{target}");
        }
    }

    #[test]
    fn targets_without_a_leading_slash_or_with_dot_segments_are_rejected() {
        for target in [
            "?x",
            "",
            "*",
            "v1/chat",
            "/../../../account",
            "/v1/./models",
            "/v1/..",
            "/%2e%2e/account",
            "/%2E%2e/account",
            "/.%2e/account",
            "/x/..%2f..%2faccount",
            "/x/..%5caccount",
            "/x/..\\account",
        ] {
            assert!(!safe_request_target(target), "{target}");
        }
    }

    #[test]
    fn malformed_escapes_stay_literal() {
        assert_eq!(percent_decode("%2"), "%2");
        assert_eq!(percent_decode("%zz%41"), "%zzA");
        assert!(safe_request_target("/%"));
    }
}
