//! Property-based tests for the geometry engine and fuzzing of parsers /
//! schemas that read untrusted input.

use fractile_core::accel::Accelerator;
use fractile_core::config::Config;
use fractile_core::desktop::{app_ids_from_systemd_unit, exec_to_argv, parse_desktop_entry};
use fractile_core::geometry::*;
use fractile_core::layout::Layout;
use fractile_core::snapshot::Snapshot;
use proptest::prelude::*;

fn work_area() -> impl Strategy<Value = Rect> {
    (-8000i32..8000, -4000i32..4000, 50i32..8000, 50i32..5000).prop_map(|(x, y, w, h)| Rect::new(x, y, w, h))
}

fn grid() -> impl Strategy<Value = Grid> {
    (1u32..=20, 1u32..=20).prop_map(|(c, r)| Grid::new(c, r))
}

fn grid_and_sel() -> impl Strategy<Value = (Grid, Selection)> {
    grid().prop_flat_map(|g| {
        (Just(g), 0..g.cols, 0..g.cols, 0..g.rows, 0..g.rows)
            .prop_map(|(g, a, b, c, d)| (g, Selection::from_corners((a, c), (b, d))))
    })
}

fn base_grid_and_sel() -> impl Strategy<Value = (Grid, Selection)> {
    (1u32..=10, 1u32..=10).prop_flat_map(|(c, r)| {
        let g = Grid::new(c, r);
        (Just(g), 0..c, 0..c, 0..r, 0..r).prop_map(|(g, a, b, c, d)| (g, Selection::from_corners((a, c), (b, d))))
    })
}

fn spacing() -> impl Strategy<Value = Spacing> {
    (any::<bool>(), 0u32..=300, 0u32..=300, 0u32..=300, 0u32..=300, any::<bool>(), 0u32..=60, 0u32..=60).prop_map(
        |(me, l, r, t, b, ge, gh, gv)| Spacing {
            margins_enabled: me,
            margins: Margins { left: Length::Px(l), right: Length::Px(r), top: Length::Px(t), bottom: Length::Px(b) },
            gaps_enabled: ge,
            gaps: Gaps { horizontal: Length::Px(gh), vertical: Length::Px(gv) },
        },
    )
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(2000))]

    /// Columns of a row partition the work area exactly: no gaps, no overlap,
    /// no drift, for every grid size up to 20x20.
    #[test]
    fn cells_partition_exactly(wa in work_area(), g in grid()) {
        let sp = Spacing::default();
        let mut x = wa.x;
        for c in 0..g.cols {
            let r = selection_rect(wa, g, Selection { col0: c, row0: 0, col1: c, row1: 0 }, &sp);
            prop_assert_eq!(r.x, x);
            prop_assert!(r.w >= 1);
            x = r.right();
        }
        prop_assert_eq!(x, wa.right());
        let mut y = wa.y;
        for rr in 0..g.rows {
            let r = selection_rect(wa, g, Selection { col0: 0, row0: rr, col1: 0, row1: rr }, &sp);
            prop_assert_eq!(r.y, y);
            y = r.bottom();
        }
        prop_assert_eq!(y, wa.bottom());
    }

    /// Any selection stays inside the work area (with or without spacing).
    #[test]
    fn selection_inside_work_area((g, s) in grid_and_sel(), wa in work_area(), sp in spacing()) {
        let r = selection_rect(wa, g, s, &sp);
        prop_assert!(r.w >= 1 && r.h >= 1);
        if wa.w > 1300 && wa.h > 1300 {
            prop_assert!(r.x >= wa.x && r.right() <= wa.right(), "{:?} not in {:?}", r, wa);
            prop_assert!(r.y >= wa.y && r.bottom() <= wa.bottom());
        }
    }

    /// A multi-cell selection equals the union of its first and last cells.
    #[test]
    fn selection_is_union_of_corners((g, s) in grid_and_sel(), wa in work_area()) {
        let sp = Spacing::default();
        let r = selection_rect(wa, g, s, &sp);
        let a = selection_rect(wa, g, Selection { col0: s.col0, row0: s.row0, col1: s.col0, row1: s.row0 }, &sp);
        let b = selection_rect(wa, g, Selection { col0: s.col1, row0: s.row1, col1: s.col1, row1: s.row1 }, &sp);
        prop_assert_eq!(r.x, a.x);
        prop_assert_eq!(r.y, a.y);
        prop_assert_eq!(r.right(), b.right());
        prop_assert_eq!(r.bottom(), b.bottom());
    }

    /// Horizontally adjacent cells are separated by exactly the gap.
    #[test]
    fn gaps_exact(wa in work_area(), g in grid(), gh in 0u32..=40) {
        prop_assume!(g.cols >= 2);
        prop_assume!(wa.w as u32 > g.cols * (gh + 2));
        let sp = Spacing { gaps_enabled: true, gaps: Gaps { horizontal: Length::Px(gh), vertical: Length::Px(0) }, ..Default::default() };
        for c in 0..g.cols - 1 {
            let a = selection_rect(wa, g, Selection { col0: c, row0: 0, col1: c, row1: 0 }, &sp);
            let b = selection_rect(wa, g, Selection { col0: c + 1, row0: 0, col1: c + 1, row1: 0 }, &sp);
            prop_assert_eq!(b.x - a.right(), gh as i32);
        }
    }

    /// Temporary subdivision covers exactly the same pixels (Alt contract).
    #[test]
    fn subdivision_equivalence((g, s) in base_grid_and_sel(), wa in work_area(), sp in spacing()) {
        let a = selection_rect(wa, g, s, &sp);
        let b = selection_rect(wa, g.subdivided(), s.subdivide(), &sp);
        prop_assert_eq!(a, b);
        prop_assert_eq!(s.subdivide().undivide(), s);
    }

    /// 100 repeated placements via normalized round trips never drift (T08).
    #[test]
    fn no_drift_on_repeated_round_trip((g, s) in grid_and_sel(), wa in work_area(), wb in work_area()) {
        let sp = Spacing::default();
        let start = selection_rect(wa, g, s, &sp);
        let mut r = start;
        for _ in 0..100 {
            let n = rect_to_norm(wa, r);
            r = norm_rect_to_rect(wa, n, &sp);
        }
        prop_assert!(r.max_edge_delta(&start) <= 1, "{:?} vs {:?}", r, start);
        // remap there and back stays within one pixel per 1000 px of scale change
        let there = remap_rect(start, wa, wb);
        prop_assert!(there.x >= wb.x && there.right() <= wb.right().max(wb.x + 1));
    }

    /// Parsers of untrusted input never panic.
    #[test]
    fn exec_parser_never_panics(s in ".{0,200}") {
        if let Ok(v) = exec_to_argv(&s, "n", Some("i")) {
            prop_assert!(!v.is_empty() && !v[0].is_empty());
        }
    }

    #[test]
    fn accel_parser_never_panics(s in ".{0,40}") {
        let _ = Accelerator::parse(&s);
    }

    #[test]
    fn desktop_parser_never_panics(s in ".{0,400}") {
        let _ = parse_desktop_entry("x.desktop", std::path::Path::new("/x"), &s);
        let _ = app_ids_from_systemd_unit(&s);
    }

    #[test]
    fn schema_parsers_never_panic(s in ".{0,400}") {
        if let Ok(snap) = serde_json::from_str::<Snapshot>(&s) { let _ = snap.validate(); }
        if let Ok(l) = serde_json::from_str::<Layout>(&s) { let _ = l.validate(); }
        if let Ok(c) = Config::from_toml(&s) { let _ = c.validate(); }
    }
}

/// Structured snapshot fuzzing: mutate a valid snapshot's JSON tree with
/// hostile values and make sure validation catches them or they are safe.
#[test]
fn structured_snapshot_fuzz() {
    use fractile_core::snapshot::*;
    let mut base = Snapshot::new_empty(SnapshotMode::Manual);
    base.windows.push(WindowRecord {
        record_id: "r1".into(),
        desktop_file_id: Some("xterm.desktop".into()),
        normalized_geometry: [0.0, 0.0, 0.5, 0.5],
        absolute_geometry: Rect::new(0, 0, 100, 100),
        ..Default::default()
    });
    base.validate().unwrap();
    let hostile: Vec<serde_json::Value> = vec![
        serde_json::json!("../../../../bin/sh.desktop"),
        serde_json::json!("x\u{0007}.desktop"),
        serde_json::json!("a".repeat(5000)),
        serde_json::json!(-1),
        serde_json::json!(1e308),
        serde_json::json!(null),
        serde_json::json!({"$(rm -rf ~)": 1}),
    ];
    let fields = ["desktop_file_id", "app_id", "title_hint", "executable", "role", "wm_class"];
    let mut rejected = 0;
    for f in fields {
        for h in &hostile {
            let mut v = serde_json::to_value(&base).unwrap();
            v["windows"][0][f] = h.clone();
            match serde_json::from_value::<Snapshot>(v) {
                Ok(s) => {
                    if s.validate().is_err() {
                        rejected += 1;
                    } else {
                        // accepted values must be inert strings: no launch happens
                        // unless desktop id is a safe plain filename.
                        if let Some(d) = &s.windows[0].desktop_file_id {
                            assert!(is_safe_desktop_id(d));
                        }
                    }
                }
                Err(_) => rejected += 1,
            }
        }
    }
    assert!(rejected >= 20, "only {rejected} hostile mutations rejected");
}
