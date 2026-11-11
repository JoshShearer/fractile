//! Manual probe: `cargo run -p fractile-adapter-x11 --example probe -- [move <id> x y w h]`
use fractile_adapter_x11::X11Adapter;
use fractile_core::adapter::CompositorAdapter;
use fractile_core::geometry::Rect;

fn main() {
    let a = X11Adapter::connect(None).expect("connect");
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.first().map(|s| s.as_str()) == Some("move") {
        let n: Vec<i32> = args[2..6].iter().map(|v| v.parse().unwrap()).collect();
        let r = a.move_resize(&args[1], Rect::new(n[0], n[1], n[2], n[3])).expect("move");
        println!("{}", serde_json_like(&format!("{r:?}")));
        return;
    }
    println!("wm: {}", a.wm_name());
    for o in a.get_outputs().unwrap() {
        println!("output {} {} rect={:?} work={:?} primary={}", o.stable_id, o.name, o.rect, o.work_area, o.primary);
    }
    for w in a.get_workspaces().unwrap() {
        println!("workspace {} {} active={}", w.index, w.name, w.active);
    }
    println!("active: {:?}", a.get_active_window().unwrap());
    for w in a.list_windows().unwrap() {
        println!("{} class={:?} type={:?} frame={:?} ws={:?} placeable={} title={:?}", w.id, w.wm_class, w.window_type, w.frame, w.workspace, w.is_placeable(), w.title);
    }
}
fn serde_json_like(s: &str) -> String { s.to_string() }
