/**
 * world-geometry.js — 桌面刚体世界的地形输入（02 前瞻 P0）。
 *
 * 职责：轮询 OS，把「地板在哪」变成一条事件流。P0 的地形只有两个来源：
 *   1. 主屏工作区底边（永远的地板，兜底）
 *   2. 任务栏（自动隐藏时的弹出/收回 → 地板抬升/降下）
 *
 * Windows 取数（F2-1 spike 实测钉过，2026-10-01）：
 *   - `GetWindowRect(Shell_TrayWnd)` 可靠；隐藏基线 = 露出 2px 触发带
 *   - ABM_GETSTATE 的 autohide 位是壳风格常量——**弹出状态只能从矩形推断**
 *   - 弹出/收回动画期矩形往返抖动 → 必须迟滞：top < workBottom-30 判 popped，
 *     top > workBottom-8 判 hidden，中间带保持上一状态
 *   - **坐标系陷阱（真机抓出）**：koffi 的 Win32 原始调用在 Electron 主进程
 *     返回物理像素；Electron screen 返回 DIP。本机 scaleFactor=1.75 时
 *     popped 托盘 top 物理=1716、hidden=1798（÷1.75 = 1027.4 DIP）。
 *
 * 迟滞兜底（机长 2026-10-01 反馈）：shell 侧任务栏可能被自身/托盘图标闪光
 * 等事拉住不收——此时光标其实早离开底边。popped 因此**同时**要求光标仍压
 * 在底边触发带（±6 DIP px）；光标离开连续 >1s 强制判 hidden。
 *
 * 非 win32：地板 = workArea.bottom 常量（无任务栏动态），模块照常工作。
 */

const koffi = (() => {
  try { return require("koffi"); } catch (_) { return null; }
})();

const POLL_MS = 100;
const POP_IN_PX = 30;   // trayTop(DIP) < workBottom - 30 → popped
const POP_OUT_PX = 8;   // trayTop(DIP) > workBottom - 8  → hidden
const CURSOR_EDGE_PX = 6;    // 光标 y 距屏底此范围内算压着触发带
const POP_RELEASE_MS = 1000; // 光标离开触发带持续此时间 → popped 强制解除

function createWorldGeometry(options = {}) {
  const screen = options.screen;                    // Electron screen module
  const isWin = !!options.isWin;
  const pollMs = options.pollMs || POLL_MS;

  let timer = null;
  let floorY = null;
  let taskbarPopped = null;
  let cursorAwaySince = null;
  let user32 = null;
  let shell32 = null;
  let RECT = null;
  let APPBARDATA = null;
  let fns = null;

  if (isWin && koffi) {
    try {
      RECT = koffi.struct("RECT", { left: "long", top: "long", right: "long", bottom: "long" });
      APPBARDATA = koffi.struct("APPBARDATA", {
        cbSize: "uint32", hWnd: "void*", uCallbackMessage: "uint", uEdge: "uint",
        rc: "RECT", lParam: "int64",
      });
      const POINT = koffi.struct("POINT", { x: "long", y: "long" });
      shell32 = koffi.load("shell32.dll");
      user32 = koffi.load("user32.dll");
      fns = {
        SHAppBarMessage: shell32.func("SHAppBarMessage", "uintptr", ["uint", koffi.pointer(APPBARDATA)]),
        FindWindowW: user32.func("FindWindowW", "void*", ["str16", "str16"]),
        GetWindowRect: user32.func("GetWindowRect", "bool", ["void*", koffi.out(koffi.pointer(RECT))]),
        GetCursorPos: user32.func("GetCursorPos", "bool", [koffi.out(koffi.pointer(POINT))]),
      };
      fns.hTray = fns.FindWindowW("Shell_TrayWnd", null);
    } catch (err) {
      console.warn("world-geometry: koffi init failed, static floor only:", err && err.message);
      fns = null;
    }
  }

  // Electron screen is DIP; raw Win32 rects are physical pixels at this DPI
  // awareness. Convert once (F2-1b spike-verified: trayTop 1722 physical ≈
  // 1027 DIP at scaleFactor 1.677 on this machine).
  function toDip(pxY) {
    const scale = screen.getPrimaryDisplay().scaleFactor || 1;
    return pxY / scale;
  }
  const _toDip = toDip; // test hook

  function readFloor() {
    const wa = screen.getPrimaryDisplay().workArea; // {x,y,width,height}
    const workBottom = wa.y + wa.height;
    if (!fns || !fns.hTray) return workBottom;
    try {
      const abd = { cbSize: 48 };
      const st = Number(fns.SHAppBarMessage(0x4, abd));
      if (!(st & 0x1)) { taskbarPopped = false; return workBottom; } // not auto-hide: taskbar already outside workArea
      const rc = {};
      fns.GetWindowRect(fns.hTray, rc);
      const trayTopDip = toDip(rc.top);
      // pop 的双通道判定：矩形迟滞 + 光标离开触发带超时兜底
      const pt = {};
      let cursorOnEdge = true;
      try {
        fns.GetCursorPos(pt);
        cursorOnEdge = toDip(pt.y) >= workBottom - CURSOR_EDGE_PX;
      } catch (_) {}
      const now = Date.now();
      if (cursorOnEdge) cursorAwaySince = null;
      else if (cursorAwaySince === null) cursorAwaySince = now;
      const cursorAwayTooLong = cursorAwaySince !== null && (now - cursorAwaySince) > POP_RELEASE_MS;

      if (taskbarPopped === null) {
        taskbarPopped = trayTopDip < workBottom - POP_IN_PX;
      } else if (taskbarPopped) {
        if (trayTopDip > workBottom - POP_OUT_PX || cursorAwayTooLong) taskbarPopped = false;
      } else if (trayTopDip < workBottom - POP_IN_PX) {
        taskbarPopped = true;
      }
      return taskbarPopped ? trayTopDip : workBottom;
    } catch (_) {
      return taskbarPopped ? wa.y + wa.height : workBottom; // keep last known on error
    }
  }

  function start(onChange) {
    if (timer) return;
    timer = setInterval(() => {
      const y = readFloor();
      if (floorY === null) { floorY = y; return; }
      if (y !== floorY) { floorY = y; try { onChange(y); } catch (_) {} }
    }, pollMs);
    if (timer.unref) timer.unref();
  }

  function stop() { if (timer) clearInterval(timer); timer = null; }
  function getFloorY() { return floorY === null ? readFloor() : floorY; }

  return { start, stop, getFloorY, isTaskbarPopped: () => taskbarPopped === true };
}

module.exports = createWorldGeometry;
