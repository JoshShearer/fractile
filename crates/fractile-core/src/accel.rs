//! Keyboard accelerator parsing ("Super+Alt+G", "<Ctrl><Shift>space").

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize, Default)]
pub struct Modifiers {
    pub shift: bool,
    pub ctrl: bool,
    pub alt: bool,
    pub super_: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct Accelerator {
    pub mods: Modifiers,
    /// Normalized key name (X keysym name, e.g. "g", "space", "Left", "KP_Add").
    pub key: String,
}

fn normalize_key(k: &str) -> Option<String> {
    let lower = k.to_ascii_lowercase();
    let named = match lower.as_str() {
        "space" => "space",
        "plus" | "+" => "plus",
        "minus" | "-" => "minus",
        "equal" | "=" => "equal",
        "return" | "enter" => "Return",
        "esc" | "escape" => "Escape",
        "tab" => "Tab",
        "left" => "Left",
        "right" => "Right",
        "up" => "Up",
        "down" => "Down",
        "home" => "Home",
        "end" => "End",
        "pageup" | "page_up" | "prior" => "Prior",
        "pagedown" | "page_down" | "next" => "Next",
        "backspace" => "BackSpace",
        "delete" | "del" => "Delete",
        "insert" | "ins" => "Insert",
        "kp_add" => "KP_Add",
        "kp_subtract" => "KP_Subtract",
        "kp_enter" => "KP_Enter",
        "grave" | "`" => "grave",
        "comma" | "," => "comma",
        "period" | "." => "period",
        "slash" | "/" => "slash",
        "semicolon" | ";" => "semicolon",
        "bracketleft" | "[" => "bracketleft",
        "bracketright" | "]" => "bracketright",
        _ => "",
    };
    if !named.is_empty() {
        return Some(named.to_string());
    }
    if lower.len() == 1 {
        let c = lower.chars().next().unwrap();
        if c.is_ascii_alphanumeric() {
            return Some(c.to_string());
        }
    }
    if let Some(n) = lower.strip_prefix('f') {
        if let Ok(v) = n.parse::<u32>() {
            if (1..=24).contains(&v) {
                return Some(format!("F{v}"));
            }
        }
    }
    if let Some(n) = lower.strip_prefix("kp_") {
        if n.len() == 1 && n.chars().all(|c| c.is_ascii_digit()) {
            return Some(format!("KP_{n}"));
        }
    }
    None
}

impl Accelerator {
    /// Parse "Ctrl+Alt+G", "super+shift+Left" or GTK-style "<Super><Alt>g".
    pub fn parse(s: &str) -> Result<Accelerator, String> {
        let s = s.trim();
        if s.is_empty() {
            return Err("empty accelerator".into());
        }
        let mut mods = Modifiers::default();
        let mut tokens: Vec<String> = Vec::new();
        if s.starts_with('<') {
            let mut rest = s;
            while let Some(stripped) = rest.strip_prefix('<') {
                let end = stripped.find('>').ok_or("unterminated <modifier>")?;
                tokens.push(stripped[..end].to_string());
                rest = &stripped[end + 1..];
            }
            if rest.is_empty() {
                return Err("missing key".into());
            }
            tokens.push(rest.to_string());
        } else {
            // split on '+' but allow a trailing literal '+' key ("Ctrl++")
            let mut parts: Vec<&str> = s.split('+').collect();
            if s.ends_with("++") {
                parts.truncate(parts.len() - 2);
                parts.push("plus");
            }
            tokens = parts.into_iter().map(|p| p.trim().to_string()).filter(|p| !p.is_empty()).collect();
        }
        let key_tok = tokens.pop().ok_or("missing key")?;
        for t in tokens {
            match t.to_ascii_lowercase().as_str() {
                "shift" => mods.shift = true,
                "ctrl" | "control" | "primary" => mods.ctrl = true,
                "alt" | "mod1" | "meta" => mods.alt = true,
                "super" | "win" | "logo" | "mod4" | "cmd" => mods.super_ = true,
                other => return Err(format!("unknown modifier '{other}'")),
            }
        }
        let key = normalize_key(&key_tok).ok_or_else(|| format!("unknown key '{key_tok}'"))?;
        Ok(Accelerator { mods, key })
    }

    /// Canonical display form, e.g. "Ctrl+Alt+G".
    pub fn to_display(&self) -> String {
        let mut s = String::new();
        if self.mods.super_ {
            s.push_str("Super+");
        }
        if self.mods.ctrl {
            s.push_str("Ctrl+");
        }
        if self.mods.alt {
            s.push_str("Alt+");
        }
        if self.mods.shift {
            s.push_str("Shift+");
        }
        if self.key.len() == 1 {
            s.push_str(&self.key.to_ascii_uppercase());
        } else {
            s.push_str(&self.key);
        }
        s
    }

    /// GTK/Mutter accelerator form, e.g. "<Control><Alt>g".
    pub fn to_gtk(&self) -> String {
        let mut s = String::new();
        if self.mods.super_ {
            s.push_str("<Super>");
        }
        if self.mods.ctrl {
            s.push_str("<Control>");
        }
        if self.mods.alt {
            s.push_str("<Alt>");
        }
        if self.mods.shift {
            s.push_str("<Shift>");
        }
        s.push_str(&self.key);
        s
    }

    pub fn has_modifier(&self) -> bool {
        self.mods.shift || self.mods.ctrl || self.mods.alt || self.mods.super_
    }

    pub fn same_as(&self, other: &Accelerator) -> bool {
        self.mods == other.mods && self.key.eq_ignore_ascii_case(&other.key)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_forms() {
        let a = Accelerator::parse("Ctrl+Alt+G").unwrap();
        let b = Accelerator::parse("<Control><Alt>g").unwrap();
        assert!(a.same_as(&b));
        assert_eq!(a.to_display(), "Ctrl+Alt+G");
        assert_eq!(a.to_gtk(), "<Control><Alt>g");
        let p = Accelerator::parse("Alt++").unwrap();
        assert_eq!(p.key, "plus");
        assert!(p.mods.alt);
        assert_eq!(Accelerator::parse("super+shift+left").unwrap().key, "Left");
        assert!(Accelerator::parse("Hyper+X").is_err());
        assert!(Accelerator::parse("").is_err());
        assert_eq!(Accelerator::parse("Super+F12").unwrap().key, "F12");
    }
}
