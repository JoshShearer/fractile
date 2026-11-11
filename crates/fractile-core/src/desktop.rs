//! Freedesktop `.desktop` entry parsing and *safe* Exec → argv conversion.
//! No shell is ever involved: the Exec value is tokenized per the Desktop
//! Entry Specification quoting rules and field codes are dropped or
//! substituted with explicit, user-approved arguments only.

use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, Default)]
pub struct DesktopEntry {
    /// Desktop file id, e.g. "org.gnome.Terminal.desktop" (subdirs → '-').
    pub id: String,
    pub path: PathBuf,
    pub name: String,
    pub exec: Option<String>,
    pub try_exec: Option<String>,
    pub icon: Option<String>,
    pub startup_wm_class: Option<String>,
    pub dbus_activatable: bool,
    pub no_display: bool,
    pub hidden: bool,
    pub terminal: bool,
    pub kind: String,
}

/// Parse the `[Desktop Entry]` group of a desktop file.
pub fn parse_desktop_entry(id: &str, path: &Path, content: &str) -> Option<DesktopEntry> {
    let mut in_group = false;
    let mut kv: BTreeMap<String, String> = BTreeMap::new();
    for line in content.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        if line.starts_with('[') {
            in_group = line == "[Desktop Entry]";
            continue;
        }
        if !in_group {
            continue;
        }
        if let Some((k, v)) = line.split_once('=') {
            let k = k.trim();
            // skip localized keys
            if k.contains('[') {
                continue;
            }
            kv.entry(k.to_string()).or_insert_with(|| unescape_value(v.trim()));
        }
    }
    let kind = kv.get("Type").cloned().unwrap_or_default();
    if kind.is_empty() {
        return None;
    }
    let b = |k: &str| kv.get(k).map(|v| v.eq_ignore_ascii_case("true")).unwrap_or(false);
    Some(DesktopEntry {
        id: id.to_string(),
        path: path.to_path_buf(),
        name: kv.get("Name").cloned().unwrap_or_else(|| id.to_string()),
        exec: kv.get("Exec").cloned(),
        try_exec: kv.get("TryExec").cloned(),
        icon: kv.get("Icon").cloned(),
        startup_wm_class: kv.get("StartupWMClass").cloned(),
        dbus_activatable: b("DBusActivatable"),
        no_display: b("NoDisplay"),
        hidden: b("Hidden"),
        terminal: b("Terminal"),
        kind,
    })
}

/// Desktop Entry "string" value escapes: \s \n \t \r \\ (and \; in lists).
fn unescape_value(v: &str) -> String {
    let mut out = String::with_capacity(v.len());
    let mut it = v.chars();
    while let Some(c) = it.next() {
        if c == '\\' {
            match it.next() {
                Some('s') => out.push(' '),
                Some('n') => out.push('\n'),
                Some('t') => out.push('\t'),
                Some('r') => out.push('\r'),
                Some('\\') => out.push('\\'),
                Some(o) => {
                    out.push('\\');
                    out.push(o);
                }
                None => out.push('\\'),
            }
        } else {
            out.push(c);
        }
    }
    out
}

/// Tokenize an Exec value into argv per the Desktop Entry spec, removing
/// field codes (%f %F %u %U %d %D %n %N %i %c %k %v %m) and substituting
/// nothing user-uncontrolled. `%%` becomes `%`. Returns Err on malformed
/// quoting or when the result is empty.
pub fn exec_to_argv(exec: &str, entry_name: &str, entry_icon: Option<&str>) -> Result<Vec<String>, String> {
    let mut args: Vec<String> = Vec::new();
    let mut cur = String::new();
    let mut in_quotes = false;
    let mut have_token = false;
    let chars: Vec<char> = exec.chars().collect();
    let mut i = 0;
    while i < chars.len() {
        let c = chars[i];
        if in_quotes {
            match c {
                '"' => in_quotes = false,
                '\\' => {
                    i += 1;
                    let n = *chars.get(i).ok_or("dangling escape in quoted Exec")?;
                    if matches!(n, '"' | '`' | '$' | '\\') {
                        cur.push(n);
                    } else {
                        return Err(format!("invalid escape \\{n} in Exec"));
                    }
                }
                _ => cur.push(c),
            }
            have_token = true;
        } else {
            match c {
                ' ' | '\t' => {
                    if have_token {
                        args.push(std::mem::take(&mut cur));
                        have_token = false;
                    }
                }
                '"' => {
                    in_quotes = true;
                    have_token = true;
                }
                _ => {
                    cur.push(c);
                    have_token = true;
                }
            }
        }
        i += 1;
    }
    if in_quotes {
        return Err("unterminated quote in Exec".into());
    }
    if have_token {
        args.push(cur);
    }
    // field codes
    let mut out = Vec::new();
    for a in args {
        match a.as_str() {
            "%f" | "%F" | "%u" | "%U" | "%d" | "%D" | "%n" | "%N" | "%v" | "%m" => continue,
            "%i" => {
                if let Some(icon) = entry_icon {
                    out.push("--icon".into());
                    out.push(icon.to_string());
                }
                continue;
            }
            "%c" => {
                out.push(entry_name.to_string());
                continue;
            }
            "%k" => continue,
            _ => {}
        }
        // embedded codes within an argument: only %% is legal
        let mut s = String::new();
        let cs: Vec<char> = a.chars().collect();
        let mut j = 0;
        while j < cs.len() {
            if cs[j] == '%' {
                match cs.get(j + 1) {
                    Some('%') => {
                        s.push('%');
                        j += 2;
                        continue;
                    }
                    Some(_) => {
                        // drop unknown embedded field code
                        j += 2;
                        continue;
                    }
                    None => {
                        j += 1;
                        continue;
                    }
                }
            }
            s.push(cs[j]);
            j += 1;
        }
        out.push(s);
    }
    if out.is_empty() || out[0].is_empty() {
        return Err("Exec resolves to an empty command".into());
    }
    Ok(out)
}

/// Desktop file id from a path relative to an applications dir
/// ("kde4/foo.desktop" → "kde4-foo.desktop").
pub fn desktop_id_from_relative(rel: &Path) -> String {
    rel.to_string_lossy().replace('/', "-")
}

/// Extract the desktop id embedded in a systemd unit name created by a
/// desktop launcher, per systemd's desktop-environments convention:
/// `app[-<launcher>]-<ApplicationID>[@<RANDOM>].service` or
/// `app[-<launcher>]-<ApplicationID>-<RANDOM>.scope`.
/// Returns candidate ids (without ".desktop"), most specific first.
pub fn app_ids_from_systemd_unit(unit: &str) -> Vec<String> {
    let unit = unescape_systemd(unit);
    let Some(rest) = unit.strip_prefix("app-") else { return vec![] };
    let (body, is_scope) = if let Some(b) = rest.strip_suffix(".scope") {
        (b.to_string(), true)
    } else if let Some(b) = rest.strip_suffix(".service") {
        (b.split('@').next().unwrap_or(b).to_string(), false)
    } else {
        return vec![];
    };
    let mut body = body;
    if is_scope {
        // drop trailing -RANDOM (often a pid or hex)
        if let Some(pos) = body.rfind('-') {
            let tail = &body[pos + 1..];
            if !tail.is_empty() && tail.chars().all(|c| c.is_ascii_alphanumeric()) && tail.chars().any(|c| c.is_ascii_digit()) {
                body.truncate(pos);
            }
        }
    }
    let mut cands = vec![];
    // Known launcher prefixes; the app id itself may contain '-'.
    for launcher in ["fractile-", "gnome-", "kde-", "cosmic-", "flatpak-", "snap-", "dbus-:1.", "xfce-"] {
        if let Some(id) = body.strip_prefix(launcher) {
            cands.push(id.to_string());
        }
    }
    if let Some(rest) = body.strip_prefix("dbus-") {
        // dbus-:1.2-org.foo.Bar
        if let Some(pos) = rest.find('-') {
            cands.push(rest[pos + 1..].to_string());
        }
    }
    cands.push(body.clone());
    cands.retain(|c| !c.is_empty());
    cands.dedup();
    cands
}

fn unescape_systemd(s: &str) -> String {
    let b = s.as_bytes();
    let mut out: Vec<u8> = Vec::with_capacity(b.len());
    let mut i = 0;
    while i < b.len() {
        if b[i] == b'\\' && i + 3 < b.len() && b[i + 1] == b'x' && b[i + 2].is_ascii_hexdigit() && b[i + 3].is_ascii_hexdigit() {
            let hex = [b[i + 2], b[i + 3]];
            if let Ok(v) = u8::from_str_radix(std::str::from_utf8(&hex).unwrap_or("zz"), 16) {
                out.push(v);
                i += 4;
                continue;
            }
        }
        out.push(b[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn exec_tokenizing_and_field_codes() {
        assert_eq!(exec_to_argv("firefox %u", "Firefox", None).unwrap(), vec!["firefox"]);
        assert_eq!(
            exec_to_argv(r#""/opt/My App/run" --flag "a \"b\"" %F"#, "x", None).unwrap(),
            vec!["/opt/My App/run", "--flag", "a \"b\""]
        );
        assert_eq!(exec_to_argv("app --pct=100%%", "x", None).unwrap(), vec!["app", "--pct=100%"]);
        assert_eq!(exec_to_argv("app %i", "x", Some("ico")).unwrap(), vec!["app", "--icon", "ico"]);
        assert!(exec_to_argv("\"unterminated", "x", None).is_err());
        assert!(exec_to_argv("%U", "x", None).is_err());
    }

    #[test]
    fn shell_metacharacters_are_literal() {
        // No shell: these become literal argv entries, never interpreted.
        let v = exec_to_argv("sh -c ;rm$(x)|y", "x", None).unwrap();
        assert_eq!(v, vec!["sh", "-c", ";rm$(x)|y"]);
    }

    #[test]
    fn parse_entry() {
        let c = "[Desktop Entry]\nType=Application\nName=Term\nName[de]=Terminal\nExec=xterm -e\\sbash\nStartupWMClass=XTerm\n[Desktop Action new]\nExec=other\n";
        let e = parse_desktop_entry("xterm.desktop", Path::new("/x"), c).unwrap();
        assert_eq!(e.name, "Term");
        assert_eq!(e.exec.as_deref(), Some("xterm -e bash"));
        assert_eq!(e.startup_wm_class.as_deref(), Some("XTerm"));
    }

    #[test]
    fn systemd_units() {
        let v = app_ids_from_systemd_unit("app-gnome-org.gnome.Terminal-12345.scope");
        assert_eq!(v[0], "org.gnome.Terminal");
        let v = app_ids_from_systemd_unit("app-gnome-brave\\x2dbrowser-4242.scope");
        assert_eq!(v[0], "brave-browser");
        let v = app_ids_from_systemd_unit("app-fractile-brave\\x2dbrowser-123045.scope");
        assert_eq!(v[0], "brave-browser");
        let v = app_ids_from_systemd_unit("app-flatpak-org.mozilla.firefox-9999.scope");
        assert_eq!(v[0], "org.mozilla.firefox");
        assert!(app_ids_from_systemd_unit("session-2.scope").is_empty());
    }
}
