/**
 * world-geometry.js — 桌面刚体世界的地形输入（前瞻 02 · P0 地板 + P1 刚体系统）
 *
 * 地形来源（按优先级）：
 *   1. 主屏工作区底边 —— 永久地板（兜底，不可移除）
 *   2. 任务栏（Shell_TrayWnd）—— 默认刚体；自动隐藏时跟随弹出/收回
 *   3. 用户添加的任意窗口刚体 —— 拾取后每 tick 跟随其矩形；窗口关闭自动移除
 *
 * 刚体判定（floor = 猫脚下方的最高支撑面）：
 *   - 只在猫的水平范围内生效（x ∈ [left, right]）
 *   - 顶面须不高于"脚底 + PUSH_TOL"——防止从下方经过时把猫吸上去，
 *     同时保留"升起的窗口把猫顶起来"的行为
 *
 * Windows 取数注意事项（真机实测钉过）：
 *   - `SHAppBarMessage(ABM_GETSTATE)` 在任务栏弹出动画期间**阻塞 shell 约
 *     200ms**，绝不能放进轮询热路径（2026-10-18 实测：整条轮询被卡住，
 *     猫要等动画放完才被"一次性弹上去"）
 *   - koffi 原始调用返回物理像素；Electron screen 返回 DIP，需 ÷ scaleFactor
 *
 * 非 win32：地板 = workArea.bottom 常量，刚体系统不可用（拾取返回错误）。
 */

const koffi = (() => {
  try { return require("koffi"); } catch (_) { return null; }
})();

const POLL_MS = 16; // 拖动窗口时猫必须即时跟随——33ms 轮询会让窗口顶部先
// 越过猫脚，视觉上"脚陷进窗口标题栏"（2026-10-18 用户实测）。16ms ≈ 60fps，
// Win32 调用开销微秒级，可忽略。
// 弹出判定阈值：取"露出多少像素就认为开始弹出"。任务栏从露出 1px 到完全
// 弹出有 30px 行程——阈值取半高（15px）会让前半段动画期间猫不动，被任务
// 栏盖上才"卡一下再弹"（2026-10-18 用户实测）。取 4px：一开始露头就跟随。
const POP_IN_EARLY_PX = 4;
const POP_OUT_PX = 4;   // trayTop(DIP) > screenBottom - 4  → hidden
const CURSOR_EDGE_PX = 6;    // 光标 y 距屏底此范围内算压着触发带
const POP_RELEASE_MS = 1000; // 光标离开触发带持续此时间 → popped 强制解除
const PUSH_TOL_PX = 48;      // 刚体顶面在猫脚上方多少像素内仍视为"顶起"支撑

function createWorldGeometry(options = {}) {
  const screen = options.screen;                    // Electron screen module
  const isWin = !!options.isWin;
  const pollMs = options.pollMs || POLL_MS;
  const getPetBounds = options.getPetBounds || null; // () => {x,y,width,height} (DIP)
  const getPetFeet = options.getPetFeet || null;     // () => 猫脚实时 y（内容底，DIP）
  const onBodiesChange = options.onBodiesChange || null; // (list) => void 刚体增删后持久化

  let timer = null;
  let floorY = null;
  let taskbarPopped = null;
  let cursorAwaySince = null;
  let user32 = null;
  let shell32 = null;
  let RECT = null;
  let POINT = null;
  let fns = null;

  // 自定义刚体：{ id, key, hwnd, rect(DIP), label }
  const bodies = [];
  let nextBodyId = 1;

  if (isWin && koffi) {
    try {
      // koffi 类型名是进程级全局的——同一进程创建多个实例（测试/热重载）
      // 会 Duplicate type name，整块 koffi 初始化降级为静态地板。加后缀隔离。
      const _sfx = Math.random().toString(36).slice(2, 7);
      RECT = koffi.struct("RECT_" + _sfx, { left: "long", top: "long", right: "long", bottom: "long" });
      POINT = koffi.struct("POINT_" + _sfx, { x: "long", y: "long" });
      shell32 = koffi.load("shell32.dll");
      user32 = koffi.load("user32.dll");
      const dwmapi = koffi.load("dwmapi.dll");
      const kernel32 = koffi.load("kernel32.dll");
      fns = {
        FindWindowW: user32.func("FindWindowW", "void*", ["str16", "str16"]),
        GetWindowRect: user32.func("GetWindowRect", "bool", ["void*", koffi.out(koffi.pointer(RECT))]),
        GetCursorPos: user32.func("GetCursorPos", "bool", [koffi.out(koffi.pointer(POINT))]),
        WindowFromPoint: user32.func("WindowFromPoint", "void*", [POINT]),
        GetAncestor: user32.func("GetAncestor", "void*", ["void*", "uint"]),
        IsWindow: user32.func("IsWindow", "bool", ["void*"]),
        IsWindowVisible: user32.func("IsWindowVisible", "bool", ["void*"]),
        // 顶层窗口链遍历（Z 序）——不用 EnumWindows 回调：koffi 回调在
        // Electron 主进程触发 V8 fatal (IsOnCentralStack)，链式遍历全是
        // 普通同步调用，同样拿全顶层窗口。
        GetTopWindow: user32.func("GetTopWindow", "void*", ["void*"]),
        GetWindow: user32.func("GetWindow", "void*", ["void*", "uint"]),
        GetClassNameW: user32.func("GetClassNameW", "int", ["void*", "void*", "int"]),
        GetWindowTextW: user32.func("GetWindowTextW", "int", ["void*", "void*", "int"]),
        GetWindowThreadProcessId: user32.func("GetWindowThreadProcessId", "uint32", ["void*", "void*"]),
        SetWindowPos: user32.func("SetWindowPos", "bool", ["void*", "void*", "int", "int", "int", "int", "uint"]),
        DwmGetWindowAttribute: dwmapi.func("DwmGetWindowAttribute", "long", ["void*", "uint", "void*", "uint"]),
        // 桌面图标（Explorer 的 SysListView32 项）——跨进程读 LVM_GETITEMRECT
        FindWindowExW: user32.func("FindWindowExW", "void*", ["void*", "void*", "str16", "str16"]),
        SendMessageW: user32.func("SendMessageW", "intptr_t", ["void*", "uint32", "uintptr_t", "void*"]),
        // Cross-process LVM_* messages to Explorer must never block forever:
        // a hung Explorer would freeze the Electron main process (and with it
        // the pet, tray and hook server). SMTO_ABORTIFHUNG + 50ms bound.
        SendMessageTimeoutW: user32.func("SendMessageTimeoutW", "intptr_t", ["void*", "uint32", "uintptr_t", "void*", "uint32", "uint32", "void*"]),
        ClientToScreen: user32.func("ClientToScreen", "bool", ["void*", "void*"]),
        OpenProcess: kernel32.func("OpenProcess", "void*", ["uint32", "bool", "uint32"]),
        VirtualAllocEx: kernel32.func("VirtualAllocEx", "void*", ["void*", "void*", "uintptr_t", "uint32", "uint32"]),
        VirtualFreeEx: kernel32.func("VirtualFreeEx", "bool", ["void*", "void*", "uintptr_t", "uint32"]),
        ReadProcessMemory: kernel32.func("ReadProcessMemory", "bool", ["void*", "void*", "void*", "uintptr_t", "void*"]),
        CloseHandle: kernel32.func("CloseHandle", "bool", ["void*"]),
      };
      fns.hTray = fns.FindWindowW("Shell_TrayWnd", null);
    } catch (err) {
      console.warn("world-geometry: koffi init failed, static floor only:", err && err.message);
      fns = null;
    }
  }

  function scaleFactor() {
    try { return screen.getPrimaryDisplay().scaleFactor || 1; } catch (_) { return 1; }
  }

  // koffi 原始调用返回物理像素 → DIP
  function toDip(pxY) {
    return pxY / scaleFactor();
  }
  const _toDip = toDip; // test hook

  function rectToDip(rc) {
    const s = scaleFactor();
    return { left: rc.left / s, top: rc.top / s, right: rc.right / s, bottom: rc.bottom / s };
  }

  function readWindowRectDip(hwnd) {
    try {
      const rc = {};
      if (!fns.GetWindowRect(hwnd, rc)) return null;
      return rectToDip(rc);
    } catch (_) {
      return null;
    }
  }

  function hwndKey(h) {
    try {
      if (koffi && typeof koffi.address === "function") return String(koffi.address(h));
    } catch (_) {}
    return String(h);
  }

  function readWStr(fn, hwnd, maxLen) {
    try {
      const buf = Buffer.alloc(maxLen * 2);
      fn(hwnd, buf, maxLen);
      return buf.toString("utf16le").replace(/\0[\s\S]*$/, "");
    } catch (_) {
      return "";
    }
  }

  // ── 基础地板：任务栏 / 工作区（原 P0 逻辑）────────────────────────────
  function readTaskbarFloor() {
    const display = screen.getPrimaryDisplay();
    const wa = display.workArea; // {x,y,width,height}
    const workBottom = wa.y + wa.height;
    // 判定基准用屏幕底（display.bounds 不随任务栏弹出变化）
    const screenBottom = display.bounds
      ? display.bounds.y + display.bounds.height
      : workBottom;
    if (!fns || !fns.hTray) return workBottom;
    try {
      // ⚠️ 这里绝不能调 SHAppBarMessage(ABM_GETSTATE)：实测它会在任务栏
      // 弹出动画期间阻塞 shell 调用约 200ms，把整条轮询链路卡住，猫要等
      // 动画放完才被"一次性弹上去"（2026-10-18 实测复现）。
      // 纯矩形判定即可：trayTop 贴屏幕底 = 隐藏；明显高于屏幕底 = 可见。
      const rc = {};
      fns.GetWindowRect(fns.hTray, rc);
      const trayTopDip = toDip(rc.top);
      // 阈值只取露出量（4px）：任务栏一开始露头就把 floor 交给 trayTop，
      // 猫全程贴着任务栏顶上升，不再"卡一下才弹上去"。
      const popInPx = POP_IN_EARLY_PX;
      // pop 的双通道判定：矩形迟滞 + 光标离开触发带超时兜底
      const pt = {};
      let cursorOnEdge = true;
      try {
        fns.GetCursorPos(pt);
        cursorOnEdge = toDip(pt.y) >= screenBottom - CURSOR_EDGE_PX;
      } catch (_) {}
      const now = Date.now();
      if (cursorOnEdge) cursorAwaySince = null;
      else if (cursorAwaySince === null) cursorAwaySince = now;
      const cursorAwayTooLong = cursorAwaySince !== null && (now - cursorAwaySince) > POP_RELEASE_MS;

      if (taskbarPopped === null) {
        taskbarPopped = trayTopDip < screenBottom - popInPx;
      } else if (taskbarPopped) {
        if (trayTopDip > screenBottom - POP_OUT_PX || cursorAwayTooLong) taskbarPopped = false;
      } else if (trayTopDip < screenBottom - popInPx) {
        taskbarPopped = true;
      }
      return taskbarPopped ? trayTopDip : workBottom;
    } catch (_) {
      return taskbarPopped ? wa.y + wa.height : workBottom; // keep last known on error
    }
  }

  // ── 刚体：每 tick 刷新矩形；窗口销毁自动移除 ────────────────────────
  function refreshBodies() {
    for (let i = bodies.length - 1; i >= 0; i--) {
      const b = bodies[i];
      if (b.kind === "icon") continue; // 桌面图标是静态矩形，不按窗口刷新
      if (!fns || !fns.IsWindow(b.hwnd)) {
        bodies.splice(i, 1);
        continue;
      }
      const r = readWindowRectDip(b.hwnd);
      if (!r) {
        bodies.splice(i, 1);
        continue;
      }
      b.rect = r;
    }
  }

  // ── 系统面板：开始菜单（自动刚体，打开时把猫顶起来）────────────────
  // Win11 开始菜单 = class "Windows.UI.Core.CoreWindow" + title "Search"，
  // rect 约 (36,228) 784x642。注意同类还有一个全屏宿主窗口（rect=整屏），
  // 必须排除，否则会误判顶面 y=0 把猫吸到屏幕顶（2026-10-18 实测）。
  function readStartMenuRect() {
    if (!fns) return null;
    try {
      const h = fns.FindWindowW("Windows.UI.Core.CoreWindow", "Search");
      if (!h) return null;
      // 开始菜单窗口常驻（IsWindowVisible 一直是 true），关闭时由 DWM 做
      // cloak 隐藏——cloaked=2 表示关闭，0 表示打开（2026-10-18 实测）。
      try {
        const buf = Buffer.alloc(4);
        const hr = fns.DwmGetWindowAttribute(h, 14 /* DWMWA_CLOAKED */, buf, 4);
        if (hr === 0 && buf.readUInt32LE(0) !== 0) return null;
      } catch (_) {}
      const rc = readWindowRectDip(h);
      if (!rc) return null;
      const display = screen.getPrimaryDisplay();
      const w = rc.right - rc.left;
      const hgt = rc.bottom - rc.top;
      if (w >= display.bounds.width * 0.9) return null; // 全屏宿主，跳过
      if (hgt < 100 || w < 100) return null;
      return rc;
    } catch (_) {
      return null;
    }
  }

  // 单个矩形能否作为支撑面：支持两种语义——
  //   1) 平台（猫在窗口上方/顶面附近）→ 站在窗口顶面
  //   2) 容器（猫已进入窗口内部）→ 站在窗口内部底边（"站在窗口里"）
  // LAND_BELOW_PX：下落时单帧最大位移（快速下落可达 ~180px/帧）——
  // 窗口顶必须在脚下方这个范围内才能被"下落经过"捕捉，否则猫会直接穿过
  // 窗口落到地面（2026-10-18 用户实测：窗口高了就站不上，低了才行）。
  const LAND_BELOW_PX = 220;

  function pickSupport(rect, x, feetY, best, screenBottom) {
    if (!rect) return null;
    if (x < rect.left || x > rect.right) return null;
    const dy = rect.top - feetY; // >0 = 窗口顶在脚下方
    // 平台语义：窗口顶在脚上方 48px 内，或脚下方 220px 内（下落捕捉带）
    if (rect.top < best && dy >= -PUSH_TOL_PX && dy <= LAND_BELOW_PX) {
      return { value: rect.top, kind: "top" };
    }
    // 容器语义：猫在窗口内部（顶面在脚之上）→ 内部底边支撑
    //（底边必须在屏幕内，否则猫会掉到屏幕外）
    if (rect.top < feetY && rect.bottom < best && rect.bottom >= feetY - PUSH_TOL_PX
        && rect.bottom <= screenBottom + 4) {
      return { value: rect.bottom, kind: "bottom" };
    }
    return null;
  }

  // 上一帧选中的支撑体：站着时"粘住"它，窗口被拖动/升高多少都跟随，
  // 不受 48px 顶起带限制（否则拖快了就断链、猫掉下去，2026-10-18 实测）。
  // 但必须带"脚底接近"校验——否则猫被拖走/掉下后仍永久粘在旧窗口上，
  // 别的窗口全都站不上（2026-10-18 回归）。
  let lastSupport = null; // { key, kind: "top" | "bottom" }
  const STICK_TOL_PX = 260; // 覆盖拖动窗口的单帧位移；拖走猫会拉开距离而解除

  function findBody(key) {
    return bodies.find((b) => b.key === key) || null;
  }

  // ── floor 计算：任务栏 + 开始菜单 + 所有覆盖猫 x 的刚体，取最高支撑 ──
  function readFloorAt(x, feetY) {
    let best = readTaskbarFloor();
    if (x == null || feetY == null) return best;
    // 开始菜单：系统面板，允许大跨度顶起（猫直接跳到面板顶部，不被淹没）。
    const sm = readStartMenuRect();
    if (sm && x >= sm.left && x <= sm.right && sm.top < best) {
      best = sm.top;
    }
    // 1) 粘住上一帧站着的刚体：仅当脚底仍在支撑面附近才粘（拖窗口时不丢链）；
    //    拉开距离（猫被拖走/掉下）立即解除——否则会永久锁死在旧窗口上，
    //    别的窗口都站不上（2026-10-18 回归修复）。
    if (lastSupport) {
      const b = findBody(lastSupport.key);
      if (b && b.rect && x >= b.rect.left && x <= b.rect.right) {
        const sup = lastSupport.kind === "bottom" ? b.rect.bottom : b.rect.top;
        if (Math.abs(feetY - sup) <= STICK_TOL_PX) {
          if (sup < best) best = sup;
        } else {
          lastSupport = null;
        }
      } else {
        lastSupport = null;
      }
    }
    // 2) 常规候选：找（新的）支撑面
    const display = screen.getPrimaryDisplay();
    const screenBottom = display.bounds
      ? display.bounds.y + display.bounds.height
      : display.workArea.y + display.workArea.height;
    for (const b of bodies) {
      const r = pickSupport(b.rect, x, feetY, best, screenBottom);
      if (r) {
        best = r.value;
        lastSupport = { key: b.key, kind: r.kind };
      }
    }
    return best;
  }

  function readFloor() {
    const pb = getPetBounds ? getPetBounds() : null;
    const x = pb && Number.isFinite(pb.x) ? pb.x + pb.width / 2 : null;
    // feet 必须用猫的实时脚底，不能用上一帧缓存的 floorY——
    // 否则猫从窗口上方落下时，下方刚体会被 48px 顶起带全部过滤，
    // 直接穿过窗口掉到地面（2026-10-18 用户实测"添加刚体没反应"）。
    const feet = getPetFeet ? getPetFeet() : floorY;
    return readFloorAt(x, feet);
  }

  // ── 刚体拾取 / 列表 / 移除 ────────────────────────────────────────
  function publicBody(b) {
    return { id: b.id, label: b.label, rect: b.rect };
  }

  // ── 持久化：窗口按 类名+标题 重新绑定，桌面图标按索引重新读 ──────────
  function serializeBodies() {
    return bodies.map((b) =>
      b.kind === "icon"
        ? { kind: "icon", index: b.index, label: b.label }
        : { kind: "window", cls: b.cls || "", title: b.title || "", label: b.label }
    );
  }

  function notifyBodiesChanged() {
    try {
      if (typeof onBodiesChange === "function") onBodiesChange(serializeBodies());
    } catch (_) {}
  }

  function restoreBodies(list) {
    if (!Array.isArray(list) || !list.length) return { status: "ok", restored: 0 };
    let restored = 0;
    for (const it of list) {
      try {
        if (!it || typeof it !== "object") continue;
        if (it.kind === "icon") {
          const icons = readDesktopIconRects();
          const hit = icons.find((x) => x.index === it.index);
          if (!hit) continue;
          const key = "icon:" + hit.index;
          if (bodies.some((b) => b.key === key)) continue;
          bodies.push({
            id: nextBodyId++, key, rect: hit.rect,
            label: it.label || `Desktop icon #${hit.index + 1}`,
            kind: "icon", index: hit.index,
          });
          restored++;
        } else if (it.kind === "window") {
          if (!it.cls && !it.title) continue;
          const hwnd = fns.FindWindowW(it.cls || null, it.title || null);
          if (!hwnd) continue;
          const key = hwndKey(hwnd);
          if (bodies.some((b) => b.key === key)) continue;
          const rect = readWindowRectDip(hwnd);
          if (!rect) continue;
          bodies.push({
            id: nextBodyId++, key, hwnd, rect,
            cls: it.cls, title: it.title,
            label: it.label || it.title || it.cls,
            kind: "window",
          });
          restored++;
        }
      } catch (_) {}
    }
    return { status: "ok", restored };
  }

  function listBodies() {
    if (!fns) return { status: "ok", bodies: [], supported: false };
    refreshBodies();
    return { status: "ok", supported: true, bodies: bodies.map(publicBody) };
  }

  // 桌面图标：Explorer 的 SysListView32 项（不是独立窗口）。跨进程读
  // LVM_GETITEMRECT 才能拿到每个图标的屏幕矩形（2026-10-18 实测本机 94 个）。
  function findDesktopListView() {
    try {
      const progman = fns.FindWindowW("Progman", null);
      let defView = progman ? fns.FindWindowExW(progman, null, "SHELLDLL_DefView", null) : null;
      if (!defView) {
        const ww = fns.FindWindowW("WorkerW", null);
        if (ww) defView = fns.FindWindowExW(ww, null, "SHELLDLL_DefView", null);
      }
      if (!defView) return null;
      return fns.FindWindowExW(defView, null, "SysListView32", "FolderView");
    } catch (_) {
      return null;
    }
  }

  // LVM_GETITEMCOUNT / LVM_GETITEMRECT are synchronous cross-process
  // messages into Explorer. Use the timeout variant so a busy Explorer
  // cannot hang the main process; returns null on timeout.
  function sendExplorerMessage(hwnd, msg, wParam, lParam) {
    if (!fns || typeof fns.SendMessageTimeoutW !== "function") return null;
    const resultBuf = Buffer.alloc(8);
    const ok = fns.SendMessageTimeoutW(hwnd, msg, wParam, lParam, 0x0002 /* SMTO_ABORTIFHUNG */, 50, resultBuf);
    if (!ok) return null;
    return resultBuf.readUInt32LE(0);
  }

  function readDesktopIconRects() {
    const lv = findDesktopListView();
    if (!lv) return [];
    let hProc = null;
    let remote = null;
    try {
      const pidBuf = Buffer.alloc(4);
      fns.GetWindowThreadProcessId(lv, pidBuf);
      hProc = fns.OpenProcess(0x0008 | 0x0010 | 0x0020, false, pidBuf.readUInt32LE(0));
      if (!hProc) return [];
      remote = fns.VirtualAllocEx(hProc, null, 64, 0x1000, 0x04);
      if (!remote) return [];
      const countRaw = sendExplorerMessage(lv, 0x1004, 0, null); // LVM_GETITEMCOUNT
      if (countRaw == null) return [];
      const count = Math.min(Number(countRaw), 200);
      const scale = scaleFactor();
      const out = [];
      const readBuf = Buffer.alloc(8);
      for (let i = 0; i < count; i++) {
        const rectOk = sendExplorerMessage(lv, 0x100E, i, remote); // LVM_GETITEMRECT (LVIR_BOUNDS)
        if (rectOk == null || rectOk === 0) continue;
        const local = Buffer.alloc(16);
        if (!fns.ReadProcessMemory(hProc, remote, local, 16, readBuf)) continue;
        if (readBuf.readUInt32LE(0) !== 16) continue;
        const l = local.readInt32LE(0);
        const t = local.readInt32LE(4);
        const r = local.readInt32LE(8);
        const b = local.readInt32LE(12);
        if (r - l <= 0 || b - t <= 0) continue;
        const pt = Buffer.alloc(8);
        pt.writeInt32LE(l, 0);
        pt.writeInt32LE(t, 4);
        fns.ClientToScreen(lv, pt);
        const sx = pt.readInt32LE(0);
        const sy = pt.readInt32LE(4);
        out.push({
          index: i,
          rect: {
            left: sx / scale,
            top: sy / scale,
            right: sx / scale + (r - l) / scale,
            bottom: sy / scale + (b - t) / scale,
          },
        });
      }
      return out;
    } catch (_) {
      return [];
    } finally {
      if (remote && hProc) { try { fns.VirtualFreeEx(hProc, remote, 0, 0x8000); } catch (_) {} }
      if (hProc) { try { fns.CloseHandle(hProc); } catch (_) {} }
    }
  }

  function addDesktopIconAt(cx, cy) {
    const icons = readDesktopIconRects();
    if (!icons.length) return { status: "error", message: "cannot read desktop icons" };
    const hit = icons.find(
      (it) => cx >= it.rect.left && cx <= it.rect.right && cy >= it.rect.top && cy <= it.rect.bottom
    );
    if (!hit) return { status: "error", message: "no desktop icon under cursor" };
    const key = "icon:" + hit.index;
    if (bodies.some((b) => b.key === key)) return { status: "error", message: "already a rigid body" };
    const body = { id: nextBodyId++, key, rect: hit.rect, label: `Desktop icon #${hit.index + 1}`, kind: "icon", index: hit.index };
    bodies.push(body);
    notifyBodiesChanged();
    return { status: "ok", body: publicBody(body) };
  }

  // ── AI 自主收养刚体：枚举候选窗口 + 按句柄注册 ──────────────────────
  // 与 pickFromCursor 同一套校验（排除自己/太小/桌面/任务栏/全屏宿主），
  // 只是"哪个窗口"由宠物自己选（pet_world_scout 看视角 → pet_world_adopt
  // 收养），不再依赖用户的光标。
  function tryAdoptWindowBody(hwnd) {
    if (!fns || !hwnd || !fns.IsWindow(hwnd)) {
      return { status: "error", message: "window is gone" };
    }
    try {
      const pidBuf = Buffer.alloc(4);
      fns.GetWindowThreadProcessId(hwnd, pidBuf);
      if (pidBuf.readUInt32LE(0) === process.pid) {
        return { status: "error", message: "cannot use the pet's own window" };
      }
    } catch (_) {}
    const key = hwndKey(hwnd);
    if (bodies.some((b) => b.key === key)) {
      return { status: "error", message: "already a rigid body" };
    }
    const rect = readWindowRectDip(hwnd);
    if (!rect) return { status: "error", message: "cannot read window rect" };
    if (rect.right - rect.left < 8 || rect.bottom - rect.top < 8) {
      return { status: "error", message: "window is too small" };
    }
    const cls = readWStr(fns.GetClassNameW, hwnd, 256);
    const title = readWStr(fns.GetWindowTextW, hwnd, 512);
    if (cls === "Progman" || cls === "WorkerW" || cls === "Shell_TrayWnd") {
      return { status: "error", message: "desktop/taskbar windows cannot be added" };
    }
    if (cls === "Windows.UI.Core.CoreWindow") {
      const display = screen.getPrimaryDisplay();
      if ((rect.right - rect.left) >= display.bounds.width * 0.9) {
        return { status: "error", message: "cannot use the fullscreen system host window" };
      }
    }
    const body = {
      id: nextBodyId++,
      key,
      hwnd,
      rect,
      cls,
      title,
      kind: "window",
      label: title || cls || `Window ${nextBodyId - 1}`,
    };
    bodies.push(body);
    notifyBodiesChanged();
    return { status: "ok", body: publicBody(body) };
  }

  function listWindows() {
    if (!fns) return { status: "error", message: "rigid bodies are only supported on Windows", windows: [] };
    const out = [];
    const seen = new Set();
    const GW_HWNDNEXT = 2;
    let hwnd = fns.GetTopWindow(null);
    let guard = 0;
    while (hwnd && guard++ < 2000) {
      try {
        if (fns.IsWindowVisible(hwnd)) {
          const root = fns.GetAncestor(hwnd, 2 /* GA_ROOT */);
          const top = root || hwnd;
          const key = hwndKey(top);
          if (!seen.has(key)) {
            seen.add(key);
            const title = readWStr(fns.GetWindowTextW, top, 512);
            const pidBuf = Buffer.alloc(4);
            fns.GetWindowThreadProcessId(top, pidBuf);
            const own = pidBuf.readUInt32LE(0) === process.pid;
            const rect = readWindowRectDip(top);
            const cls = rect ? readWStr(fns.GetClassNameW, top, 256) : "";
            const display = screen.getPrimaryDisplay();
            const adoptable = (
              title
              && !own
              && rect
              && rect.right - rect.left >= 8
              && rect.bottom - rect.top >= 8
              && cls !== "Progman" && cls !== "WorkerW" && cls !== "Shell_TrayWnd"
              && !(cls === "Windows.UI.Core.CoreWindow"
                   && (rect.right - rect.left) >= display.bounds.width * 0.9)
            );
            if (adoptable) {
              out.push({
                hwnd: key, // hwndKey = 十进制地址串；pet_world_adopt 原样回传
                title: title.slice(0, 60),
                rect,
                already: bodies.some((b) => b.key === key),
              });
            }
          }
        }
      } catch (_) {}
      hwnd = fns.GetWindow(hwnd, GW_HWNDNEXT);
    }
    return { status: "ok", windows: out };
  }

  function addBodyFromHwnd(hwndToken) {
    if (!fns) return { status: "error", message: "rigid bodies are only supported on Windows" };
    const num = Number(String(hwndToken).trim());
    if (!Number.isFinite(num) || num <= 0) {
      return { status: "error", message: "invalid hwnd token" };
    }
    return tryAdoptWindowBody(num);
  }

  function pickFromCursor() {
    if (!fns) return { status: "error", message: "rigid bodies are only supported on Windows" };
    const pt = {};
    if (!fns.GetCursorPos(pt)) return { status: "error", message: "cannot read cursor position" };
    let hwnd = fns.WindowFromPoint(pt);
    if (!hwnd) return { status: "error", message: "no window under cursor" };
    // 桌面（Progman/WorkerW/SysListView32）→ 拾取光标下的桌面图标
    const rawCls = readWStr(fns.GetClassNameW, hwnd, 256);
    if (rawCls === "SysListView32" || rawCls === "Progman" || rawCls === "WorkerW") {
      return addDesktopIconAt(toDip(pt.x), toDip(pt.y));
    }
    const root = fns.GetAncestor(hwnd, 2 /* GA_ROOT */);
    if (root) hwnd = root;
    return tryAdoptWindowBody(hwnd);
  }

  // ── AI 操作刚体：推 / 拉 / 拿 / 踢（移动真实窗口，带缓动）─────────────
  // 只对窗口刚体生效；桌面图标没有窗口可动。位移上限 250px，始终 clamp
  // 在工作区内，避免把用户的窗口扔出屏幕。
  const bodyOps = []; // { hwnd, t0, dur, fromX, fromY, toX, toY }

  function operateBody(id, action, opts = {}) {
    if (!fns) return { status: "error", message: "Windows only" };
    const b = bodies.find((x) => x.id === id);
    if (!b) return { status: "error", message: "rigid body not found" };
    if (b.kind !== "window") return { status: "error", message: "only window bodies can be moved" };
    if (!b.hwnd || !fns.IsWindow(b.hwnd)) return { status: "error", message: "window is gone" };
    const rect = readWindowRectDip(b.hwnd);
    if (!rect) return { status: "error", message: "cannot read window rect" };
    const pb = getPetBounds ? getPetBounds() : null;
    const petCx = pb ? pb.x + pb.width / 2 : null;
    if (petCx == null) return { status: "error", message: "pet bounds unavailable" };
    const bodyCx = (rect.left + rect.right) / 2;
    const dir = bodyCx >= petCx ? 1 : -1; // 刚体在猫右边 → 推向右
    const wantDist = Number(opts.distance);
    const dist = Math.max(40, Math.min(250, Number.isFinite(wantDist) ? wantDist : 160));
    let dx = 0;
    let dy = 0;
    let dur = 600;
    switch (action) {
      case "push": dx = dir * dist; break;
      case "pull": dx = -dir * dist; break;
      case "kick": dx = dir * Math.min(300, dist * 1.7); dur = 380; break;
      case "grab":
        dx = 0;
        dy = -Math.max(80, (pb && pb.height ? pb.height : 233) * 0.9);
        dur = 500;
        break;
      default:
        return { status: "error", message: `unknown action "${action}" (push|pull|kick|grab)` };
    }
    const wa = screen.getPrimaryDisplay().workArea;
    const w = rect.right - rect.left;
    const h = rect.bottom - rect.top;
    let toX = rect.left + dx;
    let toY = rect.top + dy;
    toX = Math.max(wa.x, Math.min(wa.x + wa.width - w, toX));
    toY = Math.max(wa.y, Math.min(wa.y + wa.height - h, toY));
    if (Math.abs(toX - rect.left) < 1 && Math.abs(toY - rect.top) < 1) {
      return { status: "error", message: "cannot move further in that direction" };
    }
    bodyOps.push({ hwnd: b.hwnd, t0: Date.now(), dur, fromX: rect.left, fromY: rect.top, toX, toY });
    return { status: "ok", action, from: { x: Math.round(rect.left), y: Math.round(rect.top) }, to: { x: Math.round(toX), y: Math.round(toY) } };
  }

  function stepBodyOps() {
    if (!bodyOps.length) return;
    const now = Date.now();
    for (let i = bodyOps.length - 1; i >= 0; i--) {
      const op = bodyOps[i];
      const t = Math.min(1, (now - op.t0) / op.dur);
      const e = 1 - Math.pow(1 - t, 3); // easeOutCubic
      const x = Math.round(op.fromX + (op.toX - op.fromX) * e);
      const y = Math.round(op.fromY + (op.toY - op.fromY) * e);
      try {
        // SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE —— 只移动，不改变大小/层级
        fns.SetWindowPos(op.hwnd, null, x, y, 0, 0, 0x0001 | 0x0004 | 0x0010);
      } catch (_) {}
      if (t >= 1) bodyOps.splice(i, 1);
    }
  }

  function removeBody(id) {
    const idx = bodies.findIndex((b) => b.id === id);
    if (idx < 0) return { status: "error", message: "rigid body not found" };
    bodies.splice(idx, 1);
    notifyBodiesChanged();
    return { status: "ok" };
  }

  // ── 生命周期 ─────────────────────────────────────────────────────
  function start(onChange) {
    if (timer) return;
    timer = setInterval(() => {
      refreshBodies();
      stepBodyOps();
      const y = readFloor();
      if (floorY === null) { floorY = y; return; }
      if (y !== floorY) { floorY = y; try { onChange(y); } catch (_) {} }
    }, pollMs);
    if (timer.unref) timer.unref();
  }

  function stop() { if (timer) clearInterval(timer); timer = null; }
  function getFloorY() { return floorY === null ? readFloor() : floorY; }

  return {
    start,
    stop,
    getFloorY,
    isTaskbarPopped: () => taskbarPopped === true,
    listBodies,
    pickFromCursor,
    listWindows,
    addBodyFromHwnd,
    removeBody,
    restoreBodies,
    serializeBodies,
    operateBody,
    // test hooks
    _toDip,
    _readFloorAt: readFloorAt,
  };
}

module.exports = createWorldGeometry;
