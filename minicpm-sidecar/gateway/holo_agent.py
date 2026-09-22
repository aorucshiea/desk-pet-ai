"""Holo desktop-control loop, ported from the proven holo-gui-agent prototype.

Holo (holo3.1-35b-a3b) is H Company's vision-grounded GUI-agent model over an
OpenAI-compatible endpoint. This module gives the pet HANDS: the pet's own
language model decides the WHAT (a high-level task string, e.g. "打开记事本
输入 Hello World"); Holo decides the HOW, one GUI action per step against the
live desktop screenshot.

Protocol (matching the working prototype exactly):
  - system prompt = SYSTEM_PROMPT + <task> block + <output_format> schema
  - each step pre-appends an <observation> message with a fresh screenshot
  - assistant history carries the raw JSON step (reasoning channel is dropped)
  - tool results come back as <tool_output> user messages
  - only the newest 3 screenshots stay embedded; older ones are evicted
  - extra_body: structured_outputs.json + enable_thinking + reasoning_effort

Config (env):
  MINICPM_HOLO_API_KEY   — required; H Company key (hk-...)
  MINICPM_HOLO_BASE_URL  — default https://api.hcompany.ai/v1/
  MINICPM_HOLO_MODEL     — default holo3-1-35b-a3b

Safety:
  - pyautogui FAILSAFE stays ON — slam the mouse into a corner to abort.
  - Every task run passes the gateway's consent gate (run-task level, not
    per-step: one grant authorizes the whole task the user approved).
  - Click coordinates are clamped ≥12px from screen edges.
  - pyautogui / pyperclip import lazily so headless CI can still import the
    module and test the pure logic (schema, trimming, scaling, prompts).
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import time
from typing import Any, AsyncGenerator, Dict, List, Literal, Optional, Union

import httpx
from pydantic import BaseModel, Field

from .log_setup import get_logger

log = get_logger()

DEFAULT_BASE_URL = "https://api.hcompany.ai/v1/"
DEFAULT_MODEL = "holo3-1-35b-a3b"
MAX_IMAGES = 3          # screenshots kept embedded in history
MAX_STEPS = 18
MAX_IMAGE_WIDTH = 1600  # px — downscale for upload
JPEG_QUALITY = 85
EDGE_MARGIN = 12        # px clamp from screen edges (corner failsafe zone)


# ── Step schema (faithful port of tools.py from the prototype) ───────────────


class ClickArgs(BaseModel):
    """Click the left mouse button at (x, y)."""

    tool_name: Literal["click"]
    element: str = Field(description="Detailed description of the target UI element")
    x: int = Field(ge=0, le=1000, description="X in [0,1000], normalized to the screenshot")
    y: int = Field(ge=0, le=1000)


class DoubleClickArgs(BaseModel):
    """Double-click the left mouse button at (x, y)."""

    tool_name: Literal["double_click"]
    element: str = Field(description="Detailed description of the target UI element")
    x: int = Field(ge=0, le=1000)
    y: int = Field(ge=0, le=1000)


class RightClickArgs(BaseModel):
    """Right-click at (x, y) to open a context menu."""

    tool_name: Literal["right_click"]
    element: str = Field(description="Detailed description of the target UI element")
    x: int = Field(ge=0, le=1000)
    y: int = Field(ge=0, le=1000)


class WriteArgs(BaseModel):
    """Type text into the currently focused input. Unicode OK (clipboard paste)."""

    tool_name: Literal["write"]
    content: str = Field(description="Exact text to type. Click the target field first if not focused.")
    press_enter: bool = Field(default=False, description="Press Enter after typing")


class KeyArgs(BaseModel):
    """Press a key or key combination, e.g. 'enter', 'ctrl+s', 'alt+f4', 'win', 'tab'."""

    tool_name: Literal["key"]
    combo: str = Field(description="Key names joined by '+', e.g. 'ctrl+shift+esc'")


class ScrollArgs(BaseModel):
    """Scroll the window under the mouse cursor."""

    tool_name: Literal["scroll"]
    clicks: int = Field(description="Positive scrolls up, negative scrolls down")


class WaitArgs(BaseModel):
    """Wait for the screen to settle (app launch, page load), then look again."""

    tool_name: Literal["wait"]
    seconds: float = Field(default=1.5, ge=0.2, le=10)


class DoneArgs(BaseModel):
    """Task is complete (or impossible): report the outcome and stop."""

    tool_name: Literal["done"]
    success: bool = Field(default=True, description="False if the task could not be completed")
    content: str = Field(description="Final answer / result summary for the user")


StepToolCall = Union[ClickArgs, DoubleClickArgs, RightClickArgs, WriteArgs, KeyArgs, ScrollArgs, WaitArgs, DoneArgs]


class Step(BaseModel):
    note: Optional[str] = Field(
        default=None,
        description="Durable facts to remember across steps (names, paths, on-screen values). null if nothing new.",
    )
    thought: str = Field(description="One-line plan for this step")
    tool_call: StepToolCall


SYSTEM_PROMPT = """You are a Windows desktop GUI agent. You see a screenshot of the user's screen and control \
the real mouse and keyboard one step at a time until the user's task is complete.

Coordinate convention:
- The observation image maps to coordinates in [0, 1000] on both axes, origin at the top-left corner.
- Aim at the visual center of the target element.

Rules:
- One tool call per step. After each action you receive a fresh screenshot as the next observation.
- Before clicking, verify from the CURRENT screenshot that the target is where you think it is.
- Prefer keyboard-first flows on Windows: launch apps via the Run dialog (`key` win+r, then write the app name, \
then `key` enter), or press `win` and type the app name directly — both beat hunting through menus.
- Scrolling inside the Start menu often does not work; type to search instead of scrolling.
- To type text: first click the input field so it has focus, then use write.
- Prefer keyboard shortcuts over menus when reliable (e.g. ctrl+s to save).
- If an app is still loading, use wait instead of clicking blindly.
- Keep `note` updated with anything you will need later (window titles, file names, values you read on screen); \
the reasoning channel between steps is discarded, so `note` and `content` are your only memory.
- Scroll or use the Start menu / taskbar search (`win` key then type) when the app you need is not visible.
- When the task is finished (or truly impossible), call done with a concise summary.

Be careful and conservative: never delete files, never send messages/emails, and never confirm destructive dialogs \
unless the task explicitly requires it."""


def output_format_block() -> str:
    return (
        "\n\n<output_format>\n```json\n"
        + json.dumps(Step.model_json_schema(), ensure_ascii=False)
        + "\n```\n</output_format>"
    )


def build_system_prompt(task: str) -> str:
    return SYSTEM_PROMPT + f"\n\n<task>\n{task}\n</task>" + output_format_block()


# ── Screen capture / execution ───────────────────────────────────────────────


def enable_dpi_awareness() -> None:
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE_V2
        except Exception:
            pass


def grab_screen() -> tuple[bytes, tuple[int, int]]:
    """Capture the primary monitor → (jpeg bytes, (phys_w, phys_h))."""
    import mss
    from PIL import Image

    enable_dpi_awareness()
    with mss.mss() as sct:
        monitor = sct.monitors[1]
        shot = sct.grab(monitor)
        img = Image.frombytes("RGB", shot.size, shot.rgb)
    phys = img.size
    if phys[0] > MAX_IMAGE_WIDTH:
        img = img.resize((MAX_IMAGE_WIDTH, round(phys[1] * MAX_IMAGE_WIDTH / phys[0])))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_QUALITY)
    return buf.getvalue(), phys


def to_data_uri(jpeg: bytes) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")


def scale_point(x: int, y: int, phys: tuple[int, int]) -> tuple[int, int]:
    w, h = phys
    return (
        min(max(round(x / 1000 * w), EDGE_MARGIN), w - EDGE_MARGIN),
        min(max(round(y / 1000 * h), EDGE_MARGIN), h - EDGE_MARGIN),
    )


def execute_step(step: Step, phys: tuple[int, int]) -> str:
    """Perform ONE Holo step on the real desktop. Returns a result string."""
    import pyautogui
    import pyperclip

    tc = step.tool_call
    if isinstance(tc, ClickArgs):
        x, y = scale_point(tc.x, tc.y, phys)
        pyautogui.click(x, y)
        return f"clicked ({x}, {y})"
    if isinstance(tc, DoubleClickArgs):
        x, y = scale_point(tc.x, tc.y, phys)
        pyautogui.doubleClick(x, y)
        return f"double-clicked ({x}, {y})"
    if isinstance(tc, RightClickArgs):
        x, y = scale_point(tc.x, tc.y, phys)
        pyautogui.rightClick(x, y)
        return f"right-clicked ({x}, {y})"
    if isinstance(tc, WriteArgs):
        text = tc.content
        if all(ord(c) < 128 for c in text):
            pyautogui.typewrite(text, interval=0.03)
        else:
            old = ""
            try:
                old = pyperclip.paste() or ""
            except Exception:
                pass
            pyperclip.copy(text)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(0.2)
            if old:
                try:
                    pyperclip.copy(old)
                except Exception:
                    pass
        if tc.press_enter:
            pyautogui.press("enter")
        return f"wrote {len(text)} chars" + (" +enter" if tc.press_enter else "")
    if isinstance(tc, KeyArgs):
        tokens = [t.strip() for t in tc.combo.split("+") if t.strip()]
        if len(tokens) == 1:
            pyautogui.press(tokens[0])
        else:
            pyautogui.hotkey(*tokens)
        return f"pressed {tc.combo}"
    if isinstance(tc, ScrollArgs):
        pyautogui.scroll(int(tc.clicks))
        return f"scrolled {tc.clicks} clicks"
    if isinstance(tc, WaitArgs):
        secs = min(float(tc.seconds), 10.0)
        time.sleep(secs)
        return f"waited {secs}s"
    return "no-op"


def render_step(step: Step) -> str:
    """Human-readable one-liner for logs / UI push."""
    tc = step.tool_call
    name = tc.tool_name
    if isinstance(tc, (ClickArgs, DoubleClickArgs, RightClickArgs)):
        return f"{name.upper()} [{tc.element[:60]}] @ ({tc.x}, {tc.y})"
    if isinstance(tc, WriteArgs):
        preview = tc.content if len(tc.content) <= 40 else tc.content[:37] + "..."
        return f"WRITE {preview!r}"
    if isinstance(tc, KeyArgs):
        return f"KEY {tc.combo}"
    if isinstance(tc, ScrollArgs):
        return f"SCROLL {tc.clicks}"
    if isinstance(tc, WaitArgs):
        return f"WAIT {tc.seconds}s"
    if isinstance(tc, DoneArgs):
        return f"DONE ({'OK' if tc.success else 'FAILED'}) {tc.content[:80]}"
    return name


# ── Conversation messages ────────────────────────────────────────────────────


def observation_message(jpeg: bytes) -> Dict[str, Any]:
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": "<observation>\n"},
            {"type": "image_url", "image_url": {"url": to_data_uri(jpeg)}},
            {"type": "text", "text": "\n</observation>"},
        ],
    }


def trim_to_last_n_images(messages: List[Dict[str, Any]], n: int = MAX_IMAGES) -> None:
    seen = 0
    for msg in reversed(messages):
        if msg.get("role") != "user" or not isinstance(msg.get("content"), list):
            continue
        for chunk in msg["content"]:
            if chunk.get("type") != "image_url":
                continue
            seen += 1
            if seen > n:
                chunk["type"] = "text"
                chunk["text"] = "[screenshot evicted]"
                chunk.pop("image_url", None)


# ── API call ──────────────────────────────────────────────────────────────────


async def call_holo(
    http: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    model: str,
    messages: List[Dict[str, Any]],
) -> Step:
    """One structured-output round trip. Retries 429/5xx with backoff."""
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.8,
        "extra_body": {
            "structured_outputs": {"json": Step.model_json_schema()},
            "chat_template_kwargs": {"enable_thinking": True},
            "reasoning_effort": "medium",
        },
    }
    delay = 2.0
    last_exc: Optional[Exception] = None
    for attempt in range(6):
        try:
            resp = await http.post(
                base_url.rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json=payload,
            )
            if resp.status_code in (429, 500, 502, 503, 529):
                raise httpx.HTTPStatusError(
                    f"holo busy: HTTP {resp.status_code}", request=resp.request, response=resp
                )
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            return Step.model_validate_json(content)
        except (httpx.HTTPStatusError, json.JSONDecodeError, ValueError, httpx.HTTPError) as exc:
            last_exc = exc
            status = getattr(exc.response, "status_code", None) if getattr(exc, "response", None) is not None else None
            retriable = status in (429, 500, 502, 503, 529) or status is None
            # JSON/validation errors are model-side noise worth one retry too
            if isinstance(exc, (json.JSONDecodeError, ValueError)):
                retriable = attempt < 2
            if not retriable or attempt == 5:
                raise
        except Exception as exc:  # noqa: BLE001 — validation errors land here (pydantic)
            last_exc = exc
            if attempt >= 2:
                raise
        log.info("holo retry %d after %s (%.0fs)", attempt + 1, type(last_exc).__name__, delay)
        await asyncio.sleep(delay)
        delay = min(delay * 2, 60)
    raise RuntimeError(f"holo request failed: {last_exc}")


# ── Task loop ─────────────────────────────────────────────────────────────────


async def iter_task(
    task: str,
    *,
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    model: str,
    max_steps: int = MAX_STEPS,
    dry_run: bool = False,
) -> AsyncGenerator[Dict[str, Any], None]:
    """Yield one event dict per step:
    {step, thought, note, action_line, action_json, result, done?, failed?}
    """
    messages: List[Dict[str, Any]] = [{"role": "system", "content": build_system_prompt(task)}]
    for i in range(1, max_steps + 1):
        jpeg, phys = grab_screen()
        messages.append(observation_message(jpeg))
        trim_to_last_n_images(messages)

        step = await call_holo(client, base_url, api_key, model, messages)
        messages.append({"role": "assistant", "content": step.model_dump_json()})

        event: Dict[str, Any] = {
            "step": i,
            "thought": step.thought,
            "note": step.note,
            "action_line": render_step(step),
            "action_json": json.loads(step.tool_call.model_dump_json()),
        }
        if step.tool_call.tool_name == "done":
            event["result"] = "(done)"
            event["done"] = True
            event["success"] = bool(step.tool_call.success)  # type: ignore[attr-defined]
            event["answer"] = step.tool_call.content  # type: ignore[attr-defined]
            yield event
            return

        if dry_run:
            event["result"] = "(dry-run, not executed)"
        else:
            try:
                event["result"] = execute_step(step, phys)
            except Exception as exc:  # noqa: BLE001 — includes pyautogui.FailSafeException
                event["result"] = f"execution error: {exc}"
                event["failed"] = True
                yield event
                return
        yield event

        messages.append({
            "role": "user",
            "content": f'<tool_output tool="{step.tool_call.tool_name}">\n{event["result"]}\n</tool_output>',
        })
        await asyncio.sleep(0.8)

    yield {
        "step": max_steps,
        "action_line": "FAILED (max steps)",
        "result": "max steps reached without done",
        "failed": True,
    }


class HoloRunner:
    """Mutable run state for /api/holo/* — one task at a time."""

    def __init__(self) -> None:
        self.status: str = "idle"  # idle | running | done | failed
        self.current_task: str = ""
        self.steps: List[Dict[str, Any]] = []
        self.answer: Optional[str] = None
        self.error: Optional[str] = None
        self._task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    def snapshot(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "task": self.current_task,
            "step_count": len(self.steps),
            "steps": self.steps[-8:],
            "answer": self.answer,
            "error": self.error,
            "configured": bool(os.environ.get("MINICPM_HOLO_API_KEY")),
        }

    def configured(self) -> bool:
        return bool(os.environ.get("MINICPM_HOLO_API_KEY"))

    async def run_background(
        self,
        task: str,
        max_steps: int = MAX_STEPS,
        *,
        on_done=None,  # async fn(ok: bool, summary: str) — e.g. write memory + push bridge
    ) -> Dict[str, Any]:
        """Start a background Holo task. Returns immediately with a status ack."""
        if not self.configured():
            return {"ok": False, "error": "MINICPM_HOLO_API_KEY not set"}
        async with self._lock:
            if self._task and not self._task.done():
                return {"ok": False, "error": "a Holo task is already running"}
            self.status = "running"
            self.current_task = task
            self.steps = []
            self.answer = None
            self.error = None

            async def _drive() -> None:
                base_url = os.environ.get("MINICPM_HOLO_BASE_URL", DEFAULT_BASE_URL)
                api_key = os.environ.get("MINICPM_HOLO_API_KEY", "")
                model = os.environ.get("MINICPM_HOLO_MODEL", DEFAULT_MODEL)
                ok = False
                summary = ""
                try:
                    async with httpx.AsyncClient(timeout=httpx.Timeout(180.0)) as client:
                        async for event in iter_task(
                            task,
                            client=client,
                            base_url=base_url,
                            api_key=api_key,
                            model=model,
                            max_steps=max_steps,
                        ):
                            self.steps.append(event)
                            log.info("holo step %d: %s -> %s", event.get("step"), event.get("action_line"), event.get("result"))
                            if event.get("done"):
                                ok = bool(event.get("success"))
                                summary = str(event.get("answer") or "")
                                self.answer = summary
                                self.status = "done" if ok else "failed"
                                if not ok:
                                    self.error = summary
                                break
                            if event.get("failed"):
                                ok = False
                                summary = str(event.get("result", "step failed"))
                                self.error = summary
                                self.status = "failed"
                                break
                        else:
                            pass
                    if self.status == "running":  # loop ended without done
                        self.status = "failed"
                        self.error = self.error or "ended without done"
                        summary = self.error
                except Exception as exc:  # noqa: BLE001
                    log.exception("holo run crashed")
                    self.status = "failed"
                    self.error = str(exc)
                    summary = self.error
                if on_done is not None:
                    try:
                        await on_done(ok, f"任务：{task}\n结果：{summary}")
                    except Exception:  # noqa: BLE001
                        log.exception("holo on_done hook failed")

            self._task = asyncio.get_running_loop().create_task(_drive())
        return {"ok": True, "status": "running", "task": task}

    async def wait(self, timeout: float = 600.0) -> Dict[str, Any]:
        """Block until the current task ends (or timeout). Returns snapshot."""
        t = self._task
        if t is None:
            return self.snapshot()
        try:
            await asyncio.wait_for(asyncio.shield(t), timeout)
        except asyncio.TimeoutError:
            return {**self.snapshot(), "error": (self.error or "wait timeout — task still running")}
        except Exception:
            pass
        return self.snapshot()

    def cancel(self) -> Dict[str, Any]:
        if self._task and not self._task.done():
            self._task.cancel()
            self.status = "failed"
            self.error = "cancelled by user"
            return {"ok": True}
        return {"ok": False, "error": "no running task"}
