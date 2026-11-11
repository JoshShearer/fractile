//! Named layouts ("recipes"): ordered entries that position existing windows,
//! optionally launch missing apps (permission-gated), with per-topology
//! variants for laptop / dock / home setups.

use crate::output::Output;
use crate::rules::{Matcher, OutputSel, Placement};
use crate::snapshot::{is_safe_desktop_id, LaunchPolicy};
use crate::window::WindowState;
use serde::{Deserialize, Serialize};

pub const MAX_LAYOUT_ENTRIES: usize = 200;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum ApplyMode {
    /// Only move windows that already exist.
    #[default]
    PositionExisting,
    /// Launch approved missing apps, then position.
    LaunchMissing,
    /// Ensure exactly N instances per entry (launch missing; never closes extras).
    ExactN,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct LaunchSpec {
    pub desktop_id: String,
    #[serde(default)]
    pub policy: LaunchPolicy,
    /// Delay after dependencies are satisfied.
    #[serde(default)]
    pub delay_ms: u64,
    /// Entry ids that must have a window before this one launches.
    #[serde(default)]
    pub depends_on: Vec<String>,
    /// Application group label (e.g. "IDE stack").
    #[serde(default)]
    pub group: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct LayoutEntry {
    pub id: String,
    #[serde(default)]
    pub label: String,
    pub matcher: Matcher,
    pub placement: Placement,
    #[serde(default)]
    pub output: OutputSel,
    #[serde(default)]
    pub workspace: Option<u32>,
    #[serde(default)]
    pub state: Option<WindowState>,
    #[serde(default)]
    pub launch: Option<LaunchSpec>,
    /// Number of instances for ExactN (default 1).
    #[serde(default = "one")]
    pub instances: u32,
    /// Excluded entries are kept but skipped on apply.
    #[serde(default)]
    pub excluded: bool,
}

fn one() -> u32 {
    1
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, Default)]
pub struct LayoutVariant {
    /// Use when the monitor topology signature matches exactly.
    #[serde(default)]
    pub when_topology: Option<String>,
    /// Use when this many monitors are connected.
    #[serde(default)]
    pub when_output_count: Option<u32>,
    pub entries: Vec<LayoutEntry>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Layout {
    pub schema_version: u32,
    pub name: String,
    #[serde(default)]
    pub description: String,
    #[serde(default)]
    pub favorite: bool,
    pub variants: Vec<LayoutVariant>,
    #[serde(default)]
    pub created_at: String,
    #[serde(default)]
    pub updated_at: String,
}

impl Layout {
    /// Pick the variant for the present monitors: exact topology, then
    /// monitor count, then the first unconditional variant, then the first.
    pub fn select_variant(&self, outputs: &[Output]) -> Option<&LayoutVariant> {
        let sig = crate::output::topology_signature(outputs);
        self.variants
            .iter()
            .find(|v| v.when_topology.as_deref() == Some(sig.as_str()))
            .or_else(|| self.variants.iter().find(|v| v.when_output_count == Some(outputs.len() as u32)))
            .or_else(|| self.variants.iter().find(|v| v.when_topology.is_none() && v.when_output_count.is_none()))
            .or_else(|| self.variants.first())
    }

    pub fn validate(&self) -> Result<(), String> {
        if self.name.trim().is_empty() || self.name.len() > 128 {
            return Err("layout name must be 1..128 characters".into());
        }
        if self.variants.is_empty() {
            return Err("layout needs at least one variant".into());
        }
        for v in &self.variants {
            if v.entries.len() > MAX_LAYOUT_ENTRIES {
                return Err("too many layout entries".into());
            }
            let ids: Vec<&str> = v.entries.iter().map(|e| e.id.as_str()).collect();
            for e in &v.entries {
                if e.matcher.is_empty() {
                    return Err(format!("entry '{}' needs an identity matcher", e.label));
                }
                e.matcher.validate()?;
                e.placement.validate()?;
                if e.instances == 0 || e.instances > 20 {
                    return Err(format!("entry '{}' instances must be 1..20", e.label));
                }
                if let Some(l) = &e.launch {
                    if !is_safe_desktop_id(&l.desktop_id) {
                        return Err(format!("unsafe desktop id '{}'", l.desktop_id));
                    }
                    if l.delay_ms > 120_000 {
                        return Err("launch delay too long".into());
                    }
                    for d in &l.depends_on {
                        if !ids.contains(&d.as_str()) {
                            return Err(format!("entry '{}' depends on unknown entry '{d}'", e.label));
                        }
                        if d == &e.id {
                            return Err("entry cannot depend on itself".into());
                        }
                    }
                }
            }
            // dependency cycle detection
            launch_order(&v.entries)?;
        }
        Ok(())
    }

    /// Imports demote launch policies to Ask unless provenance is trusted.
    pub fn sanitize_for_import(&mut self, trusted: bool) {
        if trusted {
            return;
        }
        for v in &mut self.variants {
            for e in &mut v.entries {
                if let Some(l) = &mut e.launch {
                    if l.policy != LaunchPolicy::Never {
                        l.policy = LaunchPolicy::Ask;
                    }
                }
            }
        }
    }
}

/// Topological launch order honouring `depends_on`; Err on cycles.
pub fn launch_order(entries: &[LayoutEntry]) -> Result<Vec<String>, String> {
    let mut order = vec![];
    let mut state: std::collections::HashMap<&str, u8> = Default::default();
    fn visit<'a>(id: &'a str, entries: &'a [LayoutEntry], state: &mut std::collections::HashMap<&'a str, u8>, order: &mut Vec<String>) -> Result<(), String> {
        match state.get(id) {
            Some(2) => return Ok(()),
            Some(1) => return Err(format!("dependency cycle at '{id}'")),
            _ => {}
        }
        state.insert(id, 1);
        if let Some(e) = entries.iter().find(|e| e.id == id) {
            if let Some(l) = &e.launch {
                for d in &l.depends_on {
                    visit(d, entries, state, order)?;
                }
            }
        }
        state.insert(id, 2);
        order.push(id.to_string());
        Ok(())
    }
    for e in entries {
        visit(&e.id, entries, &mut state, &mut order)?;
    }
    Ok(order)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::geometry::{Grid, Selection};

    fn entry(id: &str, deps: &[&str]) -> LayoutEntry {
        LayoutEntry {
            id: id.into(),
            label: id.into(),
            matcher: Matcher { desktop_id: Some(format!("{id}.desktop")), ..Default::default() },
            placement: Placement::Grid { grid: Grid::new(2, 1), selection: Selection { col0: 0, row0: 0, col1: 0, row1: 0 } },
            output: OutputSel::Current,
            workspace: None,
            state: None,
            launch: Some(LaunchSpec { desktop_id: format!("{id}.desktop"), policy: LaunchPolicy::IfMissing, delay_ms: 0, depends_on: deps.iter().map(|s| s.to_string()).collect(), group: None }),
            instances: 1,
            excluded: false,
        }
    }

    #[test]
    fn order_and_cycles() {
        let es = vec![entry("ide", &["term"]), entry("term", &[]), entry("browser", &["ide"])];
        assert_eq!(launch_order(&es).unwrap(), vec!["term", "ide", "browser"]);
        let cyc = vec![entry("a", &["b"]), entry("b", &["a"])];
        assert!(launch_order(&cyc).is_err());
    }

    #[test]
    fn import_demotes_policy() {
        let mut l = Layout { schema_version: 1, name: "x".into(), description: String::new(), favorite: false, variants: vec![LayoutVariant { entries: vec![entry("a", &[])], ..Default::default() }], created_at: String::new(), updated_at: String::new() };
        assert!(l.validate().is_ok());
        l.sanitize_for_import(false);
        assert_eq!(l.variants[0].entries[0].launch.as_ref().unwrap().policy, LaunchPolicy::Ask);
    }
}
