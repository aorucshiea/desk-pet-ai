/**
 * world-physics.js — 桌面刚体世界的运动学核（02 前瞻 P0）。
 *
 * 卡通物理（见前瞻 05 术语钉死）：这不是物理仿真，是平台游戏规则——
 * 重力、贴地（grounded）、被顶起、掉落。P0 只管 y 轴贴地与掉落；
 * 水平行走留到 P1 与动画系统 locomotion 汇合。
 *
 * 设计约束（全是踩过的坑换来的）：
 *   - 主进程跑（桌宠位移 = BrowserWindow.setBounds，每帧 IPC 不可接受）
 *   - 外部写入（用户拖拽/设置预览）优先：检测到非本模块造成的位置变化 → 静默
 *     500ms 后从新位置继续
 *   - 守卫挂起：拖拽锁定 / 宠物隐藏 / mini 模式 / mini 过场中 → 完全不碰窗口
 *   - 一切参数 px、秒、屏幕坐标系（y 向下增大）
 */

const DEFAULTS = {
  gravity: 3200,          // px/s^2 — 500px 掉落 ≈ 0.56s（真机钉过手感区间）
  tickMs: 16,             // ~60Hz
  settleEpsPx: 0.6,       // 贴地判定容差
  externalHoldMs: 500,    // 外部位置变化后的让位时间
  maxDtMs: 50,            // 主进程卡顿时的积分上限（防穿地）
};

// TEMP-PROBE (remove after P0 verification)
const probe = {
  tickN: 0,
  log(msg) {
    try {
      require("fs").appendFileSync(
        require("path").join(require("electron").app.getPath("userData"), "world-probe.log"),
        msg + "\n");
    } catch (_) {}
  },
  transitions: [],   // grounded/頂起/掉落 transitions only — quiet by design
};

function createWorldPhysics(options) {
  const getBounds = options.getBounds;               // () => {x,y,width,height}|null
  const setPosition = options.setPosition;           // (x, y) => void
  const getFloorY = options.getFloorY;               // () => number（屏幕坐标）
  const guards = options.guards || [];               // Array<() => boolean> true=suspend
  const cfg = Object.assign({}, DEFAULTS, options.config);
  const onLanded = options.onLanded || null;         // (vy) => void — A4 供 somatic 上报

  let timer = null;
  let lastT = 0;
  let vy = 0;
  let grounded = false;
  let holdUntil = 0;
  let lastApplied = null; // 本模块上次写入的位置
  let lastSeenHeight = null; // 尺寸稳定检测：clawd 主题按 GIF 逐帧调高时物理必须让位

  function suspended() {
    for (const g of guards) { try { if (g()) return true; } catch (_) {} }
    return Date.now() < holdUntil;
  }

  /** 一次积分步（抽出来便于单测）。state 见文件尾测试。 */
  function stepState(state, bounds, floor, dt) {
    const petBottom = bounds.y + bounds.height;
    if (state.grounded) {
      if (floor - petBottom > cfg.settleEpsPx) {
        // 支撑面下降/消失（任务栏收回等）→ 进入掉落（有加速过程，不是瞬移）
        return { y: bounds.y, vy: 0, grounded: false, landedVy: 0 };
      }
      // 贴住地板：刚性 reconcile —— 地板抬升（被顶起）或窗口高度被外部改变
      // （主题 GIF 逐帧调尺寸），都每帧重解算 y，防止漂移。
      const targetY = floor - bounds.height;
      if (Math.abs(bounds.y - targetY) > cfg.settleEpsPx) {
        return { y: targetY, vy: 0, grounded: true, landedVy: 0 };
      }
      return { y: bounds.y, vy: 0, grounded: true, landedVy: 0 };
    }
    const nvy = state.vy + cfg.gravity * dt;
    let ny = bounds.y + nvy * dt;
    if (ny + bounds.height >= floor) {
      ny = floor - bounds.height;
      return { y: ny, vy: 0, grounded: true, landedVy: nvy };
    }
    return { y: ny, vy: nvy, grounded: false, landedVy: 0 };
  }

  function tick() {
    probe.tickN = (probe.tickN || 0) + 1;
    const sus = suspended();
    if (sus) { lastApplied = null; return; }
    const b = getBounds();
    if (!b) return;
    // 外部写入检测：bounds 偏离本模块的上次写入 → 别人在动（拖拽等），让位
    if (lastApplied && (Math.abs(b.x - lastApplied.x) > 1 || Math.abs(b.y - lastApplied.y) > 1)) {
      holdUntil = Date.now() + cfg.externalHoldMs;
      lastApplied = null; vy = 0; grounded = false;
      return;
    }
    // 尺寸变化期间让位：高度在变说明 clawd 主题正在调动画尺寸，
    // 此时双方同写 y 会正向反馈（渲染窗被一路推飞）——物理必须闭眼等待。
    if (lastSeenHeight !== null && b.height !== lastSeenHeight) {
      lastSeenHeight = b.height;
      holdUntil = Date.now() + 150;
      lastApplied = null;
      vy = 0; grounded = false;
      return;
    }
    lastSeenHeight = b.height;
    const now = Date.now();
    const dt = Math.min(now - lastT, cfg.maxDtMs) / 1000;
    lastT = now;
    const next = stepState({ vy, grounded }, b, getFloorY(), dt);
    const changed = next.y !== b.y;
    // TEMP-PROBE: only log state transitions (grounded flips + floor moves), never steady state
    if (next.grounded !== grounded || (changed && next.grounded)) {
      probe.log(`t=${now % 100000} grounded=${grounded}->${next.grounded} y=${b.y}->${next.y} floor=${getFloorY()} vy=${Math.round(next.landedVy || vy)}`);
    }
    vy = next.vy;
    if (next.landedVy > 0 && !grounded && next.grounded && onLanded) {
      try { onLanded(next.landedVy); } catch (_) {}
    }
    grounded = next.grounded;
    if (changed) {
      setPosition(b.x, Math.round(next.y));
      lastApplied = { x: b.x, y: Math.round(next.y) };
    } else {
      lastApplied = { x: b.x, y: b.y };
    }
  }

  return {
    start() {
      if (timer) return;
      lastT = Date.now();
      timer = setInterval(tick, cfg.tickMs);
      if (timer.unref) timer.unref();
    },
    stop() { if (timer) clearInterval(timer); timer = null; lastApplied = null; },
    isRunning: () => !!timer,
    _stepState: stepState,          // 测试钩子
    _defaults: DEFAULTS,
  };
}

module.exports = createWorldPhysics;
