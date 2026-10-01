/**
 * animation/ — unified animation backend layer (foresight 05, A0 file drop).
 *
 * 双平面分层（机长 2026-09-30 裁定）：
 *   - locomotion 平面：走/跑/跳等连续基础运动 → 骨架驱动（可变速、可中断，
 *     02 物理世界的位移只进这个平面）
 *   - performance 平面：复杂、离散、角色特异的一次性表演 → 视频生成 GIF /
 *     Live2D 剪辑（质量来自美术，不做程序化改造）
 *   - 公共参数：valence / arousal / gaze / squash —— 脊髓层弹簧曲线调制
 *
 * 本目录当前只落**接口与语义常量**。GifBackend 收编（现有 renderer.js 状态机
 * 搬进接口）属于 @Qoder 地盘的接线工作，等其删功能收尾后板上打招呼再动。
 *
 * 降级纪律（写进接口契约）：后端兑现不了的语义一律**静默忽略**，不报错——
 * 降级是设计行为，不是异常。
 */

/** 基础运动步态（locomotion 平面） */
const Gait = Object.freeze({
  IDLE: 'idle',
  WALK: 'walk',
  RUN: 'run',
  FAST_RUN: 'fast_run',
  JUMP: 'jump',
});

/** 公共参数（取值范围约定见字段注释） */
const Param = Object.freeze({
  VALENCE: 'valence',     // -1..1   情绪效价（04：情绪系统）
  AROUSAL: 'arousal',     // 0..3    唤醒度（0=困倦 3=兴奋）
  GAZE_X: 'gazeX',        // -1..1   注视点（相对体轴）
  GAZE_Y: 'gazeY',        // -1..1
  SQUASH: 'squash',       // 0..1    挤压量（着地/压缩）
  ENERGY: 'energy',       // 0..1    动作能量（速度/幅度总闸）
});

/**
 * 02 物理世界每帧喂给渲染层的位移姿态（先平移兑现，骨架后接管更多自由度）。
 * @typedef {{x:number,y:number,vy:number,grounded:boolean,tilt:number}} PhysicsPose
 */

/**
 * AnimationBackend 契约（每个渲染技术一个实现）：
 *
 *   async load(themeDir, themeJson, host)
 *     host = { container: HTMLElement, width, height, pixelRatio }
 *     themeJson.gaitRig 缺失时：locomotion 平面退化为「精灵整体平移」（现状）。
 *
 *   setGait(gait: Gait, opts?: { speed?: 0..1, direction?: -1|1 })
 *     连续基础运动。可任意时刻重入（变速/换向），语义上是「当前常态」。
 *
 *   playPerformance(name: string, opts?: object) => Promise<void>
 *     一次性表现动作；播完自动回到当前 gait。name 非法 → 静默回退，不抛错。
 *
 *   setParam(name: Param, value: number)   —— 见 Param 注释的范围约定。
 *   setPhysics(pose: PhysicsPose)          —— 物理层注入（A4 汇合 02）。
 *   tick(dtMs: number)                     —— rAF 驱动。
 *   dispose()
 */
const ANIMATION_SEMANTICS_VERSION = 1;

module.exports = { Gait, Param, ANIMATION_SEMANTICS_VERSION };
