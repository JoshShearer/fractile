//! Placement rules: ordered matchers with explainable results. Also shared by
//! layout entries (a layout entry is a matcher plus a placement).

use crate::geometry::{Grid, NormRect, Rect, Selection, SnapAction, Spacing};
use crate::output::Output;
use crate::window::{WindowInfo, WindowState};
use serde::{Deserialize, Serialize};

/// Where a placement lands.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, Default)]
#[serde(tag = "kind", content = "value", rename_all = "snake_case")]
pub enum OutputSel {
    /// Monitor currently containing the window.
    #[default]
    Current,
    Primary,
    Largest,
    /// Index in deterministic left-to-right order.
    Index(u32),
    /// Stable (EDID-derived) id; falls back to Current when absent.
    Stable(String),
}

impl OutputSel {
    pub fn resolve<'a>(&self, outputs: &'a [Output], window_frame: Option<&Rect>) -> Option<&'a Output> {
        let current = || window_frame.and_then(|f| crate::output::output_for_rect(outputs, f)).or_else(|| outputs.iter().find(|o| o.primary)).or(outputs.first());
        match self {
            OutputSel::Current => current(),
            OutputSel::Primary => outputs.iter().find(|o| o.primary).or(outputs.first()),
            OutputSel::Largest => outputs.iter().max_by_key(|o| o.rect.area()),
            OutputSel::Index(i) => {
                let ord = crate::output::ordered(outputs);
                ord.get(*i as usize).and_then(|o| outputs.iter().find(|p| p.stable_id == o.stable_id)).or_else(current)
            }
            OutputSel::Stable(id) => outputs.iter().find(|o| &o.stable_id == id).or_else(current),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Placement {
    Grid { grid: Grid, selection: Selection },
    Normalized { rect: NormRect },
    Snap { action: SnapAction },
}

impl Placement {
    pub fn to_rect(&self, work_area: Rect, spacing: &Spacing) -> Rect {
        match self {
            Placement::Grid { grid, selection } => crate::geometry::selection_rect(work_area, *grid, *selection, spacing),
            Placement::Normalized { rect } => crate::geometry::norm_rect_to_rect(work_area, *rect, spacing),
            Placement::Snap { action } => {
                let (g, s) = action.grid_selection();
                crate::geometry::selection_rect(work_area, g, s, spacing)
            }
        }
    }
    pub fn validate(&self) -> Result<(), String> {
        match self {
            Placement::Grid { grid, selection } => {
                if !grid.is_valid_effective() {
                    return Err(format!("grid {}x{} out of range", grid.cols, grid.rows));
                }
                if !selection.is_valid_for(*grid) {
                    return Err("selection outside grid".into());
                }
                Ok(())
            }
            Placement::Normalized { rect } => {
                if rect.is_sane() {
                    Ok(())
                } else {
                    Err("invalid normalized rectangle".into())
                }
            }
            Placement::Snap { .. } => Ok(()),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, Default)]
pub struct Matcher {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub desktop_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub app_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub wm_class: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub role: Option<String>,
    /// Opt-in: only honoured when `allow_title_regex` is enabled globally.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub title_regex: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub workspace: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub output: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub instance_ordinal: Option<u32>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct MatchCheck {
    pub field: String,
    pub expected: String,
    pub actual: String,
    pub ok: bool,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct MatchExplanation {
    pub matched: bool,
    pub checks: Vec<MatchCheck>,
    /// True when no deterministic field was specified (matcher too broad).
    pub empty: bool,
}

impl Matcher {
    pub fn is_empty(&self) -> bool {
        self.desktop_id.is_none() && self.app_id.is_none() && self.wm_class.is_none() && self.role.is_none() && self.title_regex.is_none()
    }

    pub fn validate(&self) -> Result<(), String> {
        if let Some(r) = &self.title_regex {
            if r.len() > 256 {
                return Err("title regex too long".into());
            }
            regex::RegexBuilder::new(r).size_limit(1 << 16).build().map_err(|e| format!("invalid title regex: {e}"))?;
        }
        Ok(())
    }

    /// Explain match against a window. `instance_ordinal` is the window's
    /// ordinal among same-app windows; `output_id` the monitor it is on.
    pub fn explain(&self, w: &WindowInfo, instance_ordinal: u32, allow_title_regex: bool) -> MatchExplanation {
        let mut checks = vec![];
        let ci = |field: &str, exp: &Option<String>, act: &Option<String>, checks: &mut Vec<MatchCheck>| {
            if let Some(e) = exp {
                let a = act.clone().unwrap_or_default();
                let ok = e.trim_end_matches(".desktop").eq_ignore_ascii_case(a.trim_end_matches(".desktop"));
                checks.push(MatchCheck { field: field.into(), expected: e.clone(), actual: a, ok });
            }
        };
        ci("desktop_id", &self.desktop_id, &w.desktop_file_id, &mut checks);
        ci("app_id", &self.app_id, &w.app_id, &mut checks);
        // WM_CLASS matches either class or instance
        if let Some(e) = &self.wm_class {
            let ok = w.wm_class.as_ref().map(|c| c.eq_ignore_ascii_case(e)).unwrap_or(false)
                || w.wm_class_instance.as_ref().map(|c| c.eq_ignore_ascii_case(e)).unwrap_or(false);
            checks.push(MatchCheck { field: "wm_class".into(), expected: e.clone(), actual: w.wm_class.clone().unwrap_or_default(), ok });
        }
        if let Some(e) = &self.role {
            let a = w.role.clone().unwrap_or_default();
            checks.push(MatchCheck { field: "role".into(), expected: e.clone(), actual: a.clone(), ok: &a == e });
        }
        if let Some(r) = &self.title_regex {
            let ok = allow_title_regex
                && regex::RegexBuilder::new(r).size_limit(1 << 16).build().map(|re| re.is_match(&w.title)).unwrap_or(false);
            checks.push(MatchCheck {
                field: "title_regex".into(),
                expected: r.clone(),
                actual: if allow_title_regex { w.title.clone() } else { "(title regex disabled)".into() },
                ok,
            });
        }
        if let Some(ws) = self.workspace {
            let a = w.workspace.map(|v| v.to_string()).unwrap_or_else(|| "any".into());
            checks.push(MatchCheck { field: "workspace".into(), expected: ws.to_string(), actual: a, ok: w.workspace == Some(ws) });
        }
        if let Some(o) = &self.output {
            let a = w.output_id.clone().unwrap_or_default();
            checks.push(MatchCheck { field: "output".into(), expected: o.clone(), actual: a.clone(), ok: &a == o });
        }
        if let Some(n) = self.instance_ordinal {
            checks.push(MatchCheck { field: "instance_ordinal".into(), expected: n.to_string(), actual: instance_ordinal.to_string(), ok: n == instance_ordinal });
        }
        let empty = self.is_empty();
        MatchExplanation { matched: !empty && checks.iter().all(|c| c.ok), checks, empty }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum RuleTrigger {
    /// Apply automatically when a matching window first appears.
    OnCreate,
    /// Apply only on explicit request ("apply rules now").
    #[default]
    Manual,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Rule {
    pub id: String,
    pub name: String,
    #[serde(default = "yes")]
    pub enabled: bool,
    #[serde(default)]
    pub trigger: RuleTrigger,
    pub matcher: Matcher,
    #[serde(default)]
    pub placement: Option<Placement>,
    #[serde(default)]
    pub output: OutputSel,
    #[serde(default)]
    pub workspace: Option<u32>,
    #[serde(default)]
    pub state: Option<WindowState>,
}

fn yes() -> bool {
    true
}

impl Rule {
    pub fn validate(&self) -> Result<(), String> {
        if self.name.trim().is_empty() {
            return Err("rule name required".into());
        }
        if self.matcher.is_empty() {
            return Err(format!("rule '{}' must match on at least one identity field", self.name));
        }
        self.matcher.validate()?;
        if let Some(p) = &self.placement {
            p.validate()?;
        }
        Ok(())
    }
}

/// First enabled rule (in priority order) matching the window.
pub fn first_match<'a>(rules: &'a [Rule], w: &WindowInfo, ordinal: u32, allow_title_regex: bool, trigger: Option<RuleTrigger>) -> Option<(&'a Rule, MatchExplanation)> {
    rules.iter().filter(|r| r.enabled && trigger.map(|t| t == r.trigger).unwrap_or(true)).find_map(|r| {
        let e = r.matcher.explain(w, ordinal, allow_title_regex);
        if e.matched {
            Some((r, e))
        } else {
            None
        }
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn w() -> WindowInfo {
        WindowInfo {
            id: "1".into(),
            title: "Inbox - Mail".into(),
            wm_class: Some("Thunderbird".into()),
            wm_class_instance: Some("Mail".into()),
            desktop_file_id: Some("thunderbird.desktop".into()),
            ..Default::default()
        }
    }

    #[test]
    fn explain_and_priority() {
        let r1 = Rule {
            id: "a".into(),
            name: "browser".into(),
            enabled: true,
            trigger: RuleTrigger::OnCreate,
            matcher: Matcher { wm_class: Some("firefox".into()), ..Default::default() },
            placement: None,
            output: OutputSel::Current,
            workspace: None,
            state: None,
        };
        let mut r2 = r1.clone();
        r2.id = "b".into();
        r2.name = "mail".into();
        r2.matcher = Matcher { desktop_id: Some("thunderbird".into()), ..Default::default() };
        let rules = vec![r1, r2];
        let (r, e) = first_match(&rules, &w(), 0, false, None).unwrap();
        assert_eq!(r.id, "b");
        assert!(e.matched);
    }

    #[test]
    fn title_regex_requires_opt_in() {
        let m = Matcher { title_regex: Some("^Inbox".into()), ..Default::default() };
        assert!(!m.explain(&w(), 0, false).matched);
        assert!(m.explain(&w(), 0, true).matched);
        assert!(Matcher { title_regex: Some("(".into()), ..Default::default() }.validate().is_err());
    }

    #[test]
    fn empty_matcher_never_matches() {
        let m = Matcher { workspace: Some(0), ..Default::default() };
        let mut win = w();
        win.workspace = Some(0);
        assert!(!m.explain(&win, 0, true).matched);
    }
}
