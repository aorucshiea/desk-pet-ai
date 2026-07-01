# Special / Long-Idle State Specs

### 404 error final spec ⭐ 锁定 2026-04-26 (激进彩蛋槽 1 / error 状态)

**当前锁定文件**: `experiments/codex-pet/confirmed/states/error-thundercloud-loop-v9-tuned.svg.html` (来源 WIP: `experiments/codex-pet/wip/error-thundercloud-loop-v9-tuned.svg.html`). 关键对照文件: `error-thundercloud-no-bolt-v5-rain.svg` (静态方向), `error-thundercloud-loop-v6-rain-smooth.svg.html` (雨线平滑), `error-thundercloud-loop-v8-stable-color.svg.html` (冻结材质排查闪频).

**核心语义**: 小云崽不是被闪电击中, 而是"崩溃到下雨". 循环节奏是: 正常小雨 → 像拧毛巾一样压扁 → 雨水被挤出来哗哗变大 → 云朵缓慢回弹 → 雨势减弱 → 下一轮. 这是 error / heavy-error / 404 的状态循环, 不是一次性的电闪镜头.

**视觉锁定**:
- 乌云是**稳定漫反射材质**, 不是高光光面球. 内部可以有轻微渐变流动, 但只做位置位移, 不做颜色/透明度跳变.
- 最终去掉闪电. 早期 v2/v4 的闪电会抢主体, 跟精致风 Cloudling 不一致.
- 眼睛用 C 版软 X / 崩溃眼, 保留紫蓝渐变和 glow, 不要画成空洞无眼.
- 雨线走 v6 双 copy 平滑方案, 不是 v7 雨幕方案. 当前 `RAIN_COPIES = 2`, 8 条雨线生成 16 个 path, 用错相位和宽 fade 减少闪频.

**默认锁定参数** (打开 `error-thundercloud-loop-v9-tuned.svg.html` 默认值):

| 参数 | 值 |
|------|-----|
| 总周期 | 4.0 s |
| 开始拧的位置 | 0.53 |
| 拧+回弹时长 | 0.66 |
| 压扁用时占比 | 0.60 |
| 压扁曲线硬度 | 0.95 |
| 呼吸大小 / 速度 | 0.070 / 1.00 |
| 拧毛巾压扁幅度 | 0.088 |
| 压扁横向膨胀 | 0.058 |
| 压扁下沉 | 0.30 |
| 回弹过冲 / 衰减 | 0.018 / 0.78 |
| 雨跟拧的错位 | 0.05 |
| 雨随拧持续 | 0.66 |
| 雨峰曲线硬度 | 1.00 |
| 浅色 / 暗部漫反射流动 | 0.56 / 0.48 |
| 漫反射流带强度 | 0.28 |
| 流动位移 / 速度 / 柔化 | 4.1 / 1.25 / 0.50 |
| 拧时流动加速感 | 0.55 (只影响位置速度, 不影响颜色/透明度) |
| 雨线数量 | 8 |
| 常态雨透明度 / 速度 / 长度 | 0.58 / 0.62 / 1.30 |
| 挤出时增亮 / 加速 / 变长 | 0.34 / 0.82 / 1.60 |
| 雨线斜移 | 0.40 |
| 拧时眼睛变暗 | 0.08 |

**实现要点**:
- `squeezeAt(phase)` 是主驱动, `rainBurstAt(phase)` 跟随同一段时间线, 只用 `rainDelay` 做轻微错位. 不要让大雨和压扁脱节.
- `flow = t / period * flowSpeed * (1 + squeeze * twistFlowBoost)`, 但 flow 只驱动 `cx/cy/r/transform` 小幅变化. `core-stop-a/b`, `hard-shadow`, `blue-glow`, `flowLight/flowDark/flowBand opacity` 维持固定, 避免大雨段闪屏.
- 云体呼吸保留, 但拧毛巾阶段上下距离变窄是主视觉. 回弹必须慢于压扁, 雨势随回弹缓慢收掉.
- `statusbar` + sliders 保留给继续微调; 如果鹿鹿再给一组截图数值, 直接改 `DEFAULTS`.

**这次沉淀的流程教训**:
1. 404 的"激进"不一定要靠闪电. 对 Cloudling 更成立的是角色自己崩溃, 不是外部效果抢戏.
2. 乌云不能做光面高光, 会显成球. 用漫反射稳定材质, 靠形变和雨势表达情绪.
3. 大雨闪频通常不是雨线一个问题, 也可能是云体颜色 / opacity / filter 同步变化造成. 先做冻结材质对照版再继续调参.
4. 循环动画要用故事节奏理解: 正常雨 → 拧压 → 大雨 → 慢回弹, 不能把"下小雨"段拖太长.
5. 调参页必须能输出默认值. 鹿鹿截图确认后, 直接固化 `DEFAULTS`, 不要口头记参数.

### cloud-plane orbit final spec ⭐ 锁定 2026-04-26 (激进彩蛋槽 2 + juggling 主状态复用 2026-04-29)

**当前锁定文件**: `experiments/codex-pet/confirmed/states/cloud-plane-orbit-explore.svg.html` (commit `d0c4ff3` 时位于 wip/, 已搬入 confirmed/states/). 支撑文件: `experiments/codex-pet/confirmed/library/paper-plane-v9-cloudling.svg.html` + `experiments/codex-pet/wip/paper-plane-face-debug.svg.html`.

**双重映射** (2026-04-29 鹿鹿拍板):
- **激进彩蛋槽 2** — 长 idle / 罕见触发的视觉彩蛋 (原始定位).
- **juggling 主状态** — 对应 clawd 的 SubagentStart(1) 事件: 1 个 subagent 在工作. 纸飞机绕飞 = 1 个独立物件围着 Cloudling 转, 语义直接对应"杂耍 1 颗球". 跟 conducting (V5, 4 颗 task sparks) 形成 **1 vs 4+ 的事件量级对照**: 1 subagent → 1 颗物件 (纸飞机) orbit, 2+ subagents → 多颗 task sparks 被点名归位. 同一 SVG 文件同时承担两个映射, 不另起炉灶 (跟 happy burst V6 同时作为 attention 的复用模式一致).

**核心语义**: 纸飞机像小行星绕 Cloudling 运行. 这不是"飞机从右下飞入 → 绕一圈 → 右上飞出"的镜头, 而是一个可以无缝 loop 的绕飞彩蛋. 飞机 pivot 永远贴着倾斜椭圆轨道跑, Cloudling 根据飞机轨道相位好奇收缩/放松: **左右远端更像完整云朵, 前后贴身经过时更像球**.

**默认锁定参数** (打开 `cloud-plane-orbit-explore.svg.html` 默认值):

| 参数 | 值 |
|------|-----|
| 总周期 | 5.2 s |
| 轨道 rx / ry | 17.8 / 4.9 |
| 轨道倾斜角 | -13deg |
| 起始角 | 0deg |
| 纸飞机配色 | `bluePractical` (UI 显示 Blue) |
| 纸飞机尺寸 | 0.20 |
| 纸飞机全局 X / Y | 0.0 / 0.0 |
| 纸飞机方向修正 | 123deg |
| 纸飞机 pivot X / Y | 19.0 / 19.0 |
| 背面缩放 / 透明度 | 0.71 / 0.65 |
| 翻面压缩 | 0.85 |
| 远端球化 / 近端球化 | 8% / 60% |
| 目标 R | 7.0 |
| 眼睛追随 | 1.20 |

**纸飞机几何 source of truth**:
- 参考文件: `archive/stlabs-2026-04/deliverables/ticket-state-created.canvas.html`, 不是 `airplane-cheat` 旧视觉稿的颜色和缺面版本.
- 顶点命名: `N(3,6)`, `TR(34,15.7)`, `MR(24,23)`, `C(20.6,29.5)`, `ML(20.1,25.6)`, `TL(6.7,32.2)`.
- 只保留 4 个面: `bodyRight N-MR-C`, `bodyLeft N-ML-C`, `leftWing N-ML-TL`, `rightWing N-TR-MR`.
- **P3 / backFace `ML-C-TL` 已被鹿鹿判定多余, 不要再加回来.** 如果飞机看着又"AI 味"或面数奇怪, 先开 `paper-plane-face-debug.svg.html` 标编号对齐, 不要直接在动画里猜.

**轨道 / 形变驱动**:
- `orbitDepth = sin(theta)` 表示前后深度, `orbitProximity = smoothstep(abs(orbitDepth))` 表示飞机是否贴身经过.
- `morph = lerp(morphMin, morphMax, orbitProximity)`, 当前 `morphMin=8%`, `morphMax=60%`.
- 左右远端 (`abs(sin(theta)) ≈ 0`) 云朵更松、更像云; 前后贴身经过 (`abs(sin(theta)) ≈ 1`) 云朵收缩、更像球.
- 不要用 `frontness` 驱动球化; `frontness` 只负责前后层级、透明度、尾迹显示. 这次跑偏点是把"远近"理解成"前后", 实际鹿鹿要的是"左右远端 vs 云朵身侧".

**纸飞机朝向 / 翻面**:
- 飞机的**中心 pivot** 沿轨道跑, 机头方向沿轨道切线, 再加 `direction correction = 123deg`. 不要只调飞机外框位置, 否则机身中心点会偏离轨道.
- 需要伪 3D 翻面, 不能只 rotate 一个 SVG. 当前用 top / under 两套 symbol 按 `orbitDepth` 交叉淡入淡出, 并在翻面边界用 `rollMinScale=0.85` 压缩局部 scaleY.
- 配色默认 `bluePractical`: 蓝紫主体 + 较深折面, 比撞色 / Happy 配色更克制、跟 Cloudling 主视觉更统一.

**轨道线处理**:
- 全程显示完整轨道线不好看; 完全不显示又看不懂绕飞.
- 最终方案用**短尾迹**代替完整轨道: 尾迹贴在飞机后方, 前后层级随 `frontness` 切换, 让观众读出轨道但不把画面画脏.

**眼睛要求**:
- 眼睛必须严格盯纸飞机: tracking 直接用 Cloudling 中心指向飞机 pivot 的向量, 不要被 `sidePass` 之类的量削弱.
- 保留 idle 里的眼睛生命感: capsule 眨眼用 height / y / rx / ry 压缩, 不是简单 opacity; `eye-grad` 继续做 y1/y2 流动, 周期 5.5s, 幅度 4.
- 眼睛追随、眨眼、渐变流动缺任何一个, 画面都会变"死", 鹿鹿已在定稿前明确指出过.

**这次沉淀的流程教训**:
1. 源几何必须先找对. 纸飞机最初照 `airplane-cheat` 画, 少面 / 多面都跑偏; 正解来自 `ticket-state-created.canvas.html`.
2. 复杂小物件先单独开 debug 文件标面编号. 鹿鹿可以直接说"P3 多余", 比在完整动画里猜快很多.
3. 视觉语义优先于朴素数学. 球化不是"越靠前越球", 而是"飞机在云朵身侧贴近时球".
4. 小行星绕飞需要 pivot、切线方向、前后层级、翻面一起成立; 只调 rotate 角度不够.
5. Cloudling 的眼睛是角色生命线. 追踪、眨眼、渐变流动是基础设施, 不是可选 polish.

### long-idle cloud bush peek final spec ⭐ 锁定 2026-04-28 (long-idle 彩蛋 / idle-reading 候选)

**当前锁定文件**: `experiments/codex-pet/confirmed/states/long-idle-cloud-bush-peek-v1.svg.html`. 历史探索: `wip/long-idle-cloud-bush-peek-v1.backup-before-v2.svg.html` (script 重写前) + `wip/long-idle-cloud-bush-peek-v2-before-script-rewrite.svg.html` (重写前最后版本).

**触发场景**: 长时间无活动的彩蛋. 对应 clawd 主映射的 idle-reading 槽位 ("Idle (random) → Reading / patrol"), 但 Cloudling 把它重新解读成"被云挡住偷看"的桌宠喜剧, 不走读书 / 巡逻路线. 循环动画, 单周期 6.2s 叙事 + 1.0s gap = 7.2s 一轮, 可无缝 loop.

**核心语义**: 双层暖色云从左飘入挡住云宝 → 云宝藏到云后面 → "哧溜"从左上探出半脸 + 头顶空白处缓缓冒问号 → 缩回 → "哧溜"从右上再探一次, 左右上下扫看一圈并眨眼两下 → 缩回 → 暖云左右散开露出完整云宝 → 回到 idle. 笑点完全靠 cartoon 式两边轮流探头 + 头顶问号, **不靠球化 / 弹跳 / 粒子** (跟其他状态的语法刻意区分).

**新增视觉资产 - 双层暖云屏障**:
- 后层暖云: linear gradient `#FFE3CE → #FFD3B3 → #FFB89A` (暖橘), stroke `#E69E78` 0.55w, soft glow + warm-shadow
- 前层暖云: linear gradient `#FFF1CE → #FCE3B0 → #F5C374` (暖金, 比后层更亮更黄), stroke `#D9A04F` 0.55w
- 各带高光椭圆 + warm-hi radial gradient, 暖云路径是独立 `warm-cloud-path` (跟 cloudling 主体 cloud-shape 完全不同)
- 屏障静止位置: 后层 `(12 - barrierGap, barrierY)`, 前层 `(12 + barrierGap, barrierY)`, 默认 barrierGap=3.0 / barrierY=16.5 (在云宝下方)
- 入场起点: 后层 `(3, barrierY)`, 前层 `(4, barrierY)`, 起点留在画布内避免桌宠窗口切出硬边; 后层先动 0.18s 前层再跟

**9 段叙事时间线** (6.2s, 默认值):

| # | 段名 | 起止 | 时长 | 主要动作 |
|---|------|------|------|---------|
| 1 | warm-enter | 0.00 - 1.10s | 1.10s | 暖云双层从左飘入合拢, cloudling 眼睛追云从左到中, 末端轻沉 hideTy |
| 2 | crouch-left | 1.10 - 1.45s | 0.35s | 左探前压扁蓄力 (sx 1.085 / sy 0.885 / rot -1.5°) |
| 3 | peek-left | 1.45 - 2.35s | 0.90s | 哧溜左上探头 (tx -8.5 / ty -4.5 / rot -12.5° + 抖) + 好奇瞪眼 + 左右扫 + 头顶冒问号 |
| 4 | retract-1 | 2.35 - 2.75s | 0.40s | 缩回, easeIn 后段加速 (0-0.65 快缩, 0.65-1 完全藏好) |
| 5 | crouch-right | 2.75 - 3.10s | 0.35s | 右探前蓄力, 比左探略柔 |
| 6 | peek-right | 3.10 - 4.65s | 1.55s | 右上探头 + 多停一拍, 左→上→右→眨 2 下 + 身体 bodyCheck 微晃 |
| 7 | retract-2 | 4.65 - 5.05s | 0.40s | 缩回, 同 retract-1 节奏 |
| 8 | cloud-fade | 5.05 - 5.85s | 0.80s | 暖云先淡出再后层向左 / 前层向右散开 (driftDistance 42, drift 从 q=0.18 起, fade q=0.08-0.48), cloudling 露出 |
| 9 | recover | 5.85 - 6.20s | 0.35s | 眼睛轻扫 (左→右→中) 回 idle, 接呼吸 |

**默认锁定参数 DEFAULTS** (打开默认即是):

| 参数 | 值 | 备注 |
|------|----|----|
| 总时长 duration | 6.2 s | 9 段叙事不含 gap |
| 循环间隔 gap | 1.0 s | gap 内回 idle 状态 (静态 + 微呼吸) |
| 本体呼吸幅度 bodyBreath | 0.030 | 比 idle 略大, 长 idle 段更明显 |
| 外轮廓轻旋转 rimRotate | 1.5° | 整体 cloudShape 极轻摇 |
| 暖云入场速度 warmSpeed | 1.00 | warm-enter 段时间缩放系数 |
| 暖云不透明度峰值 warmOp | 0.95 | 不到 1.0 留一点透明感 |
| 云屏障横向错位 barrierGap | 3.0 | 前后层左右错开距离 |
| 云屏障纵向位置 barrierY | 16.5 | viewBox 中心 12, 16.5 = 在云宝下方 |
| 回落中心偏移 hideTy | 1.0 | 藏起来时 cloudling 下沉量 |
| 探头露出 peekTy | -4.5 | 探头时上移量 |
| 探头横偏 peekTx | 8.5 | 左探 -8.5 / 右探 +8.5 |
| 探头外轮廓倾斜 peekLean | 12.5° | 探头时身体倾斜方向 |
| 探头外轮廓收缩 peekScaleDip | 0.075 | 探头时整体微缩 |
| 好奇瞪大幅度 eyeWiden | 0.19 | 探头露出后眼睛宽度乘数 |
| 右探眼睛右移 rightEyeOffsetX | 1.40 | 右探时眼睛额外右偏 (强化好奇张望) |
| 右探眼睛上移 rightEyeLift | 0.30 | 同上, 略上移 |
| 问号大小 qSize | 1.00 | 头顶问号缩放 |
| 问号横向偏移 qOffsetX | -6.0 | 左探时问号在左上 (cloudling 中心 + offset) |
| 问号头顶距离 qOffsetY | -10.0 | 比头顶更远, 留呼吸感 |

**问号实现要点**:
- 路径 `M -1.4 -1.6 Q -1.4 -3.0 0 -3.0 Q 1.4 -3.0 1.4 -1.6 Q 1.4 -0.6 0.4 0.0 Q -0.1 0.35 -0.1 1.3` + 底部圆点 `circle r=0.44`
- stroke 用 `eye-grad` 紫蓝渐变 (跟眼睛同色), q-glow filter 加柔光
- **只在 peek-left 段显示** — 右探不再冒问号 (好奇感已经够了, 重复会失梗)
- 不弹跳, smoother 缓入缓出 (qOp 从 0.22 缓入到 0.68, 0.78-0.98 淡出), 飘移 1.05 → -0.55 (从下往上轻飘)

**关键决断历程教训** (写进来防止下次类似状态磨制时再犯):

1. **场景元素首次引入** — 暖云不是粒子 / 装扮 / 道具, 是"独立场景元素". CLAUDE.md §2 变化语法表里原本没有这一类, 这次是首次启用. 后续如果有"被某场景包围 / 钻洞 / 翻栅栏"类型的状态可以参考这个模式: 单独 symbol + 独立 transform + 跟主体的 z 关系
2. **双层错位才有"丛林"感** — 单层暖云做不出"被遮住"的层次感. 双层用前/后 z-index 把 cloudling 夹在中间, 探头时观众能读出"它从云后面冒出来"而不是"它穿过了云"
3. **左右探头节奏不能对称** — peek-left 0.90s vs peek-right 1.55s, 第二次必须比第一次长. 对称会失梗 (像机械重复), 不对称才像"先试探一次, 第二次确认状况". 鹿鹿在调参时确认了这个节奏
4. **问号只能出现一次** — 第一次冒问号是"咦?", 第二次再冒就成"???"了, 会从可爱变成困惑过头. 限定 peek-left 段独显, 右探只用扫视眨眼表达好奇
5. **gap 必须有** — 7.2s 一轮是个长循环, 中间 1.0s gap 让画面回到纯 idle 状态, 不会让观众觉得"这只云崽一直不停在演". gap 是 long-idle 类彩蛋的关键, 区别于 idle / typing 这种持续运行的循环
6. **bodyCheck 是 peek-right 的核心微动** — 右探多停的 1.55s 不能纯静态, `bodyCheck = sin × wave × 0.16` 给身体加非常细微的左右晃 (像桌宠真在张望时身体的伴随动作), 没这个就会显呆

**进 clawd 方式**: SVG + JS 驱动 (Cloudling 全 SVG 路线, 2026-04-26 拍板). long-idle 是循环动画, 6.2s + 1.0s gap = 7.2s 一轮无缝 loop. 接入 clawd 时按 long-idle 触发条件包装 (鹿鹿后续定: 是 60s 后随机触发还是 30min 后才触发).
