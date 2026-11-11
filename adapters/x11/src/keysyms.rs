//! Minimal keysym table for the key names `fractile_core::accel` produces.

pub fn keysym_for(name: &str) -> Option<u32> {
    if name.len() == 1 {
        let c = name.chars().next()?;
        if c.is_ascii_lowercase() || c.is_ascii_digit() {
            return Some(c as u32);
        }
        if c.is_ascii_uppercase() {
            return Some(c.to_ascii_lowercase() as u32);
        }
    }
    if let Some(n) = name.strip_prefix('F') {
        if let Ok(v) = n.parse::<u32>() {
            if (1..=24).contains(&v) {
                return Some(0xffbe + v - 1);
            }
        }
    }
    if let Some(n) = name.strip_prefix("KP_") {
        if let Ok(v) = n.parse::<u32>() {
            if v <= 9 {
                return Some(0xffb0 + v);
            }
        }
    }
    Some(match name {
        "space" => 0x20,
        "plus" => 0x2b,
        "minus" => 0x2d,
        "equal" => 0x3d,
        "grave" => 0x60,
        "comma" => 0x2c,
        "period" => 0x2e,
        "slash" => 0x2f,
        "semicolon" => 0x3b,
        "bracketleft" => 0x5b,
        "bracketright" => 0x5d,
        "Return" => 0xff0d,
        "Escape" => 0xff1b,
        "Tab" => 0xff09,
        "BackSpace" => 0xff08,
        "Delete" => 0xffff,
        "Insert" => 0xff63,
        "Home" => 0xff50,
        "Left" => 0xff51,
        "Up" => 0xff52,
        "Right" => 0xff53,
        "Down" => 0xff54,
        "Prior" => 0xff55,
        "Next" => 0xff56,
        "End" => 0xff57,
        "KP_Add" => 0xffab,
        "KP_Subtract" => 0xffad,
        "KP_Enter" => 0xff8d,
        _ => return None,
    })
}

pub const XK_NUM_LOCK: u32 = 0xff7f;
