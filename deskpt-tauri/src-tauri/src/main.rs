// deskpt-tauri — Tauri/Rust rewrite of the desk pet SHELL, replicating the
// Electron original's window model and UI (original UI files are served
// verbatim; this binary only replaces the Electron main process).
//
// Window model (same as the original):
//   pet-render — the sprite (cybercat GIFs), click-through
//   pet-hit    — input surface over the sprite hitbox (drag/click/menu)
//   chat       — the ORIGINAL minicpm-chat.html bubble (full replica)
//   menu       — the ORIGINAL context-menu.html styling
//
// The BRAIN stays the original Python gateway (sidecar), sharing the same
// memory/mood/events — and the same chat-history.json as the Electron pet.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

#[cfg(windows)]
mod virtual_desktop;

use std::io::{Read, Write};
use std::net::TcpStream;
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::time::Duration;

use tauri::{Manager, PhysicalPosition};

const GW_HOST: &str = "127.0.0.1";
const GW_PORT: u16 = 18765;

#[cfg(windows)]
use std::os::windows::process::CommandExt;
#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

/// Raw HTTP over TcpStream. HTTP/1.0 on purpose: h11 (uvicorn) replies
/// without chunked encoding and closes the connection, so read_to_end
/// gives us the whole body with zero chunk-decoding code.
fn http_request(
    method: &str,
    path: &str,
    body: Option<&str>,
    token: &str,
    timeout_ms: u64,
) -> Result<(u16, String), String> {
    let mut stream =
        TcpStream::connect((GW_HOST, GW_PORT)).map_err(|e| format!("connect: {e}"))?;
    stream
        .set_read_timeout(Some(Duration::from_millis(timeout_ms)))
        .map_err(|e| e.to_string())?;
    stream
        .set_write_timeout(Some(Duration::from_millis(timeout_ms)))
        .map_err(|e| e.to_string())?;
    let body = body.unwrap_or("");
    let req = format!(
        "{method} {path} HTTP/1.0\r\nHost: {GW_HOST}:{GW_PORT}\r\n\
         content-type: application/json\r\n\
         x-minicpm-token: {token}\r\n\
         content-length: {}\r\n\r\n{body}",
        body.len()
    );
    stream.write_all(req.as_bytes()).map_err(|e| e.to_string())?;
    let mut buf = Vec::new();
    stream.read_to_end(&mut buf).map_err(|e| e.to_string())?;
    let text = String::from_utf8_lossy(&buf).to_string();
    let status = text
        .split_whitespace()
        .nth(1)
        .and_then(|s| s.parse::<u16>().ok())
        .unwrap_or(0);
    let resp_body = text.split("\r\n\r\n").nth(1).unwrap_or("").to_string();
    Ok((status, resp_body))
}

/// B-5 auth: the gateway generates ~/.minicpm/gateway-token at boot.
fn gateway_token() -> String {
    std::env::var("USERPROFILE")
        .map(|home| PathBuf::from(home).join(".minicpm").join("gateway-token"))
        .ok()
        .and_then(|p| std::fs::read_to_string(p).ok())
        .map(|s| s.trim().to_string())
        .unwrap_or_default()
}

/// Where the original project's gateway lives. Priority: DESKPT_GATEWAY_DIR
/// env → <exe>/../../../../minicpm-sidecar (dev layout) → hard-coded sibling.
fn gateway_dir() -> PathBuf {
    if let Ok(d) = std::env::var("DESKPT_GATEWAY_DIR") {
        let p = PathBuf::from(d);
        if p.join("gateway").is_dir() {
            return p;
        }
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(root) = exe.ancestors().nth(5) {
            let cand = root.join("minicpm-sidecar");
            if cand.join("gateway").is_dir() {
                return cand;
            }
        }
    }
    PathBuf::from("D:\\MiniCPM-Desk-Pet-0.10.0\\minicpm-sidecar")
}

fn spawn_sidecar() -> Result<(), String> {
    let dir = gateway_dir();
    let python = dir.join(".venv").join("Scripts").join("python.exe");
    if !python.is_file() {
        return Err(format!("gateway venv python not found: {}", python.display()));
    }
    // Share the SAME brain files as the Electron original (its userData
    // memories dir). Overridable via DESKPT_MEMORY_DIR.
    let memory_dir = std::env::var("DESKPT_MEMORY_DIR").unwrap_or_else(|_| {
        std::env::var("APPDATA")
            .map(|a| {
                PathBuf::from(a)
                    .join("deskpt")
                    .join("memories")
                    .display()
                    .to_string()
            })
            .unwrap_or_default()
    });
    let theme = std::env::var("DESKPT_THEME").unwrap_or_else(|_| "cybercat".to_string());
    // LoRA adapter dir + real log FILES (Settings → 打开日志目录 / Upload
    // LoRA need both; the Electron host pointed the gateway the same way)
    let adapter_dir = app_data_dir().join("adapters");
    let log_dir = app_data_dir().join("logs");
    let _ = std::fs::create_dir_all(&adapter_dir);
    let _ = std::fs::create_dir_all(&log_dir);
    let mut cmd = Command::new(&python);
    cmd.args(["-m", "gateway", "--port", &GW_PORT.to_string(), "--host", GW_HOST])
        .current_dir(&dir)
        .env("MINICPM_THEME", &theme)
        .env("MINICPM_ADAPTER_DIR", &adapter_dir);
    // real log files (append) instead of discarded output — the settings
    // UI's 打开日志目录 opens this folder.
    let log_open = |name: &str| -> Option<std::fs::File> {
        std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(log_dir.join(name))
            .ok()
    };
    if let Some(f) = log_open("gateway.log") {
        cmd.stdout(Stdio::from(f));
    } else {
        cmd.stdout(Stdio::null());
    }
    if let Some(f) = log_open("gateway.err.log") {
        cmd.stderr(Stdio::from(f));
    } else {
        cmd.stderr(Stdio::null());
    }
    if !memory_dir.is_empty() {
        cmd.env("MINICPM_MEMORY_DIR", &memory_dir);
    }
    #[cfg(windows)]
    cmd.creation_flags(CREATE_NO_WINDOW);
    cmd.spawn().map_err(|e| format!("spawn gateway: {e}"))?;
    Ok(())
}

fn health_ok(token: &str) -> bool {
    matches!(
        http_request("GET", "/api/health", None, token, 1500),
        Ok((200, _))
    )
}

#[tauri::command]
async fn ensure_gateway() -> Result<serde_json::Value, String> {
    let token = gateway_token();
    if health_ok(&token) {
        return Ok(serde_json::json!({"ok": true, "spawned": false}));
    }
    spawn_sidecar()?;
    for _ in 0..90 {
        std::thread::sleep(Duration::from_millis(500));
        if health_ok(&token) {
            return Ok(serde_json::json!({"ok": true, "spawned": true}));
        }
    }
    Ok(serde_json::json!({"ok": false, "spawned": true}))
}

/// The original start() contract: gateway up AND local model loaded
/// (ensureSidecarReady → loadModel). Resolution order mirrors the
/// Electron shell: minicpm-prefs.json model_dir → DESKPT_MODEL_DIR env →
/// <repo>/models/*.gguf (first match; a dir path is scanned for a gguf).
fn resolve_model_path() -> Option<PathBuf> {
    // 1. prefs file written by the Electron Settings UI
    if let Ok(appdata) = std::env::var("APPDATA") {
        let prefs = PathBuf::from(&appdata).join("deskpt").join("minicpm-prefs.json");
        if let Ok(raw) = std::fs::read_to_string(&prefs) {
            if let Ok(v) = serde_json::from_str::<serde_json::Value>(&raw) {
                if let Some(dir) = v.get("model_dir").and_then(|d| d.as_str()) {
                    let p = PathBuf::from(dir);
                    if p.is_file() {
                        return Some(p);
                    }
                    if p.is_dir() {
                        if let Some(g) = first_gguf_in(&p) {
                            return Some(g);
                        }
                    }
                }
            }
        }
    }
    // 2. env override
    if let Ok(d) = std::env::var("DESKPT_MODEL_DIR") {
        let p = PathBuf::from(d);
        if p.is_file() {
            return Some(p);
        }
        if p.is_dir() {
            if let Some(g) = first_gguf_in(&p) {
                return Some(g);
            }
        }
    }
    // 3. dev repo models/ (exe → target → src-tauri → deskpt-tauri → root)
    if let Ok(exe) = std::env::current_exe() {
        if let Some(root) = exe.ancestors().nth(5) {
            let models = root.join("models");
            if let Some(g) = first_gguf_in(&models) {
                return Some(g);
            }
        }
    }
    None
}

fn first_gguf_in(dir: &PathBuf) -> Option<PathBuf> {
    let mut entries: Vec<_> = std::fs::read_dir(dir)
        .ok()?
        .filter_map(|e| e.ok())
        .map(|e| e.path())
        .filter(|p| p.is_file() && p.extension().map(|e| e == "gguf").unwrap_or(false))
        .collect();
    entries.sort();
    entries.into_iter().next()
}

/// chat boot: gateway health + spawn + local-model load (may take minutes
/// on a cold start — the original shows a starting spinner meanwhile).
/// The whole blocking body runs in spawn_blocking: http_request is a
/// BLOCKING TcpStream read (up to 10 min for a cold model load) and must
/// never occupy a tokio worker or the async command surface starves.
#[tauri::command]
async fn chat_start() -> Result<serde_json::Value, String> {
    tauri::async_runtime::spawn_blocking(|| {
        let token = gateway_token();
        // gateway up?
        if !health_ok(&token) {
            spawn_sidecar()?;
            let mut up = false;
            for _ in 0..120 {
                std::thread::sleep(Duration::from_millis(500));
                if health_ok(&token) {
                    up = true;
                    break;
                }
            }
            if !up {
                return Ok(serde_json::json!({"ok": false, "error": "gateway not healthy after spawn"}));
            }
        }
        // model loaded?
        let (_, health_body) = http_request("GET", "/api/health", None, &token, 3000)
            .map_err(|e| e.to_string())?;
        let alive = serde_json::from_str::<serde_json::Value>(&health_body)
            .ok()
            .and_then(|v| v.get("alive").and_then(|a| a.as_bool()))
            .unwrap_or(false);
        if alive {
            return Ok(serde_json::json!({"ok": true, "url": format!("http://{GW_HOST}:{GW_PORT}")}));
        }
        let model = resolve_model_path()
            .ok_or_else(|| "no local model found (set DESKPT_MODEL_DIR)".to_string())?;
        let body = serde_json::json!({ "path": model.display().to_string() }).to_string();
        // 10 minutes: cold model load is slow (same budget as the Electron host).
        let (status, resp) = http_request("POST", "/api/load-model", Some(&body), &token, 600_000)
            .map_err(|e| e.to_string())?;
        if status == 200 {
            return Ok(serde_json::json!({"ok": true, "url": format!("http://{GW_HOST}:{GW_PORT}")}));
        }
        Ok(serde_json::json!({"ok": false, "error": format!("load-model {status}: {resp}")}))
    })
    .await
    .map_err(|e| format!("join: {e}"))?
}

#[tauri::command]
fn gateway_info() -> serde_json::Value {
    serde_json::json!({
        "base": format!("http://{GW_HOST}:{GW_PORT}"),
        "token": gateway_token(),
    })
}

/// 身体感受: forward a body interaction to the original gateway
/// (fire-and-forget — a failed report must never break dragging).
#[tauri::command]
fn report_touch(app: tauri::AppHandle, kind: String, clicks: i64, distance_px: i64, duration_ms: i64) {
    use tauri::Emitter;
    // 碰他就直接告诉他: also push to the chat renderer so it can wake the
    // pet immediately instead of waiting for the next chat — the renderer
    // owns all gating, exactly like the Electron main pushing
    // minicpm:pet-touched after this same interaction report.
    let _ = app.emit("pet-touched", serde_json::json!({ "kind": kind }));
    std::thread::spawn(move || {
        let token = gateway_token();
        let body = serde_json::json!({
            "kind": kind,
            "clicks": clicks,
            "distance_px": distance_px,
            "duration_ms": duration_ms,
        })
        .to_string();
        let _ = http_request("POST", "/api/pet/interaction", Some(&body), &token, 2500);
    });
}

// ── pet windows (render + hit move together) ────────────────────────────

/// Move both pet windows by a delta (called per mousemove from the hit
/// window during a drag). The hit page reports deltas in CSS px (DIP) —
/// convert with the real scale factor or the pet crawls at half speed
/// on a 2x display.
#[tauri::command]
fn pet_move_by(app: tauri::AppHandle, dx: i64, dy: i64) -> Result<(), String> {
    let render = app
        .get_webview_window("pet-render")
        .ok_or("no pet-render window")?;
    let scale = render.scale_factor().map_err(|e| e.to_string())?;
    let pdx = (dx as f64 * scale).round() as i32;
    let pdy = (dy as f64 * scale).round() as i32;
    for label in ["pet-render", "pet-hit"] {
        let w = app
            .get_webview_window(label)
            .ok_or_else(|| format!("no {label} window"))?;
        let p = w.outer_position().map_err(|e| e.to_string())?;
        w.set_position(PhysicalPosition::new(p.x + pdx, p.y + pdy))
            .map_err(|e| e.to_string())?;
    }
    Ok(())
}

/// Current pet-render origin (physical), for hit-window alignment.
fn pet_render_pos(app: &tauri::AppHandle) -> Result<(i32, i32), String> {
    let w = app
        .get_webview_window("pet-render")
        .ok_or("no pet-render window")?;
    let p = w.outer_position().map_err(|e| e.to_string())?;
    Ok((p.x, p.y))
}

// ── chat bubble ─────────────────────────────────────────────────────────

/// Open the chat bubble hugging the pet (left side preferred, flip right
/// when off-screen, clamp into the monitor).
#[tauri::command]
fn open_chat(app: tauri::AppHandle) -> Result<(), String> {
    CHAT_ARMED.store(true, std::sync::atomic::Ordering::Relaxed);
    let pet = app
        .get_webview_window("pet-render")
        .ok_or("no pet-render window")?;
    let chat = app.get_webview_window("chat").ok_or("no chat window")?;
    let pet_pos = pet.outer_position().map_err(|e| e.to_string())?;
    let pet_size = pet.outer_size().map_err(|e| e.to_string())?;
    let chat_size = chat.outer_size().map_err(|e| e.to_string())?;
    let (mx, my, mw, mh) = match pet.current_monitor().ok().flatten() {
        Some(m) => (
            m.position().x,
            m.position().y,
            m.size().width as i32,
            m.size().height as i32,
        ),
        None => (0, 0, 1920, 1080),
    };
    let gap = 12;
    let mut x = pet_pos.x - chat_size.width as i32 - gap;
    if x < mx {
        x = pet_pos.x + pet_size.width as i32 + gap;
    }
    if x + chat_size.width as i32 > mx + mw {
        x = mx + mw - chat_size.width as i32 - gap;
    }
    let mut y = pet_pos.y - 30;
    if y < my + 8 {
        y = my + 8;
    }
    if y + chat_size.height as i32 > my + mh {
        y = my + mh - chat_size.height as i32 - 8;
    }
    chat.set_position(PhysicalPosition::new(x, y)).map_err(|e| e.to_string())?;
    chat.show().map_err(|e| e.to_string())?;
    chat.set_focus().map_err(|e| e.to_string())?;
    Ok(())
}

/// Chat anchor (physical Y the bubble bottom should stay pinned to while
/// it grows — the original's setChatAnchor contract).
static CHAT_ANCHOR: std::sync::Mutex<Option<i32>> = std::sync::Mutex::new(None);

#[tauri::command]
fn chat_anchor(bottom_y: Option<i64>) {
    *CHAT_ANCHOR.lock().unwrap() = bottom_y.map(|v| v as i32);
}

#[tauri::command]
fn chat_resize(app: tauri::AppHandle, width: i64, height: i64) -> Result<(), String> {
    let chat = app.get_webview_window("chat").ok_or("no chat window")?;
    let scale = chat.scale_factor().map_err(|e| e.to_string())?;
    // Renderer numbers are CSS px (the original Electron contract) →
    // apply as LOGICAL size, never physical.
    let new_w = (width.max(120) as f64).min(720.0);
    let new_h = (height.max(40) as f64).min(900.0);
    let size = chat.outer_size().map_err(|e| e.to_string())?;
    let old_w_logical = size.width as f64 / scale;
    let old_h_logical = size.height as f64 / scale;
    if (new_w - old_w_logical).abs() < 0.5 && (new_h - old_h_logical).abs() < 0.5 {
        return Ok(());
    }
    let pos = chat.outer_position().map_err(|e| e.to_string())?;
    // Keep the anchored bottom fixed while the height changes (grow up).
    let anchor = *CHAT_ANCHOR.lock().unwrap();
    let mut new_y = pos.y;
    if anchor.is_some() {
        new_y = pos.y + ((old_h_logical - new_h) * scale).round() as i32;
    }
    chat.set_size(tauri::LogicalSize::new(new_w, new_h))
        .map_err(|e| e.to_string())?;
    if new_y != pos.y {
        chat.set_position(PhysicalPosition::new(pos.x, new_y))
            .map_err(|e| e.to_string())?;
    }
    Ok(())
}

// Chat visibility gate: the chat page loads at boot and its renderer
// self-opens (cmdOpen) — but the ORIGINAL shell only surfaces the bubble
// when the user requests chat. Ignore show/hide cycles until armed once.
static CHAT_ARMED: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

#[tauri::command]
fn chat_show(app: tauri::AppHandle) -> Result<(), String> {
    if !CHAT_ARMED.load(std::sync::atomic::Ordering::Relaxed) {
        return Ok(());
    }
    let chat = app.get_webview_window("chat").ok_or("no chat window")?;
    chat.show().map_err(|e| e.to_string())
}

#[tauri::command]
fn chat_hide(app: tauri::AppHandle) -> Result<(), String> {
    let chat = app.get_webview_window("chat").ok_or("no chat window")?;
    chat.hide().map_err(|e| e.to_string())
}

#[tauri::command]
fn chat_focus(app: tauri::AppHandle) -> Result<(), String> {
    let chat = app.get_webview_window("chat").ok_or("no chat window")?;
    chat.set_focus().map_err(|e| e.to_string())
}

/// Chat history: the SAME file the Electron shell uses
/// (%APPDATA%/deskpt/chat-history.json) — one conversation stream across
/// both shells (同一个我).
fn chat_history_path() -> Result<PathBuf, String> {
    std::env::var("APPDATA")
        .map(|a| PathBuf::from(a).join("deskpt").join("chat-history.json"))
        .map_err(|_| "no APPDATA".to_string())
}

#[tauri::command]
fn chat_load_history() -> Result<serde_json::Value, String> {
    let p = chat_history_path()?;
    if !p.is_file() {
        return Ok(serde_json::Value::Null);
    }
    let raw = std::fs::read_to_string(p).map_err(|e| e.to_string())?;
    serde_json::from_str(&raw).map_err(|e| e.to_string())
}

#[tauri::command]
fn chat_save_history(json: String) -> Result<(), String> {
    let p = chat_history_path()?;
    if let Some(dir) = p.parent() {
        std::fs::create_dir_all(dir).map_err(|e| e.to_string())?;
    }
    std::fs::write(p, json).map_err(|e| e.to_string())
}

// ── context menu (original context-menu.html styling) ───────────────────

#[tauri::command]
fn menu_show(app: tauri::AppHandle, x: i64, y: i64) -> Result<(), String> {
    let menu = app.get_webview_window("menu").ok_or("no menu window")?;
    let size = menu.outer_size().map_err(|e| e.to_string())?;
    // The hit page reports cursor in CSS px (DIP) — convert to physical.
    let scale = menu.scale_factor().map_err(|e| e.to_string())?;
    let cx = (x as f64 * scale).round() as i32;
    let cy = (y as f64 * scale).round() as i32;
    let (mx, my, mw, mh) = match menu.current_monitor().ok().flatten() {
        Some(m) => (
            m.position().x,
            m.position().y,
            m.size().width as i32,
            m.size().height as i32,
        ),
        None => (0, 0, 1920, 1080),
    };
    // open left of the cursor like a native menu; clamp into the monitor
    let mut wx = cx - size.width as i32;
    let mut wy = cy;
    if wx < mx + 4 {
        wx = cx + 4;
    }
    if wx + size.width as i32 > mx + mw {
        wx = mx + mw - size.width as i32 - 4;
    }
    if wy + size.height as i32 > my + mh {
        wy = my + mh - size.height as i32 - 4;
    }
    menu.set_position(PhysicalPosition::new(wx, wy)).map_err(|e| e.to_string())?;
    menu.show().map_err(|e| e.to_string())?;
    menu.set_focus().map_err(|e| e.to_string())
}

#[tauri::command]
fn menu_resize(app: tauri::AppHandle, width: i64, height: i64) -> Result<(), String> {
    let menu = app.get_webview_window("menu").ok_or("no menu window")?;
    menu.set_size(tauri::LogicalSize::new(width.max(120) as f64, height.max(40) as f64))
        .map_err(|e| e.to_string())
}

#[tauri::command]
fn menu_hide(app: tauri::AppHandle) -> Result<(), String> {
    let menu = app.get_webview_window("menu").ok_or("no menu window")?;
    menu.hide().map_err(|e| e.to_string())
}

// ── misc shell actions ──────────────────────────────────────────────────

#[tauri::command]
fn hide_pet(app: tauri::AppHandle) -> Result<(), String> {
    for label in ["pet-render", "pet-hit"] {
        if let Some(w) = app.get_webview_window(label) {
            if w.is_visible().unwrap_or(false) {
                w.hide().map_err(|e| e.to_string())?;
            } else {
                w.show().map_err(|e| e.to_string())?;
            }
        }
    }
    Ok(())
}

/// temp diagnostics: append to %TEMP%/deskpt-debug.log
#[tauri::command]
fn debug_log(msg: String) {
    if let Ok(mut f) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(std::env::temp_dir().join("deskpt-debug.log"))
    {
        let _ = writeln!(f, "[{:?}] {}", std::process::id(), msg);
    }
}

#[tauri::command]
fn quit_app(app: tauri::AppHandle) {
    app.exit(0);
}

// ── 身体移动: the pet walks itself ──────────────────────────────────────

static WALKING: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

/// Walk the pet body by a DIP delta, animated (step ~12 DIP / 30ms).
/// The model emits [WALK:dx,dy]; the gateway turns it into SSE events and
/// the renderer calls this. Clamped to the monitor so the body cannot
/// wander off-screen.
#[tauri::command]
fn pet_walk_to(app: tauri::AppHandle, dx: i64, dy: i64) -> Result<(), String> {
    use tauri::Emitter;
    if WALKING.swap(true, std::sync::atomic::Ordering::Relaxed) {
        return Ok(()); // one walk at a time
    }
    let render = app
        .get_webview_window("pet-render")
        .ok_or("no pet-render window")?;
    let scale = render.scale_factor().map_err(|e| e.to_string())?;
    let total_dx = (dx as f64 * scale).round() as i32;
    let total_dy = (dy as f64 * scale).round() as i32;
    let steps = ((total_dx.abs() + total_dy.abs()) / 24).clamp(1, 60);
    let _ = app.emit("pet-reaction", serde_json::json!({"kind": "walk-start"}));
    std::thread::spawn(move || {
        let start: Vec<(tauri::WebviewWindow, i32, i32)> = ["pet-render", "pet-hit"]
            .iter()
            .filter_map(|l| app.get_webview_window(l))
            .filter_map(|w| {
                let p = w.outer_position().ok()?;
                Some((w, p.x, p.y))
            })
            .collect();
        for s in 1..=steps {
            let fx = s as f64 / steps as f64;
            let tx = (total_dx as f64 * fx).round() as i32;
            let ty = (total_dy as f64 * fx).round() as i32;
            for (w, sx, sy) in &start {
                let _ = w.set_position(PhysicalPosition::new(sx + tx, sy + ty));
            }
            std::thread::sleep(Duration::from_millis(28));
        }
        let _ = app.emit("pet-reaction", serde_json::json!({"kind": "walk-end"}));
        WALKING.store(false, std::sync::atomic::Ordering::Relaxed);
    });
    Ok(())
}

/// [WALK_DESKTOP]: hop the pet body to another virtual desktop.
#[tauri::command]
fn pet_hop_desktop(app: tauri::AppHandle) -> Result<(), String> {
    let mut hwnds = Vec::new();
    for label in ["pet-render", "pet-hit"] {
        if let Some(w) = app.get_webview_window(label) {
            if let Ok(h) = w.hwnd() {
                hwnds.push(h.0 as isize);
            }
        }
    }
    #[cfg(windows)]
    {
        if crate::virtual_desktop::hop_to_other_desktop(&hwnds) {
            return Ok(());
        }
        return Err("no other desktop (or the move was refused)".to_string());
    }
    #[cfg(not(windows))]
    Err("virtual desktops are Windows-only".to_string())
}

/// UI language for the copied original renderers (settings file → env → zh).
#[tauri::command]
fn shell_lang() -> String {
    settings_read()
        .get("lang")
        .and_then(|l| l.as_str())
        .map(|s| s.to_string())
        .or_else(|| std::env::var("DESKPT_LANG").ok())
        .unwrap_or_else(|| "zh".to_string())
}

// ── settings (persisted: %APPDATA%/deskpt/tauri-settings.json) ──────────

fn settings_path() -> Result<PathBuf, String> {
    std::env::var("APPDATA")
        .map(|a| PathBuf::from(a).join("deskpt").join("tauri-settings.json"))
        .map_err(|_| "no APPDATA".to_string())
}

fn settings_read() -> serde_json::Value {
    settings_path()
        .ok()
        .and_then(|p| std::fs::read_to_string(p).ok())
        .and_then(|raw| serde_json::from_str(&raw).ok())
        .unwrap_or(serde_json::json!({}))
}

// ── the REAL prefs file (shared with the Electron shell) ────────────────
fn prefs_path() -> Result<PathBuf, String> {
    std::env::var("APPDATA")
        .map(|a| PathBuf::from(a).join("deskpt").join("clawd-prefs.json"))
        .map_err(|_| "no APPDATA".to_string())
}

fn prefs_read() -> serde_json::Value {
    prefs_path()
        .ok()
        .and_then(|p| std::fs::read_to_string(p).ok())
        .and_then(|raw| serde_json::from_str::<serde_json::Value>(&raw).ok())
        .unwrap_or(serde_json::json!({}))
}

fn prefs_write(value: &serde_json::Value) -> Result<(), String> {
    let p = prefs_path()?;
    if let Some(dir) = p.parent() {
        std::fs::create_dir_all(dir).map_err(|e| e.to_string())?;
    }
    std::fs::write(p, serde_json::to_string_pretty(value).map_err(|e| e.to_string())?)
        .map_err(|e| e.to_string())
}

/// Write a possibly-DOTTED key ("agents.claude-code.enabled") so prefs
/// keep the SAME nested shape the Electron shell reads.
fn set_dotted(root: &mut serde_json::Value, key: &str, value: serde_json::Value) {
    if !key.contains('.') {
        root[key] = value;
        return;
    }
    let mut parts: Vec<&str> = key.split('.').collect();
    let last = parts.pop().unwrap();
    let mut cur = root;
    for part in parts {
        if !cur.get(part).map(|v| v.is_object()).unwrap_or(false) {
            cur[part] = serde_json::json!({});
        }
        cur = &mut cur[part];
    }
    cur[last] = value;
}

// ── REAL model / engine / LoRA management (Settings → MiniCPM tab) ──────
// Mirrors the Electron original's minicpm-settings:* handlers one-to-one
// (clawd-on-desk/src/minicpm-chat.js): same prefs keys (model_folders,
// model_dir), same gateway endpoints, same result shapes — because the
// settings UI is the ORIGINAL renderer and both shells share one brain.

fn is_main_model_file(name: &str) -> bool {
    let lower = name.to_lowercase();
    lower.ends_with(".gguf") && !lower.starts_with("mmproj-")
}

fn first_gguf_in_dir(dir: &std::path::Path) -> Option<PathBuf> {
    let mut entries: Vec<PathBuf> = std::fs::read_dir(dir)
        .ok()?
        .filter_map(|e| e.ok())
        .map(|e| e.path())
        .collect();
    entries.sort();
    entries.into_iter().find(|p| {
        p.is_file()
            && p.file_name()
                .and_then(|n| n.to_str())
                .map(is_main_model_file)
                .unwrap_or(false)
    })
}

fn model_folders_list() -> Vec<String> {
    prefs_read()
        .get("model_folders")
        .and_then(|v| v.as_array())
        .map(|a| {
            a.iter()
                .filter_map(|x| x.as_str())
                .filter(|s| !s.trim().is_empty())
                .map(|s| s.to_string())
                .collect()
        })
        .unwrap_or_default()
}

fn set_model_folders(list: Vec<String>) -> Result<(), String> {
    let mut prefs = prefs_read();
    prefs["model_folders"] = serde_json::json!(list);
    prefs_write(&prefs)
}

fn app_data_dir() -> PathBuf {
    std::env::var("APPDATA")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from("."))
        .join("deskpt")
}

/// Explorer: /select,reveal a file — or open a directory/file normally.
fn shell_open(path: &str, reveal: bool) -> Result<(), String> {
    #[cfg(windows)]
    {
        let mut cmd = Command::new("explorer.exe");
        if reveal {
            cmd.arg("/select,").arg(path);
        } else {
            cmd.arg(path);
        }
        cmd.creation_flags(CREATE_NO_WINDOW)
            .spawn()
            .map_err(|e| e.to_string())?;
        Ok(())
    }
    #[cfg(not(windows))]
    {
        let _ = (path, reveal);
        Ok(())
    }
}

/// Gateway JSON over the raw-TCP client. Non-200 → Err with the body.
fn gw_json(
    method: &str,
    path: &str,
    body: Option<&serde_json::Value>,
    timeout_ms: u64,
) -> Result<serde_json::Value, String> {
    let token = gateway_token();
    let body_str = body.map(|v| v.to_string());
    let (status, text) = http_request(method, path, body_str.as_deref(), &token, timeout_ms)?;
    if status != 200 {
        let snippet: String = text.chars().take(220).collect();
        return Err(format!("HTTP {status}: {snippet}"));
    }
    serde_json::from_str(&text).map_err(|e| format!("bad json: {e}"))
}

#[tauri::command]
async fn pick_folder(title: String) -> Result<Option<String>, String> {
    tauri::async_runtime::spawn_blocking(move || {
        rfd::FileDialog::new()
            .set_title(&title)
            .pick_folder()
            .map(|p| p.to_string_lossy().to_string())
    })
    .await
    .map_err(|e| e.to_string())
}

#[tauri::command]
async fn pick_gguf(title: String) -> Result<Option<String>, String> {
    tauri::async_runtime::spawn_blocking(move || {
        rfd::FileDialog::new()
            .set_title(&title)
            .add_filter("GGUF model", &["gguf"])
            .pick_file()
            .map(|p| p.to_string_lossy().to_string())
    })
    .await
    .map_err(|e| e.to_string())
}

#[tauri::command]
fn open_path(path: String, reveal: bool) -> Result<(), String> {
    shell_open(&path, reveal)
}

#[tauri::command]
fn list_model_folders() -> Result<serde_json::Value, String> {
    Ok(serde_json::json!({ "ok": true, "folders": model_folders_list() }))
}

#[tauri::command]
fn add_model_folder(folder: String) -> Result<serde_json::Value, String> {
    let next = folder.trim();
    if next.is_empty() {
        return Ok(serde_json::json!({ "ok": false, "error": "invalid path" }));
    }
    let p = PathBuf::from(next);
    if !p.is_dir() {
        return Ok(serde_json::json!({ "ok": false, "error": "selected path is not a folder" }));
    }
    // canonicalize → strip the \\?\ device prefix Windows adds
    let resolved = p
        .canonicalize()
        .map(|c| c.to_string_lossy().trim_start_matches(r"\\?\").to_string())
        .unwrap_or_else(|_| next.to_string());
    let mut cur = model_folders_list();
    if cur.iter().any(|x| x.eq_ignore_ascii_case(&resolved)) {
        return Ok(serde_json::json!({ "ok": true, "folders": cur, "duplicate": true }));
    }
    cur.push(resolved);
    set_model_folders(cur.clone())?;
    Ok(serde_json::json!({ "ok": true, "folders": cur }))
}

#[tauri::command]
async fn add_model_folder_dialog() -> Result<serde_json::Value, String> {
    let picked = pick_folder("选择模型文件夹".to_string()).await?;
    match picked {
        None => Ok(serde_json::json!({ "ok": false, "canceled": true })),
        Some(f) => add_model_folder(f),
    }
}

#[tauri::command]
fn remove_model_folder(folder: String) -> Result<serde_json::Value, String> {
    let target = folder.to_lowercase();
    if target.is_empty() {
        return Ok(serde_json::json!({ "ok": false, "error": "invalid path" }));
    }
    let cur = model_folders_list();
    let next: Vec<String> = cur
        .iter()
        .filter(|x| !x.to_lowercase().eq(&target))
        .cloned()
        .collect();
    if next.len() == cur.len() {
        return Ok(serde_json::json!({ "ok": true, "folders": cur, "noop": true }));
    }
    set_model_folders(next.clone())?;
    Ok(serde_json::json!({ "ok": true, "folders": next }))
}

#[tauri::command]
fn list_models() -> Result<serde_json::Value, String> {
    let mut paths: Vec<PathBuf> = Vec::new();
    for f in model_folders_list() {
        if let Ok(rd) = std::fs::read_dir(&f) {
            for e in rd.flatten() {
                let p = e.path();
                if p.is_file()
                    && p.file_name()
                        .and_then(|n| n.to_str())
                        .map(is_main_model_file)
                        .unwrap_or(false)
                {
                    paths.push(p);
                }
            }
        }
    }
    // the current effective model dir participates too (original behavior)
    let cur = prefs_read()
        .get("model_dir")
        .and_then(|v| v.as_str())
        .map(PathBuf::from);
    if let Some(c) = cur {
        if c.is_file() {
            paths.push(c);
        } else if c.is_dir() {
            if let Some(g) = first_gguf_in_dir(&c) {
                paths.push(g);
            }
        }
    }
    let mut seen = std::collections::HashSet::new();
    let models: Vec<serde_json::Value> = paths
        .into_iter()
        .filter(|p| seen.insert(p.to_string_lossy().to_lowercase()))
        .map(|p| {
            serde_json::json!({
                "path": p.to_string_lossy(),
                "name": p.file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_default(),
            })
        })
        .collect();
    Ok(serde_json::json!({ "ok": true, "models": models, "folders": model_folders_list() }))
}

async fn use_model_dir_impl(
    path: String,
    mmproj: Option<String>,
) -> Result<serde_json::Value, String> {
    let trimmed = path.trim().to_string();
    if trimmed.is_empty() {
        return Ok(serde_json::json!({ "ok": false, "error": "无效的模型路径" }));
    }
    let p = PathBuf::from(&trimmed);
    if !p.is_file() {
        return Ok(serde_json::json!({ "ok": false, "error": format!("模型文件不存在：\n{trimmed}") }));
    }
    let is_gguf = p
        .extension()
        .map(|e| e.to_string_lossy().to_lowercase() == "gguf")
        .unwrap_or(false);
    if !is_gguf {
        return Ok(serde_json::json!({ "ok": false, "error": format!("请选择 .gguf 模型：\n{trimmed}") }));
    }
    // persist effective model dir (SAME prefs key as the Electron shell;
    // takes effect for future sidecar spawns too)
    let mut prefs = prefs_read();
    prefs["model_dir"] = serde_json::json!(trimmed);
    prefs_write(&prefs)?;
    // hot-swap on the RUNNING llama-server: gateway stops + respawns it
    // with the new --model and blocks until /health is 200 again.
    let mut body = serde_json::json!({ "path": trimmed });
    if let Some(m) = mmproj.as_deref().map(str::trim).filter(|s| !s.is_empty()) {
        body["mmproj"] = serde_json::json!(m);
    }
    match gw_json("POST", "/api/load-model", Some(&body), 300_000) {
        Ok(r) => {
            if r.get("error").is_some() {
                Ok(serde_json::json!({
                    "ok": true, "modelDir": trimmed, "reloaded": false,
                    "reloadError": r.get("error").cloned().unwrap_or(serde_json::json!("unknown")),
                }))
            } else {
                Ok(serde_json::json!({ "ok": true, "modelDir": trimmed, "reloaded": true }))
            }
        }
        Err(e) => Ok(serde_json::json!({
            "ok": true, "modelDir": trimmed, "reloaded": false, "reloadError": e,
        })),
    }
}

#[tauri::command]
async fn use_model_dir(path: String, mmproj: Option<String>) -> Result<serde_json::Value, String> {
    use_model_dir_impl(path, mmproj).await
}

#[tauri::command]
async fn pick_model_dir() -> Result<serde_json::Value, String> {
    // Windows quirk from the original: file-only picker — directories go
    // through the "add model folder" flow, so the user can always pick a
    // single .gguf.
    let default_dir = prefs_read()
        .get("model_dir")
        .and_then(|v| v.as_str())
        .map(PathBuf::from)
        .map(|c| {
            if c.is_file() {
                c.parent().map(|p| p.to_path_buf()).unwrap_or(c)
            } else {
                c
            }
        })
        .filter(|d| d.is_dir());
    let picked = tauri::async_runtime::spawn_blocking(move || {
        let mut d = rfd::FileDialog::new()
            .set_title("选择本地模型 (.gguf 文件)")
            .add_filter("GGUF model", &["gguf"]);
        if let Some(dd) = default_dir {
            d = d.set_directory(&dd);
        }
        d.pick_file()
    })
    .await
    .map_err(|e| e.to_string())?;
    let Some(picked) = picked else {
        return Ok(serde_json::json!({ "ok": false, "canceled": true }));
    };
    let name = picked
        .file_name()
        .map(|n| n.to_string_lossy().to_string())
        .unwrap_or_default();
    if !is_main_model_file(&name) {
        return Ok(serde_json::json!({ "ok": false, "error": format!(
            "mmproj-* 是视觉投影文件，不能作为主模型。\n请选择同目录里的主模型 .gguf：\n{}",
            picked.to_string_lossy()
        ) }));
    }
    use_model_dir_impl(picked.to_string_lossy().to_string(), None).await
}

#[tauri::command]
async fn open_model_dir() -> Result<serde_json::Value, String> {
    // resolve the CURRENT gguf: gateway health → prefs model_dir
    let health = gw_json("GET", "/api/health", None, 1500).ok();
    let mut gguf: Option<PathBuf> = None;
    if let Some(h) = &health {
        if let Some(md) = h.get("model_dir").and_then(|v| v.as_str()) {
            let p = PathBuf::from(md);
            if p.is_file() {
                gguf = Some(p);
            } else if p.is_dir() {
                gguf = first_gguf_in_dir(&p);
            }
        }
    }
    if gguf.is_none() {
        let cur = prefs_read()
            .get("model_dir")
            .and_then(|v| v.as_str())
            .map(PathBuf::from);
        if let Some(c) = cur {
            if c.is_file() {
                gguf = Some(c);
            } else if c.is_dir() {
                gguf = first_gguf_in_dir(&c);
            }
        }
    }
    if let Some(g) = gguf {
        shell_open(&g.to_string_lossy(), true)?;
        return Ok(serde_json::json!({ "ok": true, "path": g.to_string_lossy(), "highlighted": true }));
    }
    let dir = prefs_read()
        .get("model_dir")
        .and_then(|v| v.as_str())
        .map(PathBuf::from)
        .unwrap_or_else(|| app_data_dir().join("models"));
    std::fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    shell_open(&dir.to_string_lossy(), false)?;
    Ok(serde_json::json!({ "ok": true, "dir": dir.to_string_lossy(), "highlighted": false }))
}

#[tauri::command]
fn open_logs_dir() -> Result<serde_json::Value, String> {
    let dir = app_data_dir().join("logs");
    std::fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    shell_open(&dir.to_string_lossy(), false)?;
    Ok(serde_json::json!({ "ok": true, "dir": dir.to_string_lossy() }))
}

#[tauri::command]
fn engine_local_version() -> Result<serde_json::Value, String> {
    let exe = gateway_dir().join("bin").join("win-x64").join("llama-server.exe");
    if !exe.is_file() {
        return Ok(serde_json::json!({ "version": null, "error": "llama-server.exe not found" }));
    }
    #[cfg(windows)]
    {
        match Command::new(&exe)
            .arg("--version")
            .creation_flags(CREATE_NO_WINDOW)
            .output()
        {
            Ok(o) => {
                let s = format!(
                    "{}{}",
                    String::from_utf8_lossy(&o.stdout),
                    String::from_utf8_lossy(&o.stderr)
                );
                let v = s
                    .lines()
                    .next()
                    .map(|l| l.trim().to_string())
                    .unwrap_or_default();
                Ok(serde_json::json!({ "version": if v.is_empty() { None } else { Some(v) } }))
            }
            Err(e) => Ok(serde_json::json!({ "version": null, "error": e.to_string() })),
        }
    }
    #[cfg(not(windows))]
    {
        Ok(serde_json::json!({ "version": null }))
    }
}

#[tauri::command]
async fn engine_update_check() -> Result<serde_json::Value, String> {
    gw_json("GET", "/api/engine-update-check", None, 30_000)
}

/// POST an engine-update endpoint and forward its SSE phase stream to the
/// settings UI as `engine-update-progress` events (same channel contract
/// as the Electron original's applyEngineUpdate).
async fn engine_apply(
    app: &tauri::AppHandle,
    path: &str,
    dir: Option<String>,
) -> Result<serde_json::Value, String> {
    use tauri::Emitter;
    use std::io::{BufRead, BufReader};
    let token = gateway_token();
    let path = path.to_string();
    let body = dir.map(|d| serde_json::json!({ "dir": d }).to_string());
    let apph = app.clone();
    tauri::async_runtime::spawn_blocking(move || {
        let stream = std::net::TcpStream::connect((GW_HOST, GW_PORT)).map_err(|e| e.to_string())?;
        stream
            .set_read_timeout(Some(Duration::from_secs(600)))
            .map_err(|e| e.to_string())?;
        let body_str = body.unwrap_or_default();
        let req = format!(
            "POST {path} HTTP/1.0\r\nHost: {GW_HOST}:{GW_PORT}\r\n\
             content-type: application/json\r\n\
             x-minicpm-token: {token}\r\n\
             content-length: {}\r\n\r\n{body_str}",
            body_str.len()
        );
        let mut writer = stream.try_clone().map_err(|e| e.to_string())?;
        writer.write_all(req.as_bytes()).map_err(|e| e.to_string())?;
        let reader = BufReader::new(stream);
        let mut last_phase = String::new();
        for line in reader.lines() {
            let line = match line {
                Ok(l) => l,
                Err(_) => break,
            };
            let t = line.trim_end_matches('\r');
            if let Some(data) = t.strip_prefix("data:") {
                if let Ok(ev) = serde_json::from_str::<serde_json::Value>(data.trim()) {
                    if let Some(phase) = ev.get("phase").and_then(|p| p.as_str()) {
                        last_phase = phase.to_string();
                    }
                    let _ = apph.emit("engine-update-progress", ev);
                }
            }
        }
        Ok::<serde_json::Value, String>(serde_json::json!({ "ok": true, "phase": last_phase }))
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn engine_update_apply(app: tauri::AppHandle) -> Result<serde_json::Value, String> {
    engine_apply(&app, "/api/engine-update-apply", None).await
}

#[tauri::command]
async fn engine_update_apply_dir(app: tauri::AppHandle, dir: String) -> Result<serde_json::Value, String> {
    engine_apply(&app, "/api/engine-update-apply-dir", Some(dir)).await
}

#[tauri::command]
async fn list_adapters() -> Result<serde_json::Value, String> {
    gw_json("GET", "/api/adapters", None, 8_000)
}

async fn adapter_root() -> Result<PathBuf, String> {
    let a = gw_json("GET", "/api/adapters", None, 8_000)?;
    a.get("adapter_dir")
        .and_then(|v| v.as_str())
        .map(PathBuf::from)
        .ok_or_else(|| "adapter dir unknown".to_string())
}

#[tauri::command]
async fn upload_adapter() -> Result<serde_json::Value, String> {
    let picked = pick_gguf("选择 LoRA 适配器 (.gguf)".to_string()).await?;
    let Some(p) = picked else {
        return Ok(serde_json::json!({ "ok": false, "canceled": true }));
    };
    let src = PathBuf::from(&p);
    if !src.is_file() {
        return Ok(serde_json::json!({ "ok": false, "error": "file not found" }));
    }
    let dir = adapter_root().await?;
    std::fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    let name = src.file_name().ok_or("bad file name")?.to_string_lossy().to_string();
    let dest = dir.join(&name);
    std::fs::copy(&src, &dest).map_err(|e| e.to_string())?;
    let adapters = gw_json("GET", "/api/adapters", None, 8_000)?;
    Ok(serde_json::json!({ "ok": true, "file": name, "adapters": adapters }))
}

fn sanitized_adapter_path(dir: &std::path::Path, name: &str) -> Option<PathBuf> {
    if name.is_empty() || name.contains('\\') || name.contains('/') || name.contains("..") {
        return None;
    }
    let direct = dir.join(name);
    if direct.is_file() {
        return Some(direct);
    }
    let with_ext = dir.join(format!("{name}.gguf"));
    if with_ext.is_file() {
        return Some(with_ext);
    }
    None
}

#[tauri::command]
async fn remove_adapter(name: String) -> Result<serde_json::Value, String> {
    let dir = adapter_root().await?;
    let target = sanitized_adapter_path(&dir, &name)
        .ok_or_else(|| format!("adapter not found: {name}"))?;
    std::fs::remove_file(&target).map_err(|e| e.to_string())?;
    let adapters = gw_json("GET", "/api/adapters", None, 8_000)?;
    Ok(serde_json::json!({ "ok": true, "adapters": adapters }))
}

#[tauri::command]
async fn rename_adapter(old_name: String, new_name: String) -> Result<serde_json::Value, String> {
    let dir = adapter_root().await?;
    let from = sanitized_adapter_path(&dir, &old_name)
        .ok_or_else(|| format!("adapter not found: {old_name}"))?;
    let new_stem = new_name.trim();
    if new_stem.is_empty() || new_stem.contains('\\') || new_stem.contains('/') || new_stem.contains("..") {
        return Ok(serde_json::json!({ "ok": false, "error": "invalid name" }));
    }
    let new_file = dir.join(format!(
        "{}{}",
        new_stem.trim_end_matches(".gguf"),
        ".gguf"
    ));
    std::fs::rename(&from, &new_file).map_err(|e| e.to_string())?;
    let adapters = gw_json("GET", "/api/adapters", None, 8_000)?;
    Ok(serde_json::json!({ "ok": true, "adapters": adapters }))
}

#[tauri::command]
async fn open_adapter_dir() -> Result<serde_json::Value, String> {
    let dir = adapter_root().await?;
    std::fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    shell_open(&dir.to_string_lossy(), false)?;
    Ok(serde_json::json!({ "ok": true, "dir": dir.to_string_lossy() }))
}

#[tauri::command]
async fn load_adapter(path: Option<String>) -> Result<serde_json::Value, String> {
    // path: None/"" → back to Base (deactivate any LoRA) — gateway semantics
    let body = match path.as_deref().map(str::trim).filter(|s| !s.is_empty()) {
        Some(p) => serde_json::json!({ "path": p }),
        None => serde_json::json!({ "path": null }),
    };
    gw_json("POST", "/api/load-adapter", Some(&body), 300_000)
}

#[tauri::command]
async fn set_device(device: String) -> Result<serde_json::Value, String> {
    gw_json("POST", "/api/set-device", Some(&serde_json::json!({ "device": device })), 8_000)
}

/// Kill the sidecar python (and its llama-server), then spawn + wait for
/// health — the real "restart sidecar" behind setDeviceAndRestart.
#[tauri::command]
async fn restart_gateway() -> Result<serde_json::Value, String> {
    #[cfg(windows)]
    {
        let script = "Get-Process python,llama-server -ErrorAction SilentlyContinue | Where-Object { $_.Path -and ($_.Path -like '*minicpm-sidecar*') } | Stop-Process -Force";
        let _ = Command::new("powershell")
            .args(["-NoProfile", "-Command", script])
            .creation_flags(CREATE_NO_WINDOW)
            .output();
    }
    spawn_sidecar()?;
    let token = gateway_token();
    for _ in 0..90 {
        std::thread::sleep(Duration::from_millis(500));
        if health_ok(&token) {
            return Ok(serde_json::json!({ "ok": true, "restarted": true }));
        }
    }
    Ok(serde_json::json!({ "ok": false, "error": "gateway did not become healthy after restart" }))
}

#[tauri::command]
fn save_providers_config(providers: serde_json::Value) -> Result<serde_json::Value, String> {
    // mirror the original conversion: array of {provider,apiKey,baseUrl,...}
    // → {providers: {name: {...}}} in ~/.minicpm/providers.json
    let Some(arr) = providers.as_array() else {
        return Ok(serde_json::json!({ "ok": false, "error": "invalid providers" }));
    };
    let mut map = serde_json::Map::new();
    for item in arr {
        let Some(name) = item.get("provider").and_then(|v| v.as_str()) else {
            continue;
        };
        if name.is_empty() {
            continue;
        }
        let mut entry = serde_json::json!({
            "apiKey": item.get("apiKey").and_then(|v| v.as_str()).unwrap_or(""),
            "baseUrl": item.get("baseUrl").and_then(|v| v.as_str()).unwrap_or("https://api.openai.com/v1"),
            "model": item.get("model").and_then(|v| v.as_str()).unwrap_or("gpt-4o"),
        });
        if let Some(t) = item.get("thinking") {
            if !t.is_null() {
                entry["thinking"] = t.clone();
            }
        }
        if let Some(r) = item.get("reasoningEffort") {
            if let Some(s) = r.as_str() {
                if !s.is_empty() {
                    entry["reasoningEffort"] = json!(s);
                }
            }
        }
        if let Some(c) = item.get("contextWindow") {
            if let Some(n) = c.as_i64() {
                entry["contextWindow"] = json!(n);
            }
        }
        map.insert(name.to_string(), entry);
    }
    let path = std::env::var("USERPROFILE")
        .map(|h| PathBuf::from(h).join(".minicpm").join("providers.json"))
        .map_err(|_| "no home dir")?;
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir).map_err(|e| e.to_string())?;
    }
    std::fs::write(
        &path,
        serde_json::to_string_pretty(&serde_json::json!({ "providers": map }))
            .map_err(|e| e.to_string())?,
    )
    .map_err(|e| e.to_string())?;
    Ok(serde_json::json!({ "ok": true }))
}

#[tauri::command]
fn mcp_save_config(servers: serde_json::Value) -> Result<serde_json::Value, String> {
    if !servers.is_object() {
        return Ok(serde_json::json!({ "ok": false, "error": "invalid servers object" }));
    }
    let path = std::env::var("USERPROFILE")
        .map(|h| PathBuf::from(h).join(".minicpm").join("mcp.json"))
        .map_err(|_| "no home dir")?;
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir).map_err(|e| e.to_string())?;
    }
    std::fs::write(
        &path,
        serde_json::to_string_pretty(&serde_json::json!({ "mcpServers": servers }))
            .map_err(|e| e.to_string())?,
    )
    .map_err(|e| e.to_string())?;
    Ok(serde_json::json!({ "ok": true }))
}

/// The settings UI's snapshot: the real clawd-prefs.json (same file the
/// Electron shell uses — one set of user preferences across shells).
#[tauri::command]
fn prefs_load() -> Result<serde_json::Value, String> {
    Ok(prefs_read())
}

/// Settings UI writes: persist into the shared prefs file and apply the
/// live effects this shell supports (pet size / language narration).
#[tauri::command]
fn prefs_update(
    app: tauri::AppHandle,
    key: String,
    value: serde_json::Value,
) -> Result<serde_json::Value, String> {
    let mut prefs = prefs_read();
    set_dotted(&mut prefs, &key, value.clone());
    prefs_write(&prefs)?;
    use tauri::Emitter;
    let _ = app.emit("prefs-changed", serde_json::json!({ "key": key, "value": value }));

    // live effects
    if key == "size" {
        let raw = value.as_str().unwrap_or("M");
        // prefs size is "S"/"M"/"L" or "P:<percent>" — map to the nearest bucket
        let bucket = if let Some(pct) = raw.strip_prefix("P:") {
            let n: f64 = pct.parse().unwrap_or(54.0);
            if n <= 40.0 {
                "S"
            } else if n <= 66.0 {
                "M"
            } else {
                "L"
            }
        } else {
            match raw {
                "S" => "S",
                "L" => "L",
                _ => "M",
            }
        };
        let _ = set_pet_size(app, bucket.to_string());
    } else if key == "lang" {
        let _ = app.emit(
            "chat-narrate",
            serde_json::json!({"text": "语言已切换，重开聊天气泡后生效。", "kind": "info"}),
        );
    }
    Ok(serde_json::json!({"status": "ok"}))
}

/// The MiniCPM tab's prefs file (chat params / narration / model_dir) —
/// the SAME file the Electron shell writes (minicpm-prefs.json).
#[tauri::command]
fn minicpm_prefs_load() -> Result<serde_json::Value, String> {
    let p = std::env::var("APPDATA")
        .map(|a| PathBuf::from(a).join("deskpt").join("minicpm-prefs.json"))
        .map_err(|_| "no APPDATA".to_string())?;
    if !p.is_file() {
        return Ok(serde_json::json!({}));
    }
    let raw = std::fs::read_to_string(p).map_err(|e| e.to_string())?;
    serde_json::from_str(&raw).map_err(|e| e.to_string())
}

#[tauri::command]
fn minicpm_prefs_save(json: String) -> Result<(), String> {
    let p = std::env::var("APPDATA")
        .map(|a| PathBuf::from(a).join("deskpt").join("minicpm-prefs.json"))
        .map_err(|_| "no APPDATA".to_string())?;
    if let Some(dir) = p.parent() {
        std::fs::create_dir_all(dir).map_err(|e| e.to_string())?;
    }
    let mut merged = if p.is_file() {
        std::fs::read_to_string(&p)
            .ok()
            .and_then(|raw| serde_json::from_str::<serde_json::Value>(&raw).ok())
            .unwrap_or_else(|| serde_json::json!({}))
    } else {
        serde_json::json!({})
    };
    let patch: serde_json::Value =
        serde_json::from_str(&json).map_err(|e| e.to_string())?;
    if let (Some(obj), Some(patch_obj)) = (merged.as_object_mut(), patch.as_object()) {
        for (k, v) in patch_obj {
            obj.insert(k.clone(), v.clone());
        }
    }
    std::fs::write(p, serde_json::to_string_pretty(&merged).map_err(|e| e.to_string())?)
        .map_err(|e| e.to_string())
}

/// About tab (检查更新 lives in about too — static info for the replica).
#[tauri::command]
fn about_info() -> serde_json::Value {
    serde_json::json!({
        "version": "0.1.0-tauri",
        "originalVersion": "0.10.0-electron",
        "shell": "Tauri/Rust 复刻壳",
        "brain": "原版 Python 网关（记忆/心情/事件共享）",
    })
}

/// Memory view tab: assemble the SAME payload the original gateway-side
/// handler built — {status, identity:{theme, memory_dir, memory, user},
/// mood:{mood, intensity}, events:{count}} — from /api/memory + /api/mood
/// + the theme's events.json. ASYNC + spawn_blocking: the HTTP reads must
/// never sit on the main thread (a slow/unhealthy gateway would freeze
/// the whole shell — the reported "卡了之后点设置无反应").
#[tauri::command]
async fn memory_view() -> serde_json::Value {
    tauri::async_runtime::spawn_blocking(|| {
        let token = gateway_token();
        let mut out = serde_json::json!({"status": "ok", "identity": {}, "mood": {}, "events": {"count": 0}});
        if let Ok((200, body)) = http_request("GET", "/api/memory", None, &token, 4000) {
            if let Ok(v) = serde_json::from_str::<serde_json::Value>(&body) {
                out["identity"] = v;
            }
        }
        if let Ok((200, body)) = http_request("GET", "/api/mood", None, &token, 4000) {
            if let Ok(v) = serde_json::from_str::<serde_json::Value>(&body) {
                out["mood"] = v;
            }
        }
        // events count from the theme's events.json (same dir the spawned
        // gateway uses)
        let theme = std::env::var("DESKPT_THEME").unwrap_or_else(|_| "cybercat".to_string());
        let events_file = std::env::var("DESKPT_MEMORY_DIR")
            .map(PathBuf::from)
            .or_else(|_| {
                std::env::var("APPDATA").map(|a| {
                    PathBuf::from(a).join("deskpt").join("memories")
                })
            })
            .map(|dir| dir.join(&theme).join("events.json"))
            .ok();
        if let Some(f) = events_file {
            if let Ok(raw) = std::fs::read_to_string(f) {
                if let Ok(v) = serde_json::from_str::<serde_json::Value>(&raw) {
                    let count = v
                        .get("events")
                        .and_then(|e| e.as_array())
                        .map(|a| a.len())
                        .unwrap_or(0);
                    out["events"]["count"] = serde_json::json!(count);
                }
            }
        }
        out
    })
    .await
    .unwrap_or_else(|_| serde_json::json!({"status": "error", "identity": {}, "mood": {}, "events": {"count": 0}}))
}

#[tauri::command]
fn settings_load() -> serde_json::Value {
    settings_read()
}

#[tauri::command]
fn settings_save(json: String) -> Result<(), String> {
    let p = settings_path()?;
    if let Some(dir) = p.parent() {
        std::fs::create_dir_all(dir).map_err(|e| e.to_string())?;
    }
    std::fs::write(p, json).map_err(|e| e.to_string())
}

const PET_SIZES: [(&str, f64); 3] = [("S", 200.0), ("M", 280.0), ("L", 360.0)];

fn pet_size_logical(size: &str) -> f64 {
    PET_SIZES
        .iter()
        .find(|(k, _)| *k == size)
        .map(|(_, v)| *v)
        .unwrap_or(280.0)
}

/// Resize the pet body (S/M/L, same values as the original's size menu) and
/// re-align the hit window to the sprite rect.
#[tauri::command]
fn set_pet_size(app: tauri::AppHandle, size: String) -> Result<(), String> {
    let render = app
        .get_webview_window("pet-render")
        .ok_or("no pet-render window")?;
    let hit = app.get_webview_window("pet-hit").ok_or("no pet-hit window")?;
    let scale = render.scale_factor().map_err(|e| e.to_string())?;
    let win_w = pet_size_logical(&size);
    let pos = render.outer_position().map_err(|e| e.to_string())?;
    render
        .set_size(tauri::LogicalSize::new(win_w, win_w))
        .map_err(|e| e.to_string())?;
    // sprite rect (objectScale layout — same constants as pet-render.js)
    let img_w = win_w * 0.6;
    let img_x = win_w * 0.2;
    let img_h = img_w;
    let img_y = win_w - img_h - win_w * 0.05;
    hit.set_size(tauri::LogicalSize::new(img_w, img_h))
        .map_err(|e| e.to_string())?;
    hit.set_position(PhysicalPosition::new(
        pos.x + (img_x * scale).round() as i32,
        pos.y + (img_y * scale).round() as i32,
    ))
    .map_err(|e| e.to_string())?;
    // persist
    let mut s = settings_read();
    s["size"] = serde_json::json!(size);
    settings_save(s.to_string())
}

/// Open the settings panel CENTERED in the monitor — it's a large normal
/// window (like the original), pet-anchoring only made sense for the old
/// mini panel.
#[tauri::command]
fn open_settings(app: tauri::AppHandle) -> Result<(), String> {
    let mut dbg = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(std::env::temp_dir().join("deskpt-open-settings.log"))
        .ok();
    if let Some(f) = dbg.as_mut() {
        let _ = writeln!(f, "[{:?}] open_settings called", std::process::id());
    }
    let pet = app
        .get_webview_window("pet-render")
        .ok_or("no pet-render window")?;
    let settings = app
        .get_webview_window("settings")
        .ok_or("no settings window")?;
    let s_size = settings.outer_size().map_err(|e| e.to_string())?;
    let (mx, my, mw, mh) = match pet.current_monitor().ok().flatten() {
        Some(m) => (
            m.position().x,
            m.position().y,
            m.size().width as i32,
            m.size().height as i32,
        ),
        None => (0, 0, 1920, 1080),
    };
    // clamp into the monitor (a user-resized window can exceed it)
    let max_w = (mw as f64 * 0.92) as u32;
    let max_h = (mh as f64 * 0.9) as u32;
    let clamped_w = s_size.width.min(max_w);
    let clamped_h = s_size.height.min(max_h);
    if s_size.width > max_w || s_size.height > max_h {
        settings
            .set_size(tauri::PhysicalSize::new(clamped_w, clamped_h))
            .map_err(|e| e.to_string())?;
    }
    let x = mx + (mw - clamped_w as i32) / 2;
    let y = my + (mh - clamped_h as i32) / 2;
    let r = settings.set_position(PhysicalPosition::new(x, y));
    if let Some(f) = dbg.as_mut() {
        let _ = writeln!(f, "  set_position({x},{y}): {:?}", r.as_ref().map(|_| "ok").map_err(|e| e.to_string()));
    }
    let _ = settings.unminimize(); // taskbar-minimized → restore, don't stay hidden
    let r = settings.show();
    if let Some(f) = dbg.as_mut() {
        let _ = writeln!(f, "  show: {:?}", r.as_ref().map(|_| "ok").map_err(|e| e.to_string()));
    }
    r.map_err(|e| e.to_string())?;
    let r = settings.set_focus();
    if let Some(f) = dbg.as_mut() {
        let _ = writeln!(f, "  set_focus: {:?}", r.as_ref().map(|_| "ok").map_err(|e| e.to_string()));
    }
    r.map_err(|e| e.to_string())
}

#[tauri::command]
fn settings_hide(app: tauri::AppHandle) -> Result<(), String> {
    let settings = app
        .get_webview_window("settings")
        .ok_or("no settings window")?;
    settings.hide().map_err(|e| e.to_string())
}

/// Narrate "not ported yet" feedback for unported menu items — reuses the
/// ORIGINAL renderer's narration path so the bubble itself says it.
#[tauri::command]
fn narrate_unported(app: tauri::AppHandle, label: String) -> Result<(), String> {
    use tauri::Emitter;
    let _ = app.emit(
        "chat-narrate",
        serde_json::json!({"text": format!("「{label}」还没移植到新壳——原版有的都会逐步搬过来。"), "kind": "info"}),
    );
    // the chat window must be visible for narration to be seen
    CHAT_ARMED.store(true, std::sync::atomic::Ordering::Relaxed);
    if let Some(chat) = app.get_webview_window("chat") {
        let _ = chat.show();
    }
    Ok(())
}

fn main() {
    tauri::Builder::default()
        .setup(|app| {
            // ── 虚拟桌面重复修复 ────────────────────────────────────
            // Topmost windows are shown on ALL virtual desktops by Windows
            // (the Win+Tab bug). Poll the documented IVirtualDesktopManager:
            // when the pet body is not on the CURRENT desktop → hide it;
            // when it comes back → show it again (unless the user hid it
            // via 隐藏桌宠, tracked from the hit window's visibility).
            #[cfg(windows)]
            {
                let apph = app.handle().clone();
                std::thread::spawn(move || {
                    crate::virtual_desktop::ensure_com();
                    // windows WE hid because the pet left the current desktop
                    let mut auto_hidden: std::collections::HashSet<&'static str> =
                        std::collections::HashSet::new();
                    // IsWindowOnCurrentVirtualDesktop returns FALSE for
                    // freshly-created windows (the OS hasn't assigned them
                    // a desktop yet) — hiding on that made the whole pet
                    // vanish seconds after launch. Only trust a "false"
                    // AFTER the window was once confirmed on-desktop.
                    let mut ever_on_current: std::collections::HashSet<&'static str> =
                        std::collections::HashSet::new();
                    loop {
                        std::thread::sleep(Duration::from_secs(2));
                        for label in ["pet-render", "pet-hit"] {
                            let Some(w) = apph.get_webview_window(label) else {
                                continue;
                            };
                            let Ok(hwnd) = w.hwnd() else { continue };
                            let Some(on_current) =
                                crate::virtual_desktop::is_on_current_desktop(hwnd.0 as isize)
                            else {
                                continue; // API refused — keep behavior
                            };
                            let visible = w.is_visible().unwrap_or(false);
                            if on_current {
                                ever_on_current.insert(label);
                                if visible {
                                    auto_hidden.remove(label);
                                }
                            }
                            if !on_current && ever_on_current.contains(label) && visible {
                                let _ = w.hide();
                                auto_hidden.insert(label);
                            }
                            if on_current && !visible && auto_hidden.remove(label) {
                                let _ = w.show();
                            }
                            // never-on-current + invisible = boot race or the
                            // user's 隐藏桌宠 → leave it alone either way.
                        }
                    }
                });
            }
            // Clicking X (or Alt+F4) DESTROYS a Tauri window by default —
            // the settings window died on its first close and every later
            // 设置 click hit get_webview_window → None (the "second click
            // does nothing" bug). Close = hide for aux windows so they can
            // reopen; closing a pet window means the user wants the app
            // gone → quit.
            {
                let apph = app.handle().clone();
                for label in ["chat", "menu", "settings"] {
                    if let Some(w) = app.get_webview_window(label) {
                        let wh = w.clone();
                        w.on_window_event(move |e| {
                            if let tauri::WindowEvent::CloseRequested { api, .. } = e {
                                api.prevent_close();
                                let _ = wh.hide();
                            }
                        });
                    }
                }
                for label in ["pet-render", "pet-hit"] {
                    if let Some(w) = app.get_webview_window(label) {
                        let apph = apph.clone();
                        w.on_window_event(move |e| {
                            if let tauri::WindowEvent::CloseRequested { api, .. } = e {
                                api.prevent_close();
                                apph.exit(0);
                            }
                        });
                    }
                }
            }
            // The render window is pure view — input goes to the hit window
            // (original two-window model). The hit window must sit EXACTLY
            // over the sprite's on-screen rect, which is the objectScale
            // layout from pet-render.js (imgWidthRatio 0.6, offsetX 0.2,
            // imgBottom 0.05 of the 280-logical window). The theme.json
            // hitBoxes coordinates are in viewBox space and do NOT match
            // that layout — using them left the cat's feet/tail clickable
            // through to the desktop.
            use tauri::PhysicalPosition;
            let app = app.handle();
            if let Some(render) = app.get_webview_window("pet-render") {
                let _ = render.set_ignore_cursor_events(true);
                // apply the persisted pet size (S/M/L) before aligning hit
                let size_name = settings_read()
                    .get("size")
                    .and_then(|s| s.as_str())
                    .unwrap_or("M")
                    .to_string();
                let win_w = pet_size_logical(&size_name);
                let _ = render.set_size(tauri::LogicalSize::new(win_w, win_w));
                if let Some(hit) = app.get_webview_window("pet-hit") {
                    let scale = render.scale_factor().unwrap_or(1.0);
                    let img_w = win_w * 0.6; // objectScale.imgWidthRatio
                    let img_x = win_w * 0.2; // objectScale.offsetX
                    let img_h = img_w; // square GIF canvas
                    let img_y = win_w - img_h - win_w * 0.05; // imgBottom
                    let (rx, ry) = match render.outer_position() {
                        Ok(p) => (p.x, p.y),
                        Err(_) => (0, 0),
                    };
                    let hx = (img_x * scale).round() as i32;
                    let hy = (img_y * scale).round() as i32;
                    let _ = hit.set_position(PhysicalPosition::new(rx + hx, ry + hy));
                    let _ = hit.set_size(tauri::PhysicalSize::new(
                        (img_w * scale).round() as u32,
                        (img_h * scale).round() as u32,
                    ));
                }
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            ensure_gateway,
            chat_start,
            gateway_info,
            report_touch,
            pet_move_by,
            open_chat,
            chat_anchor,
            chat_resize,
            chat_show,
            chat_hide,
            chat_focus,
            chat_load_history,
            chat_save_history,
            menu_show,
            menu_resize,
            menu_hide,
            hide_pet,
            quit_app,
            shell_lang,
            pet_walk_to,
            pet_hop_desktop,
            debug_log,
            settings_load,
            settings_save,
            set_pet_size,
            open_settings,
            settings_hide,
            narrate_unported,
            prefs_load,
            prefs_update,
            about_info,
            memory_view,
            minicpm_prefs_load,
            minicpm_prefs_save,
        ])
        .run(tauri::generate_context!())
        .expect("error while running deskpt-tauri");
}
