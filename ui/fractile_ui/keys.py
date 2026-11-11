"""Qt key events <-> Fractile accelerator strings ("Ctrl+Alt+G")."""
from __future__ import annotations

from PyQt6.QtCore import Qt

MOD_ORDER = ("Ctrl", "Alt", "Shift", "Super")

_NAMED = {
    Qt.Key.Key_Left: "Left", Qt.Key.Key_Right: "Right", Qt.Key.Key_Up: "Up", Qt.Key.Key_Down: "Down",
    Qt.Key.Key_Space: "space", Qt.Key.Key_Return: "Return", Qt.Key.Key_Enter: "KP_Enter", Qt.Key.Key_Escape: "Escape",
    Qt.Key.Key_Tab: "Tab", Qt.Key.Key_Backtab: "Tab", Qt.Key.Key_Home: "Home", Qt.Key.Key_End: "End",
    Qt.Key.Key_PageUp: "Prior", Qt.Key.Key_PageDown: "Next", Qt.Key.Key_Backspace: "BackSpace", Qt.Key.Key_Delete: "Delete",
    Qt.Key.Key_Insert: "Insert", Qt.Key.Key_Plus: "plus", Qt.Key.Key_Minus: "minus", Qt.Key.Key_Equal: "equal",
    Qt.Key.Key_QuoteLeft: "grave", Qt.Key.Key_Comma: "comma", Qt.Key.Key_Period: "period", Qt.Key.Key_Slash: "slash",
    Qt.Key.Key_Semicolon: "semicolon", Qt.Key.Key_BracketLeft: "bracketleft", Qt.Key.Key_BracketRight: "bracketright",
    Qt.Key.Key_Underscore: "minus",
}

MODIFIER_KEYS = {Qt.Key.Key_Control, Qt.Key.Key_Alt, Qt.Key.Key_Shift, Qt.Key.Key_Meta, Qt.Key.Key_Super_L, Qt.Key.Key_Super_R, Qt.Key.Key_AltGr, Qt.Key.Key_CapsLock, Qt.Key.Key_NumLock}


def key_name(key: int, keypad: bool = False) -> str | None:
    k = Qt.Key(key) if not isinstance(key, Qt.Key) else key
    if keypad:
        if Qt.Key.Key_0 <= k <= Qt.Key.Key_9:
            return f"KP_{k - Qt.Key.Key_0}"
        if k == Qt.Key.Key_Plus:
            return "KP_Add"
        if k == Qt.Key.Key_Minus:
            return "KP_Subtract"
    if k in _NAMED:
        return _NAMED[k]
    if Qt.Key.Key_F1 <= k <= Qt.Key.Key_F24:
        return f"F{k - Qt.Key.Key_F1 + 1}"
    if Qt.Key.Key_A <= k <= Qt.Key.Key_Z:
        return chr(ord("A") + (k - Qt.Key.Key_A))
    if Qt.Key.Key_0 <= k <= Qt.Key.Key_9:
        return str(k - Qt.Key.Key_0)
    return None


def mods_of(m: Qt.KeyboardModifier) -> list[str]:
    out = []
    if m & Qt.KeyboardModifier.ControlModifier:
        out.append("Ctrl")
    if m & Qt.KeyboardModifier.AltModifier:
        out.append("Alt")
    if m & Qt.KeyboardModifier.ShiftModifier:
        out.append("Shift")
    if m & Qt.KeyboardModifier.MetaModifier:
        out.append("Super")
    return out


def accel_from_event(ev) -> str | None:
    if ev.key() in MODIFIER_KEYS:
        return None
    keypad = bool(ev.modifiers() & Qt.KeyboardModifier.KeypadModifier)
    name = key_name(ev.key(), keypad)
    if name is None:
        return None
    mods = mods_of(ev.modifiers())
    # Shift+= arrives as '+' on many layouts: report the plain key with Shift.
    if name == "plus" and "Shift" in mods:
        pass
    return "+".join(mods + [name])


def normalize(accel: str) -> tuple[frozenset, str] | None:
    """Comparable form of an accelerator string in either Fractile
    ("Ctrl+Alt+G") or GTK ("<Control><Alt>g") syntax."""
    if not accel:
        return None
    s = accel.strip()
    mods: set[str] = set()
    if s.startswith("<"):
        while s.startswith("<"):
            end = s.find(">")
            if end < 0:
                return None
            mods.add(_canon_mod(s[1:end]))
            s = s[end + 1:]
        key = s
    else:
        parts = s.split("+")
        if s.endswith("++"):
            parts = parts[:-2] + ["plus"]
        key = parts[-1]
        for p in parts[:-1]:
            mods.add(_canon_mod(p))
    key = _canon_key(key)
    return frozenset(mods), key


def _canon_mod(m: str) -> str:
    m = m.lower()
    return {"control": "Ctrl", "ctrl": "Ctrl", "primary": "Ctrl", "alt": "Alt", "mod1": "Alt", "shift": "Shift", "super": "Super", "meta": "Super", "win": "Super", "logo": "Super", "mod4": "Super"}.get(m, m)


def _canon_key(k: str) -> str:
    low = k.lower()
    table = {"space": "space", "+": "plus", "plus": "plus", "minus": "minus", "-": "minus", "equal": "equal", "=": "equal", "return": "Return", "enter": "Return", "esc": "Escape", "escape": "Escape"}
    if low in table:
        return table[low]
    if len(k) == 1:
        return k.upper()
    return k[0].upper() + k[1:] if k[:1].islower() and low in ("left", "right", "up", "down", "tab", "home", "end") else k


def same(a: str, b: str) -> bool:
    na, nb = normalize(a), normalize(b)
    return na is not None and na == nb
