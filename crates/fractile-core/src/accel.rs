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

    /// Qt key code (key | modifier flags), as KDE's KGlobalAccel expects.
    pub fn to_qt_key(&self) -> Option<i32> {
        const KEYPAD: i32 = 0x2000_0000;
        let k = self.key.as_str();
        let base: i32 = match k {
            "space" => 0x20,
            "plus" => 0x2b,
            "minus" => 0x2d,
            "equal" => 0x3d,
            "Return" => 0x0100_0004,
            "Escape" => 0x0100_0000,
            "Tab" => 0x0100_0001,
            "BackSpace" => 0x0100_0003,
            "Insert" => 0x0100_0006,
            "Delete" => 0x0100_0007,
            "Home" => 0x0100_0010,
            "End" => 0x0100_0011,
            "Left" => 0x0100_0012,
            "Up" => 0x0100_0013,
            "Right" => 0x0100_0014,
            "Down" => 0x0100_0015,
            "Prior" => 0x0100_0016,
            "Next" => 0x0100_0017,
            "KP_Add" => 0x2b | KEYPAD,
            "KP_Subtract" => 0x2d | KEYPAD,
            "KP_Enter" => 0x0100_0005 | KEYPAD,
            "grave" => 0x60,
            "comma" => 0x2c,
            "period" => 0x2e,
            "slash" => 0x2f,
            "semicolon" => 0x3b,
            "bracketleft" => 0x5b,
            "bracketright" => 0x5d,
            _ => {
                if let Some(n) = k.strip_prefix("KP_") {
                    (n.as_bytes()[0] as i32) | KEYPAD
                } else if let Some(n) = k.strip_prefix('F').and_then(|n| n.parse::<i32>().ok()) {
                    0x0100_0030 + n - 1
                } else if k.len() == 1 {
                    k.to_ascii_uppercase().as_bytes()[0] as i32
                } else {
                    return None;
                }
            }
        };
        let mut v = base;
        if self.mods.shift {
            v |= 0x0200_0000;
        }
        if self.mods.ctrl {
            v |= 0x0400_0000;
        }
        if self.mods.alt {
            v |= 0x0800_0000;
        }
        if self.mods.super_ {
            v |= 0x1000_0000;
        }
        Some(v)
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
    fn qt_key_codes() {
        // Values from Qt's Qt::Key / Qt::KeyboardModifier enums.
        assert_eq!(Accelerator::parse("Ctrl+Alt+1").unwrap().to_qt_key(), Some(0x0400_0000 | 0x0800_0000 | 0x31));
        assert_eq!(Accelerator::parse("Super+Left").unwrap().to_qt_key(), Some(0x1000_0000 | 0x0100_0012));
        assert_eq!(Accelerator::parse("Ctrl+Alt+G").unwrap().to_qt_key(), Some(0x0c00_0047));
        assert_eq!(Accelerator::parse("Shift+F5").unwrap().to_qt_key(), Some(0x0200_0000 | 0x0100_0034));
        assert_eq!(Accelerator::parse("Alt+KP_Add").unwrap().to_qt_key(), Some(0x0800_0000 | 0x2000_0000 | 0x2b));
    }

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
