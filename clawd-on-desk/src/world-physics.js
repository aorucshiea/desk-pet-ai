/**
 * world-physics.js — 桌面刚体世界的运动学核（02 前瞻 P0，v2）。
 *
 * 卡通物理（见前瞻 05 术语钉死）：平台游戏规则——重力、贴地（grounded）、
 * 被顶起、掉落。只管 y 轴；水平行走留到 P1 与动画系统 locomotion 汇合。
 *
 * v2 修订（机长实测反馈 2026-10-01）：
 *   1. **参数方程掉落**：Electron 主进程 setInterval 被节流到 ~50ms（实测 vy
 *      恒撞 dt clamp 上限）——逐帧欧拉积分一卡一卡且慢放。改为掉落开始记
 *      (t0, y0)，每 tick 代入 y(t) = y0 + ½g(t−t0)² 解析取位：物理时间永远
 *      与真实时间一致，tick 频率只影响平滑度不影响速度/距离。
 *   2. **贴地锚点 = 角色内容矩形底**（getObjRect 一族），不是窗口 rect 底——
 *      窗口四周有透明边距，视觉贴地必须贴内容。
 *   3. 尺寸变化期间让位（主题 GIF 切状态重调窗高时不同写 y，防正反馈漂移）。
 *
 * 外部写入（用户拖拽/设置预览）优先：检测到非本模块造成的位置变化 → 静默
 * 500ms 后从新位置继续。守卫：拖拽锁定 / 宠物隐藏 / mini 模式 / mini 过场。
 * 一切单位：DIP（geometry 层已归一）、秒、屏幕坐标系（y 向下增大）。
 */

const DEFAULTS = {
  gravity: 6400,          // px/s^2 — 真机手感钉：~0.29s 掉半屏，有地心引力的"砸下去"感
  tickMs: 16,             // 请求 16ms；主进程实际 ~50ms——解析式不依赖它
  settleEpsPx: 0.6,       // 贴地判定容差
  externalHoldMs: 500,    // 外部位置变化后的让位时间
  maxDtMs: 50,            // 保留常量供外部读取；解析路径不再用它截断
};

function createWorldPhysics(options) {
  const getBounds = options.getBounds;               // () => {x,y,width,height}|null
  const setPosition = options.setPosition;           // (x, y) => void（窗口左上）
  const getFloorY = options.getFloorY;               // () => number DIP（屏幕坐标）
  const guards = options.guards || [];               // Array<() => boolean> true=suspend
  const cfg = Object.assign({}, DEFAULTS, options.config);
  const onLanded = options.onLanded || null;         // (vy) => void — A4 供 somatic 上报
  // 重力 live-read：设置页改完下一 tick 生效，无需重启
  const getGravity = options.getGravity || (() => cfg.gravity);
  // 内容矩形底边（视觉贴地锚点）。缺实现时退化为窗口底（行为=旧版）。
  const getContentBottom = options.getContentBottom
    || ((b) => (b ? b.y + b.height : null));

  let timer = null;
  let lastT = 0;
  let grounded = false;
  let holdUntil = 0;
  let lastApplied = null;   // 本模块上次写入的窗口位置
  let lastSeenHeight = null;
  // falling: { t0, winY0, vy0 } — 解析掉落：winY(t) = winY0 + vy0·t + ½·g·t²
  let fall = null;

  function suspended() {
    for (const g of guards) { try { if (g()) return true; } catch (_) {} }
    return Date.now() < holdUntil;
  }

  // 窗口顶 y → 内容底（DIP 屏坐标）
  function contentBottom(b) {
    const cb = b ? getContentBottom(b) : null;
    return (typeof cb === "number" && isFinite(cb)) ? cb : (b ? b.y + b.height : null);
  }
  // 目标窗口 y：使内容底 = floor
  function targetWinY(b, floor) {
    const cb = contentBottom(b);
    if (cb === null) return null;
    return b.y + (floor - cb);
  }

  /** 解析步进：输入当前时刻，直接得到目标窗口 y 与新状态。可测。 */
  function stepPosition(state, b, floor, nowT, gravityOverride) {
    const g = (typeof gravityOverride === "number" && gravityOverride > 0) ? gravityOverride : cfg.gravity;
    const target = targetWinY(b, floor);
    if (target === null) return { holding: true, winY: b.y, grounded: state.grounded, landingVy: 0 };
    if (state.grounded) {
      if (floor - (contentBottom(b)) > cfg.settleEpsPx) {
        // 支撑面下降 → 进入解析掉落
        return { holding: false, winY: b.y, grounded: false,
                 fall: { t0: nowT, winY0: b.y, vy0: 0 }, landingVy: 0 };
      }
      // 贴住（地板抬升顶起 / 内容几何变化 reconcile）
      if (Math.abs(b.y - target) > cfg.settleEpsPx) {
        return { holding: false, winY: target, grounded: true, landingVy: 0 };
      }
      return { holding: true, winY: b.y, grounded: true, landingVy: 0 };
    }
    // falling：参数方程
    const t = (nowT - state.fall.t0) / 1000;
    const winY = state.fall.winY0 + state.fall.vy0 * t + 0.5 * g * t * t;
    if (winY >= target) {
      return { holding: false, winY: target, grounded: true,
               landingVy: state.fall.vy0 + g * t };
    }
    return { holding: false, winY, grounded: false, fall: state.fall, landingVy: 0 };
  }

  function tick() {
    if (suspended()) { lastApplied = null; return; }
    const b = getBounds();
    if (!b) return;
    if (lastSeenHeight !== null && b.height !== lastSeenHeight) {
      lastSeenHeight = b.height;
      holdUntil = Date.now() + 150;
      lastApplied = null; grounded = false; fall = null;
      return;
    }
    lastSeenHeight = b.height;
    if (lastApplied && (Math.abs(b.x - lastApplied.x) > 1 || Math.abs(b.y - lastApplied.y) > 1)) {
      holdUntil = Date.now() + cfg.externalHoldMs;
      lastApplied = null; grounded = false; fall = null;
      return;
    }
    const now = Date.now();
    lastT = now;
    const next = stepPosition(
      { grounded, fall: fall || { t0: now, winY0: b.y, vy0: 0 } },
      b, getFloorY(), now, getGravity());
    if (!next.grounded && next.fall) fall = next.fall;
    if (next.grounded) fall = null;
    if (next.landingVy > 0 && !grounded && onLanded) {
      try { onLanded(next.landingVy); } catch (_) {}
    }
    grounded = next.grounded;
    if (!next.holding) {
      const wy = Math.round(next.winY);
      if (Math.abs(b.y - wy) > cfg.settleEpsPx) {
        setPosition(b.x, wy);
        lastApplied = { x: b.x, y: wy };
      } else {
        lastApplied = { x: b.x, y: b.y };
      }
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
    stop() { if (timer) clearInterval(timer); timer = null; lastApplied = null; fall = null; },
    isRunning: () => !!timer,
    _stepPosition: stepPosition,   // 测试钩子
    _defaults: DEFAULTS,
  };
}

module.exports = createWorldPhysics;
