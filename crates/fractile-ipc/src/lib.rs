//! Fractile local IPC.
//!
//! Transport: UNIX stream socket in a 0700 runtime directory, socket mode
//! 0600, and every accepted connection is checked with SO_PEERCRED: only
//! the owning user's processes are served. Framing: one JSON object per
//! line, max 1 MiB. The first request on a connection must be `hello`
//! carrying the protocol version.

use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::io::{BufRead, BufReader, Read, Write};
use std::os::unix::fs::PermissionsExt;
use std::os::unix::io::AsRawFd;
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::Path;
use std::time::Duration;

pub const PROTOCOL_VERSION: u32 = 1;
pub const MAX_LINE: usize = 1 << 20;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Request {
    pub id: u64,
    pub method: String,
    #[serde(default)]
    pub params: Value,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct RpcError {
    pub code: String,
    pub message: String,
}

impl RpcError {
    pub fn new(code: &str, message: impl Into<String>) -> RpcError {
        RpcError { code: code.into(), message: message.into() }
    }
}

impl std::fmt::Display for RpcError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.code, self.message)
    }
}

impl std::error::Error for RpcError {}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Response {
    pub id: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub result: Option<Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub error: Option<RpcError>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EventMsg {
    pub event: String,
    #[serde(default)]
    pub data: Value,
}

/// Anything the server can write to a client.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(untagged)]
pub enum ServerMsg {
    Response(Response),
    Event(EventMsg),
}

/// Peer credentials of a connected socket.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PeerCred {
    pub pid: i32,
    pub uid: u32,
    pub gid: u32,
}

pub fn peer_cred(s: &UnixStream) -> std::io::Result<PeerCred> {
    let mut cred = libc::ucred { pid: 0, uid: 0, gid: 0 };
    let mut len = std::mem::size_of::<libc::ucred>() as libc::socklen_t;
    // SAFETY: valid fd, correctly sized out-pointer.
    let r = unsafe { libc::getsockopt(s.as_raw_fd(), libc::SOL_SOCKET, libc::SO_PEERCRED, &mut cred as *mut _ as *mut libc::c_void, &mut len) };
    if r != 0 {
        return Err(std::io::Error::last_os_error());
    }
    Ok(PeerCred { pid: cred.pid, uid: cred.uid, gid: cred.gid })
}

pub fn current_uid() -> u32 {
    // SAFETY: geteuid has no preconditions.
    unsafe { libc::geteuid() }
}

/// Create the private runtime dir (0700, owned by us) and bind a 0600 socket.
/// Refuses to use a directory owned by someone else or with group/other access.
pub fn secure_bind(path: &Path) -> std::io::Result<UnixListener> {
    let dir = path.parent().ok_or_else(|| std::io::Error::other("socket path has no parent"))?;
    std::fs::create_dir_all(dir)?;
    std::fs::set_permissions(dir, std::fs::Permissions::from_mode(0o700))?;
    let meta = std::fs::metadata(dir)?;
    use std::os::unix::fs::MetadataExt;
    if meta.uid() != current_uid() {
        return Err(std::io::Error::other(format!("runtime dir {} not owned by current user", dir.display())));
    }
    if meta.mode() & 0o077 != 0 {
        return Err(std::io::Error::other("runtime dir permissions too open"));
    }
    if path.exists() {
        // Stale socket? Only remove if nobody answers.
        if UnixStream::connect(path).is_ok() {
            return Err(std::io::Error::new(std::io::ErrorKind::AddrInUse, "another instance is listening"));
        }
        std::fs::remove_file(path)?;
    }
    // umask-independent: bind then chmod before any accept happens.
    let l = UnixListener::bind(path)?;
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600))?;
    Ok(l)
}

/// Read one bounded line. Returns Ok(None) on EOF.
pub fn read_line<R: BufRead>(r: &mut R) -> std::io::Result<Option<String>> {
    let mut buf = Vec::new();
    let mut limited = r.take(MAX_LINE as u64 + 1);
    let n = limited.read_until(b'\n', &mut buf)?;
    if n == 0 {
        return Ok(None);
    }
    if buf.len() > MAX_LINE {
        return Err(std::io::Error::new(std::io::ErrorKind::InvalidData, "message too large"));
    }
    while buf.last() == Some(&b'\n') || buf.last() == Some(&b'\r') {
        buf.pop();
    }
    String::from_utf8(buf).map(Some).map_err(|_| std::io::Error::new(std::io::ErrorKind::InvalidData, "non-UTF-8 message"))
}

pub fn write_msg<W: Write, T: Serialize>(w: &mut W, msg: &T) -> std::io::Result<()> {
    let mut s = serde_json::to_string(msg).map_err(std::io::Error::other)?;
    s.push('\n');
    w.write_all(s.as_bytes())?;
    w.flush()
}

/// Blocking client. Events received while waiting for a response are queued.
pub struct Client {
    reader: BufReader<UnixStream>,
    writer: UnixStream,
    next_id: u64,
    pub pending_events: Vec<EventMsg>,
    pub server_info: Value,
}

impl Client {
    pub fn connect(path: &Path, client_name: &str) -> Result<Client, RpcError> {
        let s = UnixStream::connect(path).map_err(|e| RpcError::new("not_running", format!("cannot connect to Fractile service at {}: {e}", path.display())))?;
        let _ = s.set_read_timeout(Some(Duration::from_secs(120)));
        let writer = s.try_clone().map_err(|e| RpcError::new("io", e.to_string()))?;
        let mut c = Client { reader: BufReader::new(s), writer, next_id: 1, pending_events: vec![], server_info: Value::Null };
        let info = c.call("hello", serde_json::json!({ "protocol": PROTOCOL_VERSION, "client": client_name }))?;
        c.server_info = info;
        Ok(c)
    }

    pub fn set_timeout(&self, d: Option<Duration>) {
        let _ = self.reader.get_ref().set_read_timeout(d);
    }

    pub fn call(&mut self, method: &str, params: Value) -> Result<Value, RpcError> {
        let id = self.next_id;
        self.next_id += 1;
        write_msg(&mut self.writer, &Request { id, method: method.into(), params }).map_err(|e| RpcError::new("io", e.to_string()))?;
        loop {
            let line = read_line(&mut self.reader).map_err(|e| RpcError::new("io", e.to_string()))?.ok_or_else(|| RpcError::new("io", "connection closed"))?;
            let msg: ServerMsg = serde_json::from_str(&line).map_err(|e| RpcError::new("protocol", e.to_string()))?;
            match msg {
                ServerMsg::Response(r) if r.id == id => {
                    if let Some(e) = r.error {
                        return Err(e);
                    }
                    return Ok(r.result.unwrap_or(Value::Null));
                }
                ServerMsg::Response(_) => continue,
                ServerMsg::Event(e) => self.pending_events.push(e),
            }
        }
    }

    /// Wait for the next event (after subscribing).
    pub fn next_event(&mut self) -> Result<EventMsg, RpcError> {
        if !self.pending_events.is_empty() {
            return Ok(self.pending_events.remove(0));
        }
        loop {
            let line = read_line(&mut self.reader).map_err(|e| RpcError::new("io", e.to_string()))?.ok_or_else(|| RpcError::new("io", "connection closed"))?;
            if let Ok(ServerMsg::Event(e)) = serde_json::from_str::<ServerMsg>(&line) {
                return Ok(e);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bind_is_private_and_peercred_works() {
        let d = std::env::temp_dir().join(format!("fractile-ipc-test-{}", std::process::id()));
        let p = d.join("s.sock");
        let l = secure_bind(&p).unwrap();
        let mode = std::fs::metadata(&p).unwrap().permissions().mode() & 0o777;
        assert_eq!(mode, 0o600);
        let dmode = std::fs::metadata(&d).unwrap().permissions().mode() & 0o777;
        assert_eq!(dmode, 0o700);
        let c = UnixStream::connect(&p).unwrap();
        let (s, _) = l.accept().unwrap();
        let cred = peer_cred(&s).unwrap();
        assert_eq!(cred.uid, current_uid());
        assert_eq!(cred.pid as u32, std::process::id());
        // second bind refused while live
        assert!(secure_bind(&p).is_err());
        drop(c);
        drop(l);
        std::fs::remove_dir_all(d).unwrap();
    }

    #[test]
    fn line_limit_enforced() {
        let big = vec![b'a'; MAX_LINE + 10];
        let mut r = std::io::Cursor::new(big);
        assert!(read_line(&mut r).is_err());
        let mut r = std::io::Cursor::new(b"{\"a\":1}\n".to_vec());
        assert_eq!(read_line(&mut r).unwrap().unwrap(), "{\"a\":1}");
    }
}
