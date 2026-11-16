//! Pure placement geometry: grid selections, work areas, margins, gaps and
//! normalized coordinates. All values are *logical* pixels (the coordinate
//! space the compositor exposes to window-management APIs). Nothing here
//! applies DPI scaling; adapters are responsible for supplying logical values.

use serde::{Deserialize, Serialize};

/// Smallest grid dimension (Divvy parity: 1x1).
pub const MIN_GRID: u32 = 1;
/// Largest base grid dimension (Divvy parity: 10x10).
pub const MAX_GRID: u32 = 10;
/// Temporary subdivision factor (Divvy: hold Alt to double resolution).
pub const SUBDIVISION_FACTOR: u32 = 2;
/// Largest effective grid dimension once subdivided (Divvy parity: 20x20).
pub const MAX_SUBDIVIDED_GRID: u32 = MAX_GRID * SUBDIVISION_FACTOR;
/// Divvy margin adjustment range upper bound in pixels.
pub const MAX_MARGIN_PX: u32 = 300;

/// Half-open logical rectangle: covers x..x+w, y..y+h.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize, Default)]
pub struct Rect {
    pub x: i32,
    pub y: i32,
    pub w: i32,
    pub h: i32,
}

impl Rect {
    pub const fn new(x: i32, y: i32, w: i32, h: i32) -> Self {
        Rect { x, y, w, h }
    }
    pub fn right(&self) -> i32 {
        self.x + self.w
    }
    pub fn bottom(&self) -> i32 {
        self.y + self.h
    }
    pub fn area(&self) -> i64 {
        (self.w.max(0) as i64) * (self.h.max(0) as i64)
    }
    pub fn is_empty(&self) -> bool {
        self.w <= 0 || self.h <= 0
    }
    pub fn center(&self) -> (i32, i32) {
        (self.x + self.w / 2, self.y + self.h / 2)
    }
    pub fn contains_point(&self, px: i32, py: i32) -> bool {
        px >= self.x && px < self.right() && py >= self.y && py < self.bottom()
    }
    pub fn intersection(&self, o: &Rect) -> Option<Rect> {
        let x0 = self.x.max(o.x);
        let y0 = self.y.max(o.y);
        let x1 = self.right().min(o.right());
        let y1 = self.bottom().min(o.bottom());
        if x1 > x0 && y1 > y0 {
            Some(Rect::new(x0, y0, x1 - x0, y1 - y0))
        } else {
            None
        }
    }
    pub fn intersection_area(&self, o: &Rect) -> i64 {
        self.intersection(o).map(|r| r.area()).unwrap_or(0)
    }
    /// Maximum per-edge absolute difference.
    pub fn max_edge_delta(&self, o: &Rect) -> i32 {
        [
            (self.x - o.x).abs(),
            (self.y - o.y).abs(),
            (self.right() - o.right()).abs(),
            (self.bottom() - o.bottom()).abs(),
        ]
        .into_iter()
        .max()
        .unwrap_or(0)
    }
    /// Squared distance between centers (used for nearest-monitor fallback).
    pub fn center_distance_sq(&self, o: &Rect) -> i64 {
        let (ax, ay) = self.center();
        let (bx, by) = o.center();
        let dx = (ax - bx) as i64;
        let dy = (ay - by) as i64;
        dx * dx + dy * dy
    }
    /// Center `self` (keeping size) inside `outer`, clamped so it stays visible.
    pub fn centered_in(&self, outer: &Rect) -> Rect {
        let w = self.w.min(outer.w.max(1));
        let h = self.h.min(outer.h.max(1));
        Rect::new(outer.x + (outer.w - w) / 2, outer.y + (outer.h - h) / 2, w, h)
    }
}

/// A length that is either absolute logical pixels or a percentage of the
/// relevant work-area dimension (so user intent survives fractional scaling
/// and resolution changes).
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
#[serde(tag = "unit", content = "value", rename_all = "snake_case")]
pub enum Length {
    Px(u32),
    Percent(f64),
}

impl Default for Length {
    fn default() -> Self {
        Length::Px(0)
    }
}

impl Length {
    /// Resolve against a dimension, rounding to the nearest pixel.
    pub fn resolve(&self, dimension: i32) -> i32 {
        match *self {
            Length::Px(p) => p.min(MAX_MARGIN_PX * 4) as i32,
            Length::Percent(pc) => {
                let pc = if pc.is_finite() { pc.clamp(0.0, 45.0) } else { 0.0 };
                ((dimension.max(0) as f64) * pc / 100.0).round() as i32
            }
        }
    }
}

/// Outer screen-edge margins (Divvy "screen edge" margins).
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize, Default)]
pub struct Margins {
    pub left: Length,
    pub right: Length,
    pub top: Length,
    pub bottom: Length,
}

impl Margins {
    pub fn uniform_px(px: u32) -> Self {
        Margins {
            left: Length::Px(px),
            right: Length::Px(px),
            top: Length::Px(px),
            bottom: Length::Px(px),
        }
    }
}

/// Inter-window gaps, independently configurable per axis.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize, Default)]
pub struct Gaps {
    /// Gap between horizontally adjacent windows (affects x/width).
    pub horizontal: Length,
    /// Gap between vertically adjacent windows (affects y/height).
    pub vertical: Length,
}

/// Spacing configuration applied to a placement.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize, Default)]
pub struct Spacing {
    pub margins_enabled: bool,
    pub margins: Margins,
    pub gaps_enabled: bool,
    pub gaps: Gaps,
}

/// Grid dimensions.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct Grid {
    pub cols: u32,
    pub rows: u32,
}

impl Default for Grid {
    fn default() -> Self {
        Grid { cols: 6, rows: 6 }
    }
}

impl Grid {
    pub fn new(cols: u32, rows: u32) -> Self {
        Grid { cols, rows }
    }
    /// Base grids obey Divvy's 1..=10 range.
    pub fn is_valid_base(&self) -> bool {
        (MIN_GRID..=MAX_GRID).contains(&self.cols) && (MIN_GRID..=MAX_GRID).contains(&self.rows)
    }
    /// Any effective grid (including subdivided) obeys 1..=20.
    pub fn is_valid_effective(&self) -> bool {
        (MIN_GRID..=MAX_SUBDIVIDED_GRID).contains(&self.cols)
            && (MIN_GRID..=MAX_SUBDIVIDED_GRID).contains(&self.rows)
    }
    pub fn clamped_base(&self) -> Grid {
        Grid {
            cols: self.cols.clamp(MIN_GRID, MAX_GRID),
            rows: self.rows.clamp(MIN_GRID, MAX_GRID),
        }
    }
    pub fn subdivided(&self) -> Grid {
        Grid {
            cols: (self.cols * SUBDIVISION_FACTOR).min(MAX_SUBDIVIDED_GRID),
            rows: (self.rows * SUBDIVISION_FACTOR).min(MAX_SUBDIVIDED_GRID),
        }
    }
    /// Change column count by `delta`, clamped to the base range.
    pub fn with_cols_delta(&self, delta: i32) -> Grid {
        let c = (self.cols as i32 + delta).clamp(MIN_GRID as i32, MAX_GRID as i32) as u32;
        Grid { cols: c, rows: self.rows }
    }
    pub fn with_rows_delta(&self, delta: i32) -> Grid {
        let r = (self.rows as i32 + delta).clamp(MIN_GRID as i32, MAX_GRID as i32) as u32;
        Grid { cols: self.cols, rows: r }
    }
}

/// Inclusive cell bounds of a rectangular grid selection.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct Selection {
    pub col0: u32,
    pub row0: u32,
    pub col1: u32,
    pub row1: u32,
}

impl Selection {
    /// Build a normalized selection from two arbitrary corner cells
    /// (the user may drag in any direction).
    pub fn from_corners(a: (u32, u32), b: (u32, u32)) -> Self {
        Selection {
            col0: a.0.min(b.0),
            row0: a.1.min(b.1),
            col1: a.0.max(b.0),
            row1: a.1.max(b.1),
        }
    }
    pub fn full(grid: Grid) -> Self {
        Selection { col0: 0, row0: 0, col1: grid.cols - 1, row1: grid.rows - 1 }
    }
    pub fn is_valid_for(&self, grid: Grid) -> bool {
        self.col0 <= self.col1 && self.row0 <= self.row1 && self.col1 < grid.cols && self.row1 < grid.rows
    }
    pub fn clamp_to(&self, grid: Grid) -> Selection {
        let c1 = self.col1.min(grid.cols - 1);
        let r1 = self.row1.min(grid.rows - 1);
        Selection { col0: self.col0.min(c1), row0: self.row0.min(r1), col1: c1, row1: r1 }
    }
    /// Map a selection made on `grid` to the subdivided grid (covers the same area).
    pub fn subdivide(&self) -> Selection {
        let f = SUBDIVISION_FACTOR;
        Selection {
            col0: self.col0 * f,
            row0: self.row0 * f,
            col1: self.col1 * f + (f - 1),
            row1: self.row1 * f + (f - 1),
        }
    }
    /// Map a subdivided selection back to the base grid, preserving intent by
    /// covering every base cell the subdivided selection touches.
    pub fn undivide(&self) -> Selection {
        let f = SUBDIVISION_FACTOR;
        Selection { col0: self.col0 / f, row0: self.row0 / f, col1: self.col1 / f, row1: self.row1 / f }
    }
    /// Normalized fractions [x, y, w, h] of the grid this selection covers.
    pub fn to_fractions(&self, grid: Grid) -> NormRect {
        NormRect {
            x: self.col0 as f64 / grid.cols as f64,
            y: self.row0 as f64 / grid.rows as f64,
            w: (self.col1 - self.col0 + 1) as f64 / grid.cols as f64,
            h: (self.row1 - self.row0 + 1) as f64 / grid.rows as f64,
        }
    }
}

/// Rectangle in work-area-relative fractions (0..1).
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize, Default)]
pub struct NormRect {
    pub x: f64,
    pub y: f64,
    pub w: f64,
    pub h: f64,
}

impl NormRect {
    pub fn is_sane(&self) -> bool {
        let fin = self.x.is_finite() && self.y.is_finite() && self.w.is_finite() && self.h.is_finite();
        fin && self.w > 0.0
            && self.h > 0.0
            && self.x >= -1.0
            && self.y >= -1.0
            && self.x <= 2.0
            && self.y <= 2.0
            && self.w <= 3.0
            && self.h <= 3.0
    }
    pub fn as_array(&self) -> [f64; 4] {
        [self.x, self.y, self.w, self.h]
    }
    pub fn from_array(a: [f64; 4]) -> Self {
        NormRect { x: a[0], y: a[1], w: a[2], h: a[3] }
    }
}

/// Deterministic boundary of the k-th division of `len` into `n` parts.
/// Integer floor division means adjacent cells share boundaries exactly and
/// remainder pixels are distributed across cells without cumulative drift.
pub fn division_edge(origin: i32, len: i32, n: u32, k: u32) -> i32 {
    debug_assert!(n > 0 && k <= n);
    origin + ((len as i64 * k as i64) / n as i64) as i32
}

/// Work area after applying outer margins (if enabled).
pub fn inner_area(work_area: Rect, spacing: &Spacing) -> Rect {
    if !spacing.margins_enabled {
        return work_area;
    }
    let m = &spacing.margins;
    let l = m.left.resolve(work_area.w);
    let r = m.right.resolve(work_area.w);
    let t = m.top.resolve(work_area.h);
    let b = m.bottom.resolve(work_area.h);
    let w = (work_area.w - l - r).max(1);
    let h = (work_area.h - t - b).max(1);
    Rect::new(work_area.x + l.min(work_area.w - 1).max(0), work_area.y + t.min(work_area.h - 1).max(0), w, h)
}

/// Compute the target rectangle for a grid selection inside a work area,
/// with margins and gaps. Gaps are applied only on interior edges, split so
/// two adjacent windows are separated by exactly the configured gap.
pub fn selection_rect(work_area: Rect, grid: Grid, sel: Selection, spacing: &Spacing) -> Rect {
    let sel = sel.clamp_to(grid);
    let area = inner_area(work_area, spacing);
    let x0 = division_edge(area.x, area.w, grid.cols, sel.col0);
    let x1 = division_edge(area.x, area.w, grid.cols, sel.col1 + 1);
    let y0 = division_edge(area.y, area.h, grid.rows, sel.row0);
    let y1 = division_edge(area.y, area.h, grid.rows, sel.row1 + 1);
    let (mut x0, mut x1, mut y0, mut y1) = (x0, x1, y0, y1);
    if spacing.gaps_enabled {
        let gh = spacing.gaps.horizontal.resolve(area.w);
        let gv = spacing.gaps.vertical.resolve(area.h);
        // Leading interior edge takes ceil(g/2), trailing interior edge floor(g/2):
        // neighbour A's trailing + neighbour B's leading = exactly g.
        if sel.col0 > 0 {
            x0 += gh - gh / 2;
        }
        if sel.col1 + 1 < grid.cols {
            x1 -= gh / 2;
        }
        if sel.row0 > 0 {
            y0 += gv - gv / 2;
        }
        if sel.row1 + 1 < grid.rows {
            y1 -= gv / 2;
        }
    }
    Rect::new(x0, y0, (x1 - x0).max(1), (y1 - y0).max(1))
}

/// Same as [`selection_rect`] but from arbitrary normalized fractions
/// (used by layouts, snapshots and snap commands).
pub fn norm_rect_to_rect(work_area: Rect, n: NormRect, spacing: &Spacing) -> Rect {
    let area = inner_area(work_area, spacing);
    let fx0 = n.x.clamp(0.0, 1.0);
    let fy0 = n.y.clamp(0.0, 1.0);
    let fx1 = (n.x + n.w).clamp(0.0, 1.0);
    let fy1 = (n.y + n.h).clamp(0.0, 1.0);
    let px = |f: f64| area.x + (area.w as f64 * f).round() as i32;
    let py = |f: f64| area.y + (area.h as f64 * f).round() as i32;
    let (mut x0, mut x1, mut y0, mut y1) = (px(fx0), px(fx1), py(fy0), py(fy1));
    if spacing.gaps_enabled {
        let gh = spacing.gaps.horizontal.resolve(area.w);
        let gv = spacing.gaps.vertical.resolve(area.h);
        const EPS: f64 = 1e-9;
        if fx0 > EPS {
            x0 += gh - gh / 2;
        }
        if fx1 < 1.0 - EPS {
            x1 -= gh / 2;
        }
        if fy0 > EPS {
            y0 += gv - gv / 2;
        }
        if fy1 < 1.0 - EPS {
            y1 -= gv / 2;
        }
    }
    Rect::new(x0, y0, (x1 - x0).max(1), (y1 - y0).max(1))
}

/// Express an absolute rectangle as fractions of a work area.
pub fn rect_to_norm(work_area: Rect, r: Rect) -> NormRect {
    let w = work_area.w.max(1) as f64;
    let h = work_area.h.max(1) as f64;
    NormRect {
        x: (r.x - work_area.x) as f64 / w,
        y: (r.y - work_area.y) as f64 / h,
        w: r.w as f64 / w,
        h: r.h as f64 / h,
    }
}

/// Remap a rectangle from one work area to another, preserving relative
/// position and size (used for moving windows between monitors and for
/// restoring snapshots on a changed topology).
pub fn remap_rect(r: Rect, from: Rect, to: Rect) -> Rect {
    let n = rect_to_norm(from, r);
    let x = to.x + (n.x * to.w as f64).round() as i32;
    let y = to.y + (n.y * to.h as f64).round() as i32;
    let w = ((n.w * to.w as f64).round() as i32).clamp(1, to.w.max(1));
    let h = ((n.h * to.h as f64).round() as i32).clamp(1, to.h.max(1));
    // keep fully inside destination where possible
    let x = x.clamp(to.x, (to.right() - w).max(to.x));
    let y = y.clamp(to.y, (to.bottom() - h).max(to.y));
    Rect::new(x, y, w, h)
}

/// Size constraints reported by a window (WM_NORMAL_HINTS or compositor).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
pub struct SizeConstraints {
    pub min_w: Option<i32>,
    pub min_h: Option<i32>,
    pub max_w: Option<i32>,
    pub max_h: Option<i32>,
    pub inc_w: Option<i32>,
    pub inc_h: Option<i32>,
}

impl SizeConstraints {
    /// The stricter of two sets of limits.
    pub fn merged(&self, o: &SizeConstraints) -> SizeConstraints {
        fn hi(a: Option<i32>, b: Option<i32>) -> Option<i32> {
            match (a, b) {
                (Some(x), Some(y)) => Some(x.max(y)),
                (x, y) => x.or(y),
            }
        }
        fn lo(a: Option<i32>, b: Option<i32>) -> Option<i32> {
            match (a, b) {
                (Some(x), Some(y)) => Some(x.min(y)),
                (x, y) => x.or(y),
            }
        }
        SizeConstraints {
            min_w: hi(self.min_w, o.min_w),
            min_h: hi(self.min_h, o.min_h),
            max_w: lo(self.max_w, o.max_w),
            max_h: lo(self.max_h, o.max_h),
            inc_w: self.inc_w.or(o.inc_w),
            inc_h: self.inc_h.or(o.inc_h),
        }
    }
}

/// Minimum sizes revealed by a placement: the window ended up larger than
/// requested. Wayland compositors do not expose client minimum sizes to
/// Fractile, so this is how they become known. Smaller-than-requested
/// results are not treated as maximums (the app may still be resizing).
pub fn learn_min_size(requested: Rect, observed: Rect, tolerance: i32) -> Option<SizeConstraints> {
    let mut c = SizeConstraints::default();
    if observed.w > requested.w + tolerance {
        c.min_w = Some(observed.w);
    }
    if observed.h > requested.h + tolerance {
        c.min_h = Some(observed.h);
    }
    (c.min_w.is_some() || c.min_h.is_some()).then_some(c)
}

/// Policy when the requested rectangle violates window constraints.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum ConstraintPolicy {
    /// Clamp size to the constraint and keep the top-left anchor (default).
    #[default]
    Fit,
    /// Clamp size and center the result inside the requested rectangle.
    Center,
    /// Refuse the placement and report.
    Fail,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ConstrainedRect {
    pub rect: Rect,
    pub adjusted: bool,
    pub reason: Option<String>,
}

/// Apply size constraints according to policy. Returns Err with a reason
/// for `ConstraintPolicy::Fail` when constraints are violated.
pub fn apply_constraints(req: Rect, c: &SizeConstraints, policy: ConstraintPolicy) -> Result<ConstrainedRect, String> {
    let mut w = req.w;
    let mut h = req.h;
    let mut reasons = vec![];
    if let Some(mw) = c.min_w.filter(|v| *v > 0) {
        if w < mw {
            w = mw;
            reasons.push(format!("min width {mw}"));
        }
    }
    if let Some(mh) = c.min_h.filter(|v| *v > 0) {
        if h < mh {
            h = mh;
            reasons.push(format!("min height {mh}"));
        }
    }
    if let Some(xw) = c.max_w.filter(|v| *v > 0) {
        if w > xw {
            w = xw;
            reasons.push(format!("max width {xw}"));
        }
    }
    if let Some(xh) = c.max_h.filter(|v| *v > 0) {
        if h > xh {
            h = xh;
            reasons.push(format!("max height {xh}"));
        }
    }
    if reasons.is_empty() {
        return Ok(ConstrainedRect { rect: req, adjusted: false, reason: None });
    }
    let reason = reasons.join(", ");
    match policy {
        ConstraintPolicy::Fail => Err(format!("window constraints prevent requested size: {reason}")),
        ConstraintPolicy::Fit => Ok(ConstrainedRect { rect: Rect::new(req.x, req.y, w, h), adjusted: true, reason: Some(reason) }),
        ConstraintPolicy::Center => Ok(ConstrainedRect {
            rect: Rect::new(req.x + (req.w - w) / 2, req.y + (req.h - h) / 2, w, h),
            adjusted: true,
            reason: Some(reason),
        }),
    }
}

/// Move `r` (not resize it) so it lies inside `area`, as close to where it
/// was as possible. Used when a window's minimum size makes it larger than
/// the cell it was placed on: without this, the part past the monitor edge
/// makes the compositor move the window, sometimes onto another monitor. A
/// rectangle larger than `area` is aligned to its top-left corner.
pub fn shift_into(r: Rect, area: Rect) -> Rect {
    let x = if r.w >= area.w { area.x } else { r.x.clamp(area.x, area.x + area.w - r.w) };
    let y = if r.h >= area.h { area.y } else { r.y.clamp(area.y, area.y + area.h - r.h) };
    Rect::new(x, y, r.w, r.h)
}

/// Whether an observed frame after moving a window between monitors of
/// different scale (`ratio` = target scale / source scale, not 1) looks like
/// the compositor's transient rescale rather than the window's own limits:
/// the size is the request multiplied or divided by the ratio, or the window
/// ended up outside the requested monitor's `target_area` without growing
/// past the request (a window that grew shows a real minimum size, and the
/// compositor skipping the move is the known reaction to that). Seen with Mutter
/// in its physical layout mode (GNOME 45) while the client switches its
/// buffer scale. Such an observation is re-requested, never learned from.
pub fn scale_transient(requested: Rect, observed: Rect, ratio: f64, target_area: Rect, tolerance: i32) -> bool {
    if !(ratio.is_finite() && ratio > 0.0) || (ratio - 1.0).abs() < 1e-6 {
        return false;
    }
    let near = |a: i32, b: f64| (a as f64 - b).abs() <= tolerance as f64 * ratio.max(1.0 / ratio);
    let scaled = |k: f64| near(observed.w, requested.w as f64 * k) && near(observed.h, requested.h as f64 * k);
    let (cx, cy) = observed.center();
    // Any growth counts: a window 2 px over the request that the compositor
    // did not move (GNOME 47, a 73 px minimum for a 71 px cell) has a limit.
    let grew = observed.w > requested.w || observed.h > requested.h;
    scaled(ratio) || scaled(1.0 / ratio) || (!grew && !target_area.contains_point(cx, cy))
}

/// Learned minimum sizes the window has since shown it does not have (it was
/// observed smaller) are dropped. Returns None when nothing is left.
pub fn relax_learned(learned: &SizeConstraints, observed: Rect, tolerance: i32) -> Option<SizeConstraints> {
    let mut l = *learned;
    if l.min_w.is_some_and(|m| observed.w + tolerance < m) {
        l.min_w = None;
    }
    if l.min_h.is_some_and(|m| observed.h + tolerance < m) {
        l.min_h = None;
    }
    (l != SizeConstraints::default()).then_some(l)
}

/// Keyboard snap commands (beyond Divvy) expressed as normalized rects.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum SnapAction {
    LeftHalf,
    RightHalf,
    TopHalf,
    BottomHalf,
    LeftThird,
    CenterThird,
    RightThird,
    LeftTwoThirds,
    RightTwoThirds,
    TopLeftQuarter,
    TopRightQuarter,
    BottomLeftQuarter,
    BottomRightQuarter,
    Center,
    Maximize,
}

impl SnapAction {
    pub const ALL: [SnapAction; 15] = [
        SnapAction::LeftHalf,
        SnapAction::RightHalf,
        SnapAction::TopHalf,
        SnapAction::BottomHalf,
        SnapAction::LeftThird,
        SnapAction::CenterThird,
        SnapAction::RightThird,
        SnapAction::LeftTwoThirds,
        SnapAction::RightTwoThirds,
        SnapAction::TopLeftQuarter,
        SnapAction::TopRightQuarter,
        SnapAction::BottomLeftQuarter,
        SnapAction::BottomRightQuarter,
        SnapAction::Center,
        SnapAction::Maximize,
    ];

    /// Grid + selection equivalent, so snaps share the exact grid math.
    pub fn grid_selection(&self) -> (Grid, Selection) {
        use SnapAction::*;
        let s = |c0, r0, c1, r1| Selection { col0: c0, row0: r0, col1: c1, row1: r1 };
        match self {
            LeftHalf => (Grid::new(2, 1), s(0, 0, 0, 0)),
            RightHalf => (Grid::new(2, 1), s(1, 0, 1, 0)),
            TopHalf => (Grid::new(1, 2), s(0, 0, 0, 0)),
            BottomHalf => (Grid::new(1, 2), s(0, 1, 0, 1)),
            LeftThird => (Grid::new(3, 1), s(0, 0, 0, 0)),
            CenterThird => (Grid::new(3, 1), s(1, 0, 1, 0)),
            RightThird => (Grid::new(3, 1), s(2, 0, 2, 0)),
            LeftTwoThirds => (Grid::new(3, 1), s(0, 0, 1, 0)),
            RightTwoThirds => (Grid::new(3, 1), s(1, 0, 2, 0)),
            TopLeftQuarter => (Grid::new(2, 2), s(0, 0, 0, 0)),
            TopRightQuarter => (Grid::new(2, 2), s(1, 0, 1, 0)),
            BottomLeftQuarter => (Grid::new(2, 2), s(0, 1, 0, 1)),
            BottomRightQuarter => (Grid::new(2, 2), s(1, 1, 1, 1)),
            Center => (Grid::new(4, 4), s(1, 1, 2, 2)),
            Maximize => (Grid::new(1, 1), s(0, 0, 0, 0)),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const WA: Rect = Rect::new(0, 32, 2560, 1408);

    #[test]
    fn full_grid_covers_work_area() {
        let g = Grid::default();
        let r = selection_rect(WA, g, Selection::full(g), &Spacing::default());
        assert_eq!(r, WA);
    }

    #[test]
    fn halves_are_seamless() {
        let g = Grid::new(6, 6);
        let l = selection_rect(WA, g, Selection { col0: 0, row0: 0, col1: 2, row1: 5 }, &Spacing::default());
        let r = selection_rect(WA, g, Selection { col0: 3, row0: 0, col1: 5, row1: 5 }, &Spacing::default());
        assert_eq!(l.right(), r.x);
        assert_eq!(l.w + r.w, WA.w);
    }

    #[test]
    fn thirds_on_odd_width_have_no_drift() {
        let wa = Rect::new(-1000, 0, 1001, 700);
        let g = Grid::new(3, 1);
        let mut x = wa.x;
        for c in 0..3 {
            let r = selection_rect(wa, g, Selection { col0: c, row0: 0, col1: c, row1: 0 }, &Spacing::default());
            assert_eq!(r.x, x);
            x = r.right();
        }
        assert_eq!(x, wa.right());
    }

    /// A portrait monitor left of and above the primary (negative x and y,
    /// odd sizes): every cell of a 20x20 grid tiles its work area exactly,
    /// with no gaps, overlaps or drift.
    #[test]
    fn portrait_monitor_at_negative_origin_tiles_exactly() {
        let wa = Rect::new(-1081, -333, 1081, 1917);
        let g = Grid::new(20, 20);
        let mut area = 0i64;
        for c in 0..20 {
            for r in 0..20 {
                let cell = selection_rect(wa, g, Selection { col0: c, row0: r, col1: c, row1: r }, &Spacing::default());
                assert!(cell.x >= wa.x && cell.y >= wa.y && cell.right() <= wa.right() && cell.bottom() <= wa.bottom(), "{cell:?}");
                if c + 1 < 20 {
                    let n = selection_rect(wa, g, Selection { col0: c + 1, row0: r, col1: c + 1, row1: r }, &Spacing::default());
                    assert_eq!(cell.right(), n.x);
                }
                if r + 1 < 20 {
                    let n = selection_rect(wa, g, Selection { col0: c, row0: r + 1, col1: c, row1: r + 1 }, &Spacing::default());
                    assert_eq!(cell.bottom(), n.y);
                }
                area += cell.w as i64 * cell.h as i64;
            }
        }
        assert_eq!(area, wa.w as i64 * wa.h as i64);
        let full = selection_rect(wa, g, Selection { col0: 0, row0: 0, col1: 19, row1: 19 }, &Spacing::default());
        assert_eq!(full, wa);
    }

    #[test]
    fn gaps_are_exact_between_neighbours() {
        let sp = Spacing {
            gaps_enabled: true,
            gaps: Gaps { horizontal: Length::Px(11), vertical: Length::Px(7) },
            ..Default::default()
        };
        let g = Grid::new(2, 2);
        let a = selection_rect(WA, g, Selection { col0: 0, row0: 0, col1: 0, row1: 0 }, &sp);
        let b = selection_rect(WA, g, Selection { col0: 1, row0: 0, col1: 1, row1: 0 }, &sp);
        let c = selection_rect(WA, g, Selection { col0: 0, row0: 1, col1: 0, row1: 1 }, &sp);
        assert_eq!(b.x - a.right(), 11);
        assert_eq!(c.y - a.bottom(), 7);
        // outer edges untouched by gaps
        assert_eq!(a.x, WA.x);
        assert_eq!(a.y, WA.y);
        assert_eq!(b.right(), WA.right());
    }

    #[test]
    fn margins_shrink_from_each_side() {
        let sp = Spacing {
            margins_enabled: true,
            margins: Margins {
                left: Length::Px(10),
                right: Length::Px(20),
                top: Length::Px(30),
                bottom: Length::Percent(10.0),
            },
            ..Default::default()
        };
        let g = Grid::new(1, 1);
        let r = selection_rect(WA, g, Selection::full(g), &sp);
        assert_eq!(r, Rect::new(10, 62, 2560 - 30, 1408 - 30 - 141));
    }

    #[test]
    fn subdivision_round_trip_preserves_intent() {
        let s = Selection { col0: 1, row0: 2, col1: 3, row1: 2 };
        let d = s.subdivide();
        assert_eq!(d, Selection { col0: 2, row0: 4, col1: 7, row1: 5 });
        assert_eq!(d.undivide(), s);
        let g = Grid::new(6, 6);
        let sp = Spacing::default();
        assert_eq!(selection_rect(WA, g, s, &sp), selection_rect(WA, g.subdivided(), d, &sp));
    }

    #[test]
    fn grid_limits() {
        assert!(Grid::new(1, 1).is_valid_base());
        assert!(Grid::new(10, 10).is_valid_base());
        assert!(!Grid::new(11, 10).is_valid_base());
        assert_eq!(Grid::new(10, 10).subdivided(), Grid::new(20, 20));
        assert_eq!(Grid::new(10, 3).with_cols_delta(1), Grid::new(10, 3));
        assert_eq!(Grid::new(1, 3).with_rows_delta(-5), Grid::new(1, 1));
    }

    #[test]
    fn remap_between_monitors() {
        let a = Rect::new(0, 0, 2560, 1440);
        let b = Rect::new(2560, 0, 3440, 1440);
        let r = Rect::new(0, 0, 1280, 1440);
        assert_eq!(remap_rect(r, a, b), Rect::new(2560, 0, 1720, 1440));
    }

    #[test]
    fn a_rescale_while_crossing_monitors_is_a_transient_not_a_limit() {
        let wa = Rect::new(0, 32, 1600, 1168);
        let req = Rect::new(800, 64, 400, 1136);
        // GNOME 45, scale 1 -> 2: frame doubled
        assert!(scale_transient(req, Rect::new(800, 64, 800, 2272), 2.0, wa, 2));
        // or halved
        assert!(scale_transient(Rect::new(888, 64, 178, 1136), Rect::new(888, 64, 89, 568), 2.0, wa, 2));
        // or left on the other monitor
        assert!(scale_transient(Rect::new(888, 64, 178, 1136), Rect::new(1600, 0, 178, 600), 2.0, wa, 2));
        // a real minimum width on the right monitor is not a transient
        assert!(!scale_transient(Rect::new(800, 632, 200, 568), Rect::new(800, 632, 292, 568), 2.0, wa, 2));
        // nor is a window grown to its minimum and left on the old monitor
        // (GNOME 45 skips the move then): that minimum must be learned
        let wb = Rect::new(1600, 0, 1280, 1024);
        assert!(!scale_transient(Rect::new(2026, 409, 285, 307), Rect::new(1280, 632, 292, 306), 0.5, wb, 2));
        // grown by no more than the tolerance and not moved (GNOME 47)
        let wl = Rect::new(0, 32, 800, 568);
        assert!(!scale_transient(Rect::new(0, 529, 800, 71), Rect::new(800, 682, 800, 73), 2.0, wl, 2));
        // same scale: never
        assert!(!scale_transient(req, Rect::new(800, 64, 800, 2272), 1.0, wa, 2));
    }

    #[test]
    fn a_learned_minimum_the_window_later_undercuts_is_dropped() {
        let l = SizeConstraints { min_w: Some(800), min_h: Some(2272), ..Default::default() };
        assert_eq!(relax_learned(&l, Rect::new(0, 64, 800, 1136), 2), Some(SizeConstraints { min_w: Some(800), ..Default::default() }));
        assert_eq!(relax_learned(&l, Rect::new(0, 64, 400, 1136), 2), None);
        assert_eq!(relax_learned(&l, Rect::new(0, 64, 900, 2300), 2), Some(l));
    }

    #[test]
    fn shift_into_keeps_a_grown_window_on_its_monitor() {
        let wa = Rect::new(0, 32, 800, 568);
        // bottom cell, window 2 px taller than the cell: moved up, not resized
        assert_eq!(shift_into(Rect::new(0, 529, 800, 73), wa), Rect::new(0, 527, 800, 73));
        // right edge
        assert_eq!(shift_into(Rect::new(700, 100, 200, 50), wa), Rect::new(600, 100, 200, 50));
        // already inside: unchanged
        assert_eq!(shift_into(Rect::new(10, 40, 100, 100), wa), Rect::new(10, 40, 100, 100));
        // larger than the area: top-left aligned
        assert_eq!(shift_into(Rect::new(300, 300, 900, 700), wa), Rect::new(0, 32, 900, 700));
        // second monitor to the right, left of it overflowing
        let wb = Rect::new(800, 0, 1280, 1024);
        assert_eq!(shift_into(Rect::new(780, 10, 300, 300), wb), Rect::new(800, 10, 300, 300));
    }

    #[test]
    fn learned_minimum_sizes() {
        let req = Rect::new(2311, 0, 142, 1024);
        // the window kept a larger width and ignored the move
        let obs = Rect::new(159, 140, 146, 1024);
        let l = learn_min_size(req, obs, 2).unwrap();
        assert_eq!((l.min_w, l.min_h), (Some(146), None));
        // within tolerance or smaller than requested teaches nothing
        assert!(learn_min_size(req, Rect::new(0, 0, 143, 1024), 2).is_none());
        assert!(learn_min_size(req, Rect::new(0, 0, 100, 900), 2).is_none());
        // merging keeps the stricter limits
        let a = SizeConstraints { min_w: Some(200), max_w: Some(900), ..Default::default() };
        let m = a.merged(&SizeConstraints { min_w: Some(146), min_h: Some(80), max_w: Some(800), ..Default::default() });
        assert_eq!((m.min_w, m.min_h, m.max_w), (Some(200), Some(80), Some(800)));
        assert_eq!(apply_constraints(req, &m, ConstraintPolicy::Fit).unwrap().rect, Rect::new(2311, 0, 200, 1024));
    }

    #[test]
    fn constraints() {
        let c = SizeConstraints { min_w: Some(800), ..Default::default() };
        let r = Rect::new(0, 0, 400, 300);
        assert_eq!(apply_constraints(r, &c, ConstraintPolicy::Fit).unwrap().rect, Rect::new(0, 0, 800, 300));
        assert_eq!(apply_constraints(r, &c, ConstraintPolicy::Center).unwrap().rect, Rect::new(-200, 0, 800, 300));
        assert!(apply_constraints(r, &c, ConstraintPolicy::Fail).is_err());
    }

    #[test]
    fn snap_actions_match_grid() {
        let sp = Spacing::default();
        let (g, s) = SnapAction::LeftHalf.grid_selection();
        assert_eq!(selection_rect(WA, g, s, &sp), Rect::new(0, 32, 1280, 1408));
        let (g, s) = SnapAction::Maximize.grid_selection();
        assert_eq!(selection_rect(WA, g, s, &sp), WA);
    }
}
