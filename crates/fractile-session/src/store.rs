//! SQLite persistence (WAL, synchronous=FULL). Snapshots are checksummed;
//! a snapshot whose checksum fails is never returned as "latest good".

use crate::restore::ItemResult;
use fractile_core::layout::Layout;
use fractile_core::snapshot::{Snapshot, SnapshotMode};
use rusqlite::{params, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};

pub const DB_SCHEMA_VERSION: i64 = 1;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct SnapshotMeta {
    pub id: String,
    pub created_at: String,
    pub name: Option<String>,
    pub mode: SnapshotMode,
    pub window_count: usize,
    pub app_count: usize,
    pub checksum_ok: bool,
    pub pinned: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct SessionRecord {
    pub session_key: String,
    pub boot_id: String,
    pub started_at: String,
    pub ended_at: Option<String>,
    pub clean: bool,
    pub restored: bool,
}

/// A restore run as stored.
#[derive(Debug, Clone, PartialEq)]
pub struct RunRow {
    pub source: String,
    pub source_id: Option<String>,
    pub state: String,
    pub summary: Option<String>,
}

/// One item of a restore run as stored.
#[derive(Debug, Clone, PartialEq)]
pub struct ItemRow {
    pub record_id: String,
    pub status: String,
    pub window_id: Option<String>,
    pub launched_at: Option<String>,
    pub label: String,
    pub message: Option<String>,
}

pub struct Store {
    conn: Connection,
    path: PathBuf,
}

fn mode_str(m: SnapshotMode) -> String {
    serde_json::to_value(m).ok().and_then(|v| v.as_str().map(|s| s.to_string())).unwrap_or_else(|| "manual".into())
}

fn mode_from(s: &str) -> SnapshotMode {
    serde_json::from_value(serde_json::Value::String(s.into())).unwrap_or_default()
}

impl Store {
    pub fn open(path: &Path) -> Result<Store, String> {
        if let Some(d) = path.parent() {
            std::fs::create_dir_all(d).map_err(|e| e.to_string())?;
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                let _ = std::fs::set_permissions(d, std::fs::Permissions::from_mode(0o700));
            }
        }
        let conn = Connection::open(path).map_err(|e| format!("open db {}: {e}", path.display()))?;
        conn.busy_timeout(std::time::Duration::from_secs(5)).map_err(|e| e.to_string())?;
        conn.execute_batch(
            "PRAGMA journal_mode=WAL;
             PRAGMA synchronous=FULL;
             PRAGMA foreign_keys=ON;
             CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
             CREATE TABLE IF NOT EXISTS snapshots (
                id TEXT PRIMARY KEY, created_at TEXT NOT NULL, name TEXT, mode TEXT NOT NULL,
                payload TEXT NOT NULL, checksum TEXT NOT NULL, fingerprint TEXT NOT NULL,
                window_count INTEGER NOT NULL, app_count INTEGER NOT NULL, pinned INTEGER NOT NULL DEFAULT 0);
             CREATE INDEX IF NOT EXISTS snapshots_created ON snapshots(created_at);
             CREATE TABLE IF NOT EXISTS layouts (name TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL);
             CREATE TABLE IF NOT EXISTS restore_runs (
                run_id TEXT PRIMARY KEY, source TEXT NOT NULL, source_id TEXT, started_at TEXT NOT NULL,
                finished_at TEXT, state TEXT NOT NULL, summary TEXT);
             CREATE TABLE IF NOT EXISTS restore_items (
                run_id TEXT NOT NULL, record_id TEXT NOT NULL, label TEXT, status TEXT NOT NULL,
                window_id TEXT, launched_at TEXT, attempts INTEGER NOT NULL DEFAULT 0, message TEXT,
                updated_at TEXT NOT NULL, PRIMARY KEY (run_id, record_id));
             CREATE TABLE IF NOT EXISTS launches (
                id INTEGER PRIMARY KEY AUTOINCREMENT, desktop_id TEXT NOT NULL, run_id TEXT,
                launched_at_unix INTEGER NOT NULL, pid INTEGER);
             CREATE TABLE IF NOT EXISTS sessions (
                session_key TEXT PRIMARY KEY, boot_id TEXT NOT NULL, started_at TEXT NOT NULL,
                ended_at TEXT, clean INTEGER NOT NULL DEFAULT 0, restored INTEGER NOT NULL DEFAULT 0);",
        )
        .map_err(|e| format!("init db: {e}"))?;
        let s = Store { conn, path: path.to_path_buf() };
        let v: Option<String> = s.meta_get("schema_version")?;
        match v.and_then(|v| v.parse::<i64>().ok()) {
            None => s.meta_set("schema_version", &DB_SCHEMA_VERSION.to_string())?,
            Some(v) if v > DB_SCHEMA_VERSION => return Err(format!("database schema {v} is newer than this Fractile ({DB_SCHEMA_VERSION}); refusing to downgrade")),
            Some(_) => {}
        }
        Ok(s)
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    pub fn meta_get(&self, key: &str) -> Result<Option<String>, String> {
        self.conn.query_row("SELECT value FROM meta WHERE key=?1", [key], |r| r.get(0)).optional().map_err(|e| e.to_string())
    }
    pub fn meta_set(&self, key: &str, value: &str) -> Result<(), String> {
        self.conn.execute("INSERT INTO meta(key,value) VALUES(?1,?2) ON CONFLICT(key) DO UPDATE SET value=excluded.value", params![key, value]).map(|_| ()).map_err(|e| e.to_string())
    }

    /// Atomically persist a snapshot (single transaction). Validates first.
    pub fn save_snapshot(&mut self, s: &Snapshot, pinned: bool) -> Result<(), String> {
        s.validate()?;
        let payload = s.canonical_json();
        let checksum = s.checksum();
        let fp = s.content_fingerprint();
        let apps: std::collections::HashSet<String> = s.windows.iter().map(|w| w.desktop_file_id.clone().or(w.wm_class.clone()).unwrap_or_default()).collect();
        let tx = self.conn.transaction().map_err(|e| e.to_string())?;
        tx.execute(
            "INSERT OR REPLACE INTO snapshots(id,created_at,name,mode,payload,checksum,fingerprint,window_count,app_count,pinned) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10)",
            params![s.snapshot_id, s.created_at, s.name, mode_str(s.mode), payload, checksum, fp, s.windows.len() as i64, apps.len() as i64, pinned as i64],
        )
        .map_err(|e| e.to_string())?;
        tx.commit().map_err(|e| e.to_string())
    }

    fn row_to_snapshot(payload: String, checksum: String) -> Result<Snapshot, String> {
        let s: Snapshot = serde_json::from_str(&payload).map_err(|e| format!("corrupt snapshot payload: {e}"))?;
        if s.checksum() != checksum {
            return Err("snapshot checksum mismatch".into());
        }
        s.validate()?;
        Ok(s)
    }

    pub fn get_snapshot(&self, id: &str) -> Result<Snapshot, String> {
        let (p, c): (String, String) = self
            .conn
            .query_row("SELECT payload, checksum FROM snapshots WHERE id=?1", [id], |r| Ok((r.get(0)?, r.get(1)?)))
            .optional()
            .map_err(|e| e.to_string())?
            .ok_or_else(|| format!("no snapshot '{id}'"))?;
        Self::row_to_snapshot(p, c)
    }

    /// Resolve an id prefix or a name to a snapshot id.
    pub fn resolve_snapshot_ref(&self, r: &str) -> Result<String, String> {
        if r == "latest" {
            let modes = [SnapshotMode::Manual, SnapshotMode::Periodic, SnapshotMode::Logout, SnapshotMode::Shutdown, SnapshotMode::PreRestore, SnapshotMode::Layout];
            return self.latest_good(Some(&modes)).map(|s| s.snapshot_id).ok_or_else(|| "no valid snapshot".into());
        }
        let mut st = self.conn.prepare("SELECT id FROM snapshots WHERE id LIKE ?1 || '%' OR name=?1 ORDER BY created_at DESC").map_err(|e| e.to_string())?;
        let ids: Vec<String> = st.query_map([r], |row| row.get(0)).map_err(|e| e.to_string())?.filter_map(|x| x.ok()).collect();
        match ids.len() {
            0 => Err(format!("no snapshot matches '{r}'")),
            1 => Ok(ids[0].clone()),
            _ if ids.iter().any(|i| i == r) => Ok(r.to_string()),
            _ => Ok(ids[0].clone()),
        }
    }

    pub fn list_snapshots(&self, limit: usize) -> Result<Vec<SnapshotMeta>, String> {
        let mut st = self
            .conn
            .prepare("SELECT id, created_at, name, mode, payload, checksum, window_count, app_count, pinned FROM snapshots ORDER BY created_at DESC LIMIT ?1")
            .map_err(|e| e.to_string())?;
        let rows = st
            .query_map([limit as i64], |r| {
                let payload: String = r.get(4)?;
                let checksum: String = r.get(5)?;
                let mode: String = r.get(3)?;
                Ok(SnapshotMeta {
                    id: r.get(0)?,
                    created_at: r.get(1)?,
                    name: r.get(2)?,
                    mode: mode_from(&mode),
                    window_count: r.get::<_, i64>(6)? as usize,
                    app_count: r.get::<_, i64>(7)? as usize,
                    checksum_ok: Self::row_to_snapshot(payload, checksum).is_ok(),
                    pinned: r.get::<_, i64>(8)? != 0,
                })
            })
            .map_err(|e| e.to_string())?;
        Ok(rows.filter_map(|r| r.ok()).collect())
    }

    /// Newest snapshot that passes checksum + validation, optionally of
    /// given modes. Corrupt rows are skipped (previous good is used).
    /// The newest snapshot of one of `modes` (any mode when None) whose
    /// payload passes its checksum. The modes are filtered in the query, so
    /// any number of newer snapshots of other modes (manual saves, say) do
    /// not hide it; up to 200 corrupt rows of the wanted modes are skipped.
    pub fn latest_good(&self, modes: Option<&[SnapshotMode]>) -> Option<Snapshot> {
        let names: Vec<String> = modes.map(|ms| ms.iter().map(|m| mode_str(*m)).collect()).unwrap_or_default();
        let filter = if modes.is_some() { format!("WHERE mode IN ({})", vec!["?"; names.len().max(1)].join(",")) } else { String::new() };
        let mut st = self.conn.prepare(&format!("SELECT payload, checksum FROM snapshots {filter} ORDER BY created_at DESC LIMIT 200")).ok()?;
        let args: Vec<&dyn rusqlite::ToSql> = if modes.is_some() && names.is_empty() { vec![&""] } else { names.iter().map(|n| n as &dyn rusqlite::ToSql).collect() };
        let rows = st.query_map(args.as_slice(), |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))).ok()?;
        for (p, c) in rows.flatten() {
            if let Ok(s) = Self::row_to_snapshot(p, c) {
                return Some(s);
            }
        }
        None
    }

    pub fn latest_fingerprint(&self) -> Option<String> {
        self.conn.query_row("SELECT fingerprint FROM snapshots WHERE mode IN ('periodic','logout','shutdown') ORDER BY created_at DESC LIMIT 1", [], |r| r.get(0)).ok()
    }

    pub fn delete_snapshot(&self, id: &str) -> Result<bool, String> {
        self.conn.execute("DELETE FROM snapshots WHERE id=?1", [id]).map(|n| n > 0).map_err(|e| e.to_string())
    }

    pub fn set_pinned(&self, id: &str, pinned: bool) -> Result<(), String> {
        self.conn.execute("UPDATE snapshots SET pinned=?2 WHERE id=?1", params![id, pinned as i64]).map(|_| ()).map_err(|e| e.to_string())
    }

    /// Rolling retention for automatic snapshots: keep the newest `max_recent`,
    /// plus the newest per calendar day for `daily_days` days. Manual, named
    /// and pinned snapshots are never pruned. Returns number deleted.
    pub fn prune(&self, max_recent: usize, daily_days: u32) -> Result<usize, String> {
        let mut st = self
            .conn
            .prepare("SELECT id, created_at FROM snapshots WHERE pinned=0 AND name IS NULL AND mode IN ('periodic','logout','shutdown','pre_restore') ORDER BY created_at DESC")
            .map_err(|e| e.to_string())?;
        let rows: Vec<(String, String)> = st.query_map([], |r| Ok((r.get(0)?, r.get(1)?))).map_err(|e| e.to_string())?.filter_map(|r| r.ok()).collect();
        let now = chrono::Local::now();
        let mut keep_days = std::collections::HashSet::new();
        let mut delete = vec![];
        for (i, (id, created)) in rows.iter().enumerate() {
            if i < max_recent {
                continue;
            }
            let day = created.get(0..10).unwrap_or("").to_string();
            let age_days = chrono::DateTime::parse_from_rfc3339(created).map(|t| (now.signed_duration_since(t)).num_days()).unwrap_or(i64::MAX);
            if age_days <= daily_days as i64 && keep_days.insert(day) {
                continue;
            }
            delete.push(id.clone());
        }
        for id in &delete {
            self.conn.execute("DELETE FROM snapshots WHERE id=?1", [id]).map_err(|e| e.to_string())?;
        }
        Ok(delete.len())
    }

    /// Online backup of the whole database to `dest` (crash-consistent).
    pub fn backup_to(&self, dest: &Path) -> Result<(), String> {
        let tmp = dest.with_extension("tmp");
        let _ = std::fs::remove_file(&tmp);
        {
            let mut dst = Connection::open(&tmp).map_err(|e| e.to_string())?;
            let b = rusqlite::backup::Backup::new(&self.conn, &mut dst).map_err(|e| e.to_string())?;
            b.run_to_completion(64, std::time::Duration::from_millis(5), None).map_err(|e| e.to_string())?;
        }
        std::fs::rename(&tmp, dest).map_err(|e| e.to_string())
    }

    // ---- layouts ----
    pub fn save_layout(&self, l: &Layout) -> Result<(), String> {
        l.validate()?;
        let payload = serde_json::to_string(l).map_err(|e| e.to_string())?;
        self.conn
            .execute("INSERT INTO layouts(name,payload,updated_at) VALUES(?1,?2,?3) ON CONFLICT(name) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at", params![l.name, payload, chrono::Local::now().to_rfc3339()])
            .map(|_| ())
            .map_err(|e| e.to_string())
    }
    pub fn get_layout(&self, name: &str) -> Result<Layout, String> {
        let p: String = self.conn.query_row("SELECT payload FROM layouts WHERE name=?1", [name], |r| r.get(0)).optional().map_err(|e| e.to_string())?.ok_or_else(|| format!("no layout '{name}'"))?;
        serde_json::from_str(&p).map_err(|e| format!("corrupt layout: {e}"))
    }
    pub fn list_layouts(&self) -> Result<Vec<Layout>, String> {
        let mut st = self.conn.prepare("SELECT payload FROM layouts ORDER BY name").map_err(|e| e.to_string())?;
        let v = st.query_map([], |r| r.get::<_, String>(0)).map_err(|e| e.to_string())?.filter_map(|r| r.ok()).filter_map(|p| serde_json::from_str(&p).ok()).collect();
        Ok(v)
    }
    pub fn delete_layout(&self, name: &str) -> Result<bool, String> {
        self.conn.execute("DELETE FROM layouts WHERE name=?1", [name]).map(|n| n > 0).map_err(|e| e.to_string())
    }

    // ---- restore transaction log ----
    pub fn run_begin(&self, run_id: &str, source: &str, source_id: Option<&str>) -> Result<(), String> {
        self.conn
            .execute("INSERT OR IGNORE INTO restore_runs(run_id,source,source_id,started_at,state) VALUES(?1,?2,?3,?4,'running')", params![run_id, source, source_id, chrono::Local::now().to_rfc3339()])
            .map(|_| ())
            .map_err(|e| e.to_string())?;
        self.conn.execute("UPDATE restore_runs SET state='running', finished_at=NULL WHERE run_id=?1", [run_id]).map(|_| ()).map_err(|e| e.to_string())
    }
    pub fn run_finish(&self, run_id: &str, state: &str, summary: &str) -> Result<(), String> {
        self.conn
            .execute("UPDATE restore_runs SET state=?2, finished_at=?3, summary=?4 WHERE run_id=?1", params![run_id, state, chrono::Local::now().to_rfc3339(), summary])
            .map(|_| ())
            .map_err(|e| e.to_string())
    }
    pub fn run_get(&self, run_id: &str) -> Result<Option<RunRow>, String> {
        self.conn
            .query_row("SELECT source, source_id, state, summary FROM restore_runs WHERE run_id=?1", [run_id], |r| {
                Ok(RunRow { source: r.get(0)?, source_id: r.get(1)?, state: r.get(2)?, summary: r.get(3)? })
            })
            .optional()
            .map_err(|e| e.to_string())
    }
    /// Runs left in 'running' state (daemon crashed mid-restore).
    pub fn interrupted_runs(&self) -> Result<Vec<String>, String> {
        let mut st = self.conn.prepare("SELECT run_id FROM restore_runs WHERE state='running' ORDER BY started_at DESC").map_err(|e| e.to_string())?;
        let v = st.query_map([], |r| r.get(0)).map_err(|e| e.to_string())?.filter_map(|r| r.ok()).collect();
        Ok(v)
    }
    pub fn recent_runs(&self, limit: usize) -> Result<Vec<serde_json::Value>, String> {
        let mut st = self.conn.prepare("SELECT run_id, source, source_id, started_at, finished_at, state, summary FROM restore_runs ORDER BY started_at DESC LIMIT ?1").map_err(|e| e.to_string())?;
        let v = st
            .query_map([limit as i64], |r| {
                Ok(serde_json::json!({
                    "run_id": r.get::<_, String>(0)?, "source": r.get::<_, String>(1)?, "source_id": r.get::<_, Option<String>>(2)?,
                    "started_at": r.get::<_, String>(3)?, "finished_at": r.get::<_, Option<String>>(4)?, "state": r.get::<_, String>(5)?,
                    "summary": r.get::<_, Option<String>>(6)?,
                }))
            })
            .map_err(|e| e.to_string())?
            .filter_map(|r| r.ok())
            .collect();
        Ok(v)
    }
    pub fn item_upsert(&self, run_id: &str, item: &ItemResult) -> Result<(), String> {
        let now = chrono::Local::now().to_rfc3339();
        self.conn
            .execute(
                "INSERT INTO restore_items(run_id,record_id,label,status,window_id,launched_at,attempts,message,updated_at)
                 VALUES(?1,?2,?3,?4,?5,CASE WHEN ?6 THEN ?8 ELSE NULL END,1,?7,?8)
                 ON CONFLICT(run_id,record_id) DO UPDATE SET status=excluded.status, window_id=COALESCE(excluded.window_id, window_id),
                   launched_at=COALESCE(launched_at, excluded.launched_at), attempts=attempts+1, message=excluded.message, updated_at=excluded.updated_at",
                params![run_id, item.record_id, item.label, item.status.as_str(), item.window_id, item.launched, item.message, now],
            )
            .map(|_| ())
            .map_err(|e| e.to_string())
    }
    pub fn items(&self, run_id: &str) -> Result<Vec<ItemRow>, String> {
        let mut st = self.conn.prepare("SELECT record_id, status, window_id, launched_at, label, message FROM restore_items WHERE run_id=?1 ORDER BY rowid").map_err(|e| e.to_string())?;
        let v = st.query_map([run_id], |r| {
            Ok(ItemRow { record_id: r.get(0)?, status: r.get(1)?, window_id: r.get(2)?, launched_at: r.get(3)?, label: r.get(4)?, message: r.get(5)? })
        });
        let v = v.map_err(|e| e.to_string())?.filter_map(|r| r.ok()).collect();
        Ok(v)
    }

    // ---- launch ledger (duplicate-launch prevention across restarts) ----
    pub fn record_launch(&self, desktop_id: &str, run_id: Option<&str>, pid: Option<u32>) -> Result<(), String> {
        self.conn
            .execute("INSERT INTO launches(desktop_id,run_id,launched_at_unix,pid) VALUES(?1,?2,?3,?4)", params![desktop_id, run_id, chrono::Utc::now().timestamp(), pid.map(|p| p as i64)])
            .map(|_| ())
            .map_err(|e| e.to_string())?;
        let _ = self.conn.execute("DELETE FROM launches WHERE launched_at_unix < ?1", [chrono::Utc::now().timestamp() - 86400]);
        Ok(())
    }
    pub fn launches_since(&self, desktop_id: &str, seconds: i64) -> Result<u32, String> {
        self.conn
            .query_row("SELECT COUNT(*) FROM launches WHERE desktop_id=?1 AND launched_at_unix >= ?2", params![desktop_id, chrono::Utc::now().timestamp() - seconds], |r| r.get::<_, i64>(0))
            .map(|n| n as u32)
            .map_err(|e| e.to_string())
    }

    // ---- sessions (clean/unclean shutdown detection) ----
    pub fn session_start(&self, key: &str, boot_id: &str) -> Result<bool, String> {
        let n = self
            .conn
            .execute("INSERT OR IGNORE INTO sessions(session_key,boot_id,started_at) VALUES(?1,?2,?3)", params![key, boot_id, chrono::Local::now().to_rfc3339()])
            .map_err(|e| e.to_string())?;
        Ok(n > 0)
    }
    pub fn session_end(&self, key: &str, clean: bool) -> Result<(), String> {
        self.conn
            .execute("UPDATE sessions SET ended_at=?2, clean=?3 WHERE session_key=?1", params![key, chrono::Local::now().to_rfc3339(), clean as i64])
            .map(|_| ())
            .map_err(|e| e.to_string())
    }
    pub fn session_mark_restored(&self, key: &str) -> Result<(), String> {
        self.conn.execute("UPDATE sessions SET restored=1 WHERE session_key=?1", [key]).map(|_| ()).map_err(|e| e.to_string())
    }
    pub fn session_get(&self, key: &str) -> Result<Option<SessionRecord>, String> {
        self.conn
            .query_row("SELECT session_key, boot_id, started_at, ended_at, clean, restored FROM sessions WHERE session_key=?1", [key], |r| {
                Ok(SessionRecord { session_key: r.get(0)?, boot_id: r.get(1)?, started_at: r.get(2)?, ended_at: r.get(3)?, clean: r.get::<_, i64>(4)? != 0, restored: r.get::<_, i64>(5)? != 0 })
            })
            .optional()
            .map_err(|e| e.to_string())
    }
    /// Most recent session other than `current`.
    pub fn previous_session(&self, current: &str) -> Result<Option<SessionRecord>, String> {
        self.conn
            .query_row("SELECT session_key, boot_id, started_at, ended_at, clean, restored FROM sessions WHERE session_key<>?1 ORDER BY started_at DESC LIMIT 1", [current], |r| {
                Ok(SessionRecord { session_key: r.get(0)?, boot_id: r.get(1)?, started_at: r.get(2)?, ended_at: r.get(3)?, clean: r.get::<_, i64>(4)? != 0, restored: r.get::<_, i64>(5)? != 0 })
            })
            .optional()
            .map_err(|e| e.to_string())
    }
    pub fn consecutive_failed_auto_runs(&self) -> u32 {
        let mut st = match self.conn.prepare("SELECT state FROM restore_runs WHERE source='login' ORDER BY started_at DESC LIMIT 20") {
            Ok(s) => s,
            Err(_) => return 0,
        };
        let states: Vec<String> = st.query_map([], |r| r.get(0)).map(|it| it.filter_map(|r| r.ok()).collect()).unwrap_or_default();
        states.iter().take_while(|s| s.as_str() == "failed" || s.as_str() == "running").count() as u32
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::restore::ItemStatus;
    use fractile_core::geometry::Rect;
    use fractile_core::snapshot::WindowRecord;

    fn tmpdb() -> (Store, PathBuf) {
        let d = std::env::temp_dir().join(format!("fractile-store-{}", uuid::Uuid::new_v4()));
        let s = Store::open(&d.join("t.db")).unwrap();
        (s, d)
    }

    fn snap(mode: SnapshotMode, created: &str) -> Snapshot {
        let mut s = Snapshot::new_empty(mode);
        s.created_at = created.into();
        s.windows.push(WindowRecord { record_id: "r".into(), desktop_file_id: Some("a.desktop".into()), normalized_geometry: [0.0, 0.0, 1.0, 1.0], absolute_geometry: Rect::new(0, 0, 10, 10), ..Default::default() });
        s
    }

    #[test]
    fn latest_of_a_mode_is_found_behind_many_newer_manual_saves() {
        // Seen in the VM power-cut test: 542 manual saves after the last
        // periodic snapshot hid it from the login restore.
        let (mut st, d) = tmpdb();
        let p = snap(SnapshotMode::Periodic, "2026-11-10T10:00:00+00:00");
        st.save_snapshot(&p, false).unwrap();
        for k in 0..300 {
            st.save_snapshot(&snap(SnapshotMode::Manual, &format!("2026-11-10T10:{:02}:{:02}+00:00", 1 + k / 60, k % 60)), false).unwrap();
        }
        let got = st.latest_good(Some(&[SnapshotMode::Logout, SnapshotMode::Shutdown, SnapshotMode::Periodic])).unwrap();
        assert_eq!(got.snapshot_id, p.snapshot_id);
        assert_eq!(st.latest_good(None).unwrap().mode, SnapshotMode::Manual);
        assert!(st.latest_good(Some(&[SnapshotMode::Logout])).is_none());
        std::fs::remove_dir_all(d).unwrap();
    }

    #[test]
    fn corrupt_latest_falls_back_to_previous_good() {
        let (mut st, d) = tmpdb();
        let a = snap(SnapshotMode::Periodic, "2026-11-10T10:00:00+00:00");
        let b = snap(SnapshotMode::Periodic, "2026-11-10T11:00:00+00:00");
        st.save_snapshot(&a, false).unwrap();
        st.save_snapshot(&b, false).unwrap();
        assert_eq!(st.latest_good(None).unwrap().snapshot_id, b.snapshot_id);
        // simulate torn/corrupted write of the newest snapshot
        st.conn.execute("UPDATE snapshots SET payload=substr(payload,1,40) WHERE id=?1", [&b.snapshot_id]).unwrap();
        assert_eq!(st.latest_good(None).unwrap().snapshot_id, a.snapshot_id);
        let metas = st.list_snapshots(10).unwrap();
        assert!(!metas.iter().find(|m| m.id == b.snapshot_id).unwrap().checksum_ok);
        std::fs::remove_dir_all(d).unwrap();
    }

    #[test]
    fn retention_keeps_recent_daily_and_manual() {
        let (mut st, d) = tmpdb();
        let now = chrono::Local::now();
        for i in 0..50 {
            let t = (now - chrono::Duration::hours(i * 6)).to_rfc3339();
            st.save_snapshot(&snap(SnapshotMode::Periodic, &t), false).unwrap();
        }
        let mut m = snap(SnapshotMode::Manual, &(now - chrono::Duration::days(60)).to_rfc3339());
        m.name = Some("keep me".into());
        st.save_snapshot(&m, false).unwrap();
        let deleted = st.prune(10, 7).unwrap();
        let left = st.list_snapshots(500).unwrap();
        assert!(deleted > 0);
        assert!(left.iter().any(|x| x.name.as_deref() == Some("keep me")));
        // 10 recent + at most one per day for 7 days beyond those
        let auto = left.iter().filter(|x| x.mode == SnapshotMode::Periodic).count();
        assert!((10..=10 + 8).contains(&auto), "auto={auto}");
        std::fs::remove_dir_all(d).unwrap();
    }

    /// The 24 h soak (T27): a restore every few seconds writes a
    /// pre-restore snapshot each time, and the run crosses midnight. What
    /// stays is the newest `max_recent`, plus the newest of each calendar
    /// day beyond those: 20 + 2.
    #[test]
    fn retention_across_midnight_keeps_one_extra_per_day() {
        use chrono::TimeZone;
        let (mut st, d) = tmpdb();
        let midnight = chrono::Local.from_local_datetime(&chrono::Local::now().date_naive().and_hms_opt(0, 0, 0).unwrap()).earliest().unwrap();
        for k in 0..1440 {
            let t = (midnight + chrono::Duration::minutes(60 - k)).to_rfc3339();
            st.save_snapshot(&snap(SnapshotMode::PreRestore, &t), false).unwrap();
        }
        st.prune(20, 7).unwrap();
        let left = st.list_snapshots(500).unwrap();
        let days: std::collections::BTreeSet<&str> = left.iter().map(|x| &x.created_at[0..10]).collect();
        assert_eq!(left.len(), 22, "20 newest + the newest of each of the 2 days beyond them");
        assert_eq!(days.len(), 2);
        std::fs::remove_dir_all(d).unwrap();
    }

    #[test]
    fn transaction_log_and_ledger() {
        let (st, d) = tmpdb();
        st.run_begin("run1", "manual", Some("s")).unwrap();
        let mut item = ItemResult { record_id: "r1".into(), label: "xterm".into(), status: ItemStatus::Launching, window_id: None, message: None, requested: None, observed: None, launched: true, excluded: false };
        st.item_upsert("run1", &item).unwrap();
        item.status = ItemStatus::LaunchedAndPlaced;
        item.window_id = Some("x11:0x1".into());
        item.launched = false;
        st.item_upsert("run1", &item).unwrap();
        let items = st.items("run1").unwrap();
        assert_eq!(items[0].status, "LAUNCHED_AND_PLACED");
        assert!(items[0].launched_at.is_some(), "launched_at preserved");
        assert_eq!(st.interrupted_runs().unwrap(), vec!["run1"]);
        st.run_finish("run1", "completed", "ok").unwrap();
        assert!(st.interrupted_runs().unwrap().is_empty());
        st.record_launch("xterm.desktop", Some("run1"), Some(1)).unwrap();
        assert_eq!(st.launches_since("xterm.desktop", 60).unwrap(), 1);
        std::fs::remove_dir_all(d).unwrap();
    }

    #[test]
    fn sessions_detect_unclean() {
        let (st, d) = tmpdb();
        assert!(st.session_start("s1", "b1").unwrap());
        assert!(!st.session_start("s1", "b1").unwrap());
        st.session_start("s2", "b2").unwrap();
        let prev = st.previous_session("s2").unwrap().unwrap();
        assert_eq!(prev.session_key, "s1");
        assert!(!prev.clean);
        std::fs::remove_dir_all(d).unwrap();
    }

    #[test]
    fn refuses_newer_schema() {
        let (st, d) = tmpdb();
        st.meta_set("schema_version", "99").unwrap();
        let p = st.path().to_path_buf();
        drop(st);
        assert!(Store::open(&p).is_err());
        std::fs::remove_dir_all(d).unwrap();
    }
}
