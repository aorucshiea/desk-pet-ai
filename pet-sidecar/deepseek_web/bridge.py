#!/usr/bin/env python3
"""OpenAI-compatible bridge: Desk Pet <-> a browser tab running DeepSeek web chat.

The browser extension polls ``/jobs/next``, fills the page's input, sends the
prompt, waits for the reply, and POSTs it back to ``/jobs/<id>/result``.  This
server exposes ``/v1/chat/completions`` (stream + non-stream) so Desk Pet can
use it through the existing OpenAI provider without touching gateway code.

Run:  python bridge.py
Then: load extension/ as an unpacked extension in the browser that is logged
      into https://chat.deepseek.com and keep that tab open.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HOST = "127.0.0.1"
PORT = 8766
JOB_TIMEOUT_SECONDS = 240.0
MAX_PROMPT_CHARS = 60_000
MAX_IMAGES = 4
# Base64 payload only; a decoded screenshot is ~75% of this.
MAX_IMAGE_BASE64_CHARS = 1_500_000
# Four screenshots plus JSON overhead must fit in a chat request body.
MAX_BODY_BYTES = 8_000_000
IMAGE_PLACEHOLDER = "[附带一张屏幕截图，请直接查看图片。]"
_DATA_URL_RE = re.compile(
    r"^data:(image/[A-Za-z0-9.+-]+);base64,([A-Za-z0-9+/=\s]+)$",
    re.IGNORECASE,
)
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".pet", "deepseek-web-config.json")
MIN_DAILY_LIMIT = 0
MAX_DAILY_LIMIT = 10_000


def _daily_limit_from_env() -> int:
    try:
        value = int(os.environ.get("PET_WEB_DAILY_LIMIT", "100"))
    except ValueError:
        return 100
    # 0 / negative = unlimited (useful for local testing).
    return value


def _valid_daily_limit(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if MIN_DAILY_LIMIT <= parsed <= MAX_DAILY_LIMIT:
        return parsed
    return None


def _read_config_daily_limit() -> int | None:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            return _valid_daily_limit(data.get("daily_limit"))
    except (OSError, ValueError, TypeError):
        pass
    return None


def _write_config_daily_limit(value: int) -> None:
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump({"daily_limit": value}, handle)
    os.replace(tmp, CONFIG_PATH)


def _initial_daily_limit() -> int:
    configured = _read_config_daily_limit()
    if configured is not None:
        return configured
    env_value = _valid_daily_limit(_daily_limit_from_env())
    return env_value if env_value is not None else 100


DAILY_LIMIT = _initial_daily_limit()

# Same-conversation mode: the web page keeps its own transcript, so after the
# first turn we send only the new user/tool message(s), never the hard system
# prompt again. Reset on bridge restart, page change, errors, or turn/char cap.
SESSION = {
    "active": False,
    "fingerprints": [],
    "page": "",
    "turns": 0,
    "chars": 0,
}
SESSION_LOCK = threading.Lock()
LAST_PAGE = ""
SESSION_MAX_TURNS = int(os.environ.get("PET_WEB_SESSION_MAX_TURNS", "30") or 30)
SESSION_MAX_CHARS = int(os.environ.get("PET_WEB_SESSION_MAX_CHARS", "120000") or 120000)


JOBS: "queue.Queue[dict]" = queue.Queue()
PENDING: dict[str, dict] = {}
LOCK = threading.Lock()
LAST_POLL_AT = 0.0
LAST_RESULT_AT = 0.0
COMPLETED_JOBS = 0
MESSAGES_TODAY = 0
MESSAGES_DAY = ""

EXTENSION_NOT_CONNECTED_MESSAGE = (
    "browser extension not connected; open https://chat.deepseek.com "
    "and keep the extension tab alive"
)


def extension_connected(now: float | None = None, max_age: float = 12.0) -> bool:
    """True iff the extension polled ``/jobs/next`` within ``max_age`` seconds."""
    current = time.time() if now is None else now
    return bool(LAST_POLL_AT) and (current - LAST_POLL_AT) <= max_age


def _reject_if_no_extension() -> str | None:
    """Return an error message when no browser extension is polling, else None."""
    return None if extension_connected() else EXTENSION_NOT_CONNECTED_MESSAGE


def _image_url_from_part(part: dict) -> str | None:
    """Return the image URL of an OpenAI-style content part, if any."""
    kind = part.get("type")
    if kind == "image_url":
        value = part.get("image_url")
        if isinstance(value, dict):
            value = value.get("url")
        if isinstance(value, str):
            return value
        value = part.get("url")
        return value if isinstance(value, str) else None
    if kind == "image":
        for key in ("url", "image_url"):
            value = part.get(key)
            if isinstance(value, dict):
                value = value.get("url")
            if isinstance(value, str):
                return value
    return None


def _parse_image_data_url(url: str) -> dict | None:
    """Convert ``data:image/<mime>;base64,<data>`` into ``{mime, base64}``."""
    if not isinstance(url, str):
        return None
    match = _DATA_URL_RE.match(url.strip())
    if not match:
        return None
    mime = match.group(1).lower()
    base64_data = re.sub(r"\s+", "", match.group(2))
    if not base64_data or len(base64_data) > MAX_IMAGE_BASE64_CHARS:
        return None
    return {"mime": mime, "base64": base64_data}


def extract_images(body: dict) -> list[dict]:
    """Collect inline base64 images from OpenAI chat message content parts."""
    images: list[dict] = []
    messages = body.get("messages") if isinstance(body, dict) else None
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            image = _parse_image_data_url(_image_url_from_part(part) or "")
            if image is None:
                continue
            images.append(image)
            if len(images) >= MAX_IMAGES:
                return images
    return images


def flatten_messages(body: dict) -> str:
    system = body.get("system") or ""
    parts: list[str] = []
    if system:
        parts.append("【系统设定】\n" + str(system).strip())
    for message in body.get("messages") or []:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user")
        content = message.get("content")
        has_image = False
        if isinstance(content, list):
            texts: list[str] = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                if _image_url_from_part(part) is not None:
                    has_image = True
                    continue
                text = part.get("text")
                if text:
                    texts.append(str(text))
            content = " ".join(texts)
        content = str(content or "").strip()
        if has_image:
            content = (
                content + "\n" + IMAGE_PLACEHOLDER if content else IMAGE_PLACEHOLDER
            )
        if not content:
            continue
        if role == "system":
            parts.append("【系统设定】\n" + content)
        elif role == "assistant":
            parts.append("【助手】\n" + content)
        elif role == "tool":
            parts.append("【工具结果】\n" + content)
        else:
            parts.append("【用户】\n" + content)
    parts.append("请只输出【助手】这一轮要对用户说的话。不要复述设定，不要输出思考过程。")
    return "\n\n".join(parts)[:MAX_PROMPT_CHARS]


def _message_fingerprint(message: dict) -> str:
    try:
        raw = json.dumps(message, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        raw = str(message)
    return hashlib.sha256(raw.encode("utf-8", "ignore")).hexdigest()[:16]


def _overlap(old: list[str], new: list[str]) -> int:
    """Largest suffix of ``old`` that is a prefix of ``new``.

    The gateway trims web history to the last 8 messages, so the new list can
    drop old entries; this alignment still finds the carried-over tail.
    """
    for size in range(min(len(old), len(new)), 0, -1):
        if old[-size:] == new[:size]:
            return size
    return 0


def _reset_session() -> None:
    with SESSION_LOCK:
        SESSION.update({
            "active": False,
            "fingerprints": [],
            "page": "",
            "turns": 0,
            "chars": 0,
        })


def _commit_session(candidate: dict | None, page: str = "") -> None:
    """Commit a session only after the browser produced a real reply."""
    if not isinstance(candidate, dict):
        return
    if candidate.get("commit") is False:
        # Boot wake / compression requests intentionally run without tools.
        # They must not become the session base, or later deltas would never
        # receive the tool-bearing hard prompt.
        _reset_session()
        return
    with SESSION_LOCK:
        SESSION.update({
            "active": True,
            "fingerprints": list(candidate.get("fingerprints") or []),
            "page": page or str(candidate.get("page") or ""),
            "turns": int(candidate.get("turns") or 0),
            "chars": int(candidate.get("chars") or 0),
        })


def _plan_session(body: dict, page: str) -> tuple[str, list[dict], bool, dict]:
    """Return (prompt, images, new_chat, candidate session state).

    First turn (or after a reset) sends the full system + visible history. On
    later turns the DeepSeek page already has the transcript, so only new
    user/tool messages are sent; assistant messages are already in the page.
    """
    messages = body.get("messages") or []
    fingerprints = [
        _message_fingerprint(m) for m in messages if isinstance(m, dict)
    ]
    current_page = page or LAST_PAGE
    # Boot wake / compression explicitly disable tools. Such a turn must not
    # become the long-lived conversation base: the next real turn would send
    # only a delta and the model would never see the tool-bearing hard prompt.
    ephemeral = body.get("tools_enabled") is False
    with SESSION_LOCK:
        old_fingerprints = list(SESSION["fingerprints"])
        page_changed = bool(
            current_page and SESSION["page"] and current_page != SESSION["page"]
        )
        capped = (
            SESSION["turns"] >= SESSION_MAX_TURNS
            or SESSION["chars"] >= SESSION_MAX_CHARS
        )
        full = ephemeral or (not SESSION["active"]) or page_changed or capped
        if not full:
            if len(fingerprints) > 1 and len(set(fingerprints)) == 1 and old_fingerprints:
                # Proactive life rounds all carry the same text ("生命在继续")
                # and the gateway keeps sending the last 8 turns. Every entry
                # has the same fingerprint, so suffix alignment is meaningless:
                # it would either resend all 8 or (once old and new lengths
                # match) look like a duplicate and resend the full system
                # prompt. Only the final entry is this round's new turn.
                carried = len(messages) - 1
            else:
                carried = _overlap(old_fingerprints, fingerprints)
            delta = [
                m for m in messages[carried:]
                if isinstance(m, dict) and str(m.get("role") or "user") != "assistant"
            ]
            if delta:
                prompt = flatten_messages({"messages": delta})
                images = extract_images({"messages": delta})
                candidate = {
                    "fingerprints": fingerprints,
                    "page": current_page or SESSION["page"],
                    "turns": SESSION["turns"] + 1,
                    "chars": SESSION["chars"] + len(prompt),
                    "mode": "delta",
                    "commit": True,
                }
                return prompt, images, False, candidate
            # Nothing new to say. This is the normal case for proactive
            # "生命在继续" rounds: the gateway trims history to the last 8
            # turns and every turn's text is identical, so a suffix overlap
            # finds all 8 and the new turn looks like a duplicate. Falling
            # back to the full prompt here re-sends the whole system prompt
            # on every life tick (minutes apart, 8k chars each), pollutes the
            # page and makes every reply slower. The last user message is the
            # new turn even when it is textually identical to the previous
            # one; send only that.
            last = messages[-1] if messages else None
            if isinstance(last, dict) and str(last.get("role") or "user") != "assistant":
                delta = [last]
                prompt = flatten_messages({"messages": delta})
                images = extract_images({"messages": delta})
                if prompt.strip() or images:
                    candidate = {
                        "fingerprints": fingerprints,
                        "page": current_page or SESSION["page"],
                        "turns": SESSION["turns"] + 1,
                        "chars": SESSION["chars"] + len(prompt),
                        "mode": "delta-repeat",
                        "commit": True,
                    }
                    return prompt, images, False, candidate
            # A genuine retry with no new user turn: start a fresh
            # conversation rather than appending a duplicate full prompt to
            # whatever chat the page happens to be on.
        # First turn of a new chat. When every history entry is the same
        # proactive prompt (the gateway keeps the last 8 life ticks), send a
        # single copy: the page needs the current turn, not eight identical
        # instruction blocks that bloat the context and confuse the model.
        full_body = body
        if len(fingerprints) > 1 and len(set(fingerprints)) == 1:
            full_body = {
                "system": body.get("system"),
                "messages": [messages[-1]],
            }
        prompt = flatten_messages(full_body)
        images = extract_images(full_body)
        candidate = {
            "fingerprints": fingerprints,
            "page": current_page,
            "turns": 1,
            "chars": len(prompt),
            "mode": "full",
            "commit": not ephemeral,
        }
        return prompt, images, True, candidate


def build_job(body: dict) -> dict | None:
    """Build the queued browser job, or None when there is nothing to send.

    Images travel beside the prompt, never inside it: the base64 payload is
    delivered to the extension through the job's ``images`` field, while the
    prompt only carries a human-readable placeholder. The private
    ``_session`` key is removed by the HTTP handler and committed only after
    the browser replies successfully.
    """
    prompt, images, new_chat, candidate = _plan_session(body, LAST_PAGE)
    if not prompt.strip() and not images:
        return None
    return {
        "id": "chatcmpl-" + uuid.uuid4().hex[:16],
        "prompt": prompt,
        "images": images,
        "new_chat": new_chat,
        "_session": candidate,
    }


def _chunk(text: str):
    for index in range(0, len(text), 18):
        yield text[index:index + 18]


def _bump_message_counter() -> None:
    global MESSAGES_TODAY, MESSAGES_DAY
    day = time.strftime("%Y-%m-%d")
    if day != MESSAGES_DAY:
        MESSAGES_DAY = day
        MESSAGES_TODAY = 0
    MESSAGES_TODAY += 1
    try:
        target = os.path.join(os.path.expanduser("~"), ".pet")
        os.makedirs(target, exist_ok=True)
        with open(os.path.join(target, "deepseek-web-usage.json"), "w", encoding="utf-8") as handle:
            json.dump({"day": day, "count": MESSAGES_TODAY}, handle)
    except OSError:
        pass


def _load_message_counter() -> None:
    global MESSAGES_TODAY, MESSAGES_DAY
    path = os.path.join(os.path.expanduser("~"), ".pet", "deepseek-web-usage.json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if data.get("day") == time.strftime("%Y-%m-%d"):
            MESSAGES_DAY = data["day"]
            MESSAGES_TODAY = int(data.get("count") or 0)
    except (OSError, ValueError, TypeError):
        pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, fmt, *args):  # keep the terminal readable
        return

    def _json(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_json(self, max_bytes: int = 2_000_000) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > max_bytes:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8", "ignore"))
        except Exception:
            return {}

    # ------------------------------------------------------------------
    def do_GET(self):
        global LAST_POLL_AT, LAST_PAGE
        parsed = urlparse(self.path)
        path = parsed.path
        page = (parse_qs(parsed.query).get("page") or [""])[0]
        if path == "/jobs/next" and page:
            LAST_PAGE = page
        if path == "/health":
            now = time.time()
            last = LAST_POLL_AT
            age = (now - last) if last else None
            return self._json(200, {
                "ok": True,
                "queued": JOBS.qsize(),
                "pending": len(PENDING),
                "extension_connected": extension_connected(now),
                "last_poll_seconds": round(age, 1) if age is not None else None,
                "completed": COMPLETED_JOBS,
                "messages_today": MESSAGES_TODAY,
                "messages_day": MESSAGES_DAY,
                "daily_limit": DAILY_LIMIT,
                "remaining": max(0, DAILY_LIMIT - MESSAGES_TODAY) if DAILY_LIMIT > 0 else None,
                "session_active": SESSION["active"],
                "session_turns": SESSION["turns"],
                "session_page": SESSION["page"],
                "last_page": LAST_PAGE,
                "model": "deepseek-web",
            })
        if path == "/v1/models":
            return self._json(200, {
                "object": "list",
                "data": [{
                    "id": "deepseek-web",
                    "object": "model",
                    "created": 0,
                    "owned_by": "deepseek-web-bridge",
                }],
            })
        if path == "/jobs/next":
            LAST_POLL_AT = time.time()
            try:
                job = JOBS.get(timeout=5.0)
            except queue.Empty:
                LAST_POLL_AT = time.time()
                self.send_response(204)
                self.end_headers()
                return
            return self._json(200, job)
        if path == "/mock":
            raw = MOCK_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        return self._json(200, {"ok": True, "service": "deepseek-web-bridge"})

    # ------------------------------------------------------------------
    def do_POST(self):
        global LAST_RESULT_AT, COMPLETED_JOBS, DAILY_LIMIT
        path = urlparse(self.path).path
        # CSRF guard: a webpage can only send a cross-origin JSON POST after
        # a CORS preflight, and this server never answers OPTIONS. Rejecting
        # non-JSON also blocks form/text-plain "simple" requests from turning
        # the user's logged-in browser into a free prompt relay.
        if "application/json" not in (self.headers.get("Content-Type") or "").lower():
            return self._json(415, {"ok": False, "error": "Content-Type must be application/json"})
        if path.startswith("/jobs/") and path.endswith("/result"):
            job_id = path[len("/jobs/"):-len("/result")]
            body = self._read_json()
            with LOCK:
                pending = PENDING.get(job_id)
            if pending is None:
                return self._json(404, {"ok": False, "error": "unknown job"})
            pending["text"] = str(body.get("text") or "")
            pending["error"] = str(body.get("error") or "")
            pending["page"] = str(body.get("page") or "")
            pending["event"].set()
            LAST_RESULT_AT = time.time()
            COMPLETED_JOBS += 1
            return self._json(200, {"ok": True})

        if path == "/session/reset":
            _reset_session()
            return self._json(200, {"ok": True, "session_active": False})

        if path == "/config":
            body = self._read_json()
            value = _valid_daily_limit(body.get("daily_limit"))
            if value is None:
                return self._json(400, {
                    "error": {
                        "message": (
                            f"daily_limit must be an integer between "
                            f"{MIN_DAILY_LIMIT} and {MAX_DAILY_LIMIT}"
                        )
                    }
                })
            try:
                _write_config_daily_limit(value)
            except OSError as exc:
                return self._json(500, {"error": {"message": f"could not save config: {exc}"}})
            with LOCK:
                DAILY_LIMIT = value
            return self._json(200, {"ok": True, "daily_limit": DAILY_LIMIT})

        if path == "/v1/chat/completions":
            # Only chat requests may carry inline screenshots; the other
            # endpoints keep the original 2 MB cap.
            body = self._read_json(MAX_BODY_BYTES)
            job = build_job(body)
            if job is None:
                return self._json(400, {"error": {"message": "empty prompt"}})
            # Fail fast when no extension is polling: otherwise the request
            # would sit in JOBS for the full JOB_TIMEOUT_SECONDS (240s) before
            # giving up. Do this before reserving a message / pending job.
            reject = _reject_if_no_extension()
            if reject:
                return self._json(503, {"error": {"message": reject}})
            if DAILY_LIMIT > 0 and MESSAGES_TODAY >= DAILY_LIMIT:
                return self._json(429, {
                    "error": {
                        "message": (
                            f"今日网页消息已达上限（{DAILY_LIMIT} 条）。"
                            "请明天再试，或在设置中切回 API/本地模型。"
                        )
                    }
                })
            job_id = job["id"]
            session_candidate = job.pop("_session", None)
            pending = {
                "event": threading.Event(),
                "text": "",
                "error": "",
                "session": session_candidate,
            }
            with LOCK:
                PENDING[job_id] = pending
            _bump_message_counter()
            JOBS.put(job)
            # Keep the long timeout for a mid-job disconnect: while the
            # extension is busy processing we cannot tell "busy" from "gone",
            # so only the initial connect check above can fail fast.
            if not pending["event"].wait(JOB_TIMEOUT_SECONDS):
                with LOCK:
                    PENDING.pop(job_id, None)
                _reset_session()
                return self._json(504, {"error": {"message": "browser did not answer in time"}})
            with LOCK:
                PENDING.pop(job_id, None)
            if pending["error"]:
                _reset_session()
                return self._json(502, {"error": {"message": pending["error"]}})
            text = pending["text"]
            if text.strip():
                _commit_session(pending.get("session"), str(pending.get("page") or ""))
            else:
                _reset_session()
            if body.get("stream"):
                return self._stream(job_id, text, body.get("model") or "deepseek-web")
            return self._json(200, {
                "id": job_id,
                "object": "chat.completion",
                "created": int(time.time()),
                "model": body.get("model") or "deepseek-web",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            })

        return self._json(404, {"error": {"message": "not found"}})

    def _stream(self, job_id: str, text: str, model: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        created = int(time.time())
        try:
            for piece in _chunk(text):
                payload = {
                    "id": job_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [{
                        "index": 0,
                        "delta": {"content": piece},
                        "finish_reason": None,
                    }],
                }
                self.wfile.write(
                    ("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode("utf-8")
                )
                self.wfile.flush()
                time.sleep(0.012)
            final = {
                "id": job_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
            self.wfile.write(
                ("data: " + json.dumps(final, ensure_ascii=False) + "\n\n").encode("utf-8")
            )
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


MOCK_HTML = """<!doctype html>
<meta charset="utf-8">
<title>DeepSeek Web Bridge Mock</title>
<style>
body{font:14px system-ui;background:#111;color:#eee;padding:24px;max-width:820px;margin:auto}
#log{white-space:pre-wrap;background:#1b1b1b;padding:12px;border-radius:8px;min-height:160px}
textarea{width:100%;height:72px;background:#1b1b1b;color:#eee;border:1px solid #444;border-radius:8px;padding:8px}
button{margin:8px 4px 8px 0;padding:6px 12px;border-radius:6px;border:1px solid #555;background:#2a2a2a;color:#eee}
</style>
<h3>DeepSeek Web Bridge — mock page</h3>
<button id="newchat">New chat</button>
<div id="log"></div>
<textarea id="chat-input" placeholder="Type a prompt…"></textarea>
<button aria-label="发送" id="send">Send</button>
<script>
const log = document.getElementById('log');
const input = document.getElementById('chat-input');
function add(role, text) {
  const div = document.createElement('div');
  div.className = 'ds-markdown ds-markdown--block';
  div.dataset.role = role;
  div.textContent = text;
  log.appendChild(div);
  return div;
}
document.getElementById('newchat').onclick = () => { log.innerHTML = ''; };
document.getElementById('send').onclick = () => {
  const text = input.value.trim(); if (!text) return;
  input.value = '';
  add('user', '用户：' + text);
  const stop = document.createElement('button');
  stop.className = 'stop'; stop.textContent = 'Stop';
  document.body.appendChild(stop);
  setTimeout(() => {
    add('assistant', '回声：' + text);
    stop.remove();
  }, 900);
};
</script>
"""


if __name__ == "__main__":
    _load_message_counter()
    print(f"deepseek-web bridge on http://{HOST}:{PORT}")
    print("load extension/ unpacked, open https://chat.deepseek.com, then POST /v1/chat/completions")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
