# 待处理 Bug（Backlog）

本文件记录已诊断、但暂未修复的已知瑕疵与新功能缺口。每条注明根因、影响、建议方案，便于后续排期。

---

## ✅ 本轮已修复（2026-08-30，B-1/B-5/B-8/B-9/B-15 + Holo 接入）

| 号 | 结论 |
|---|---|
| B-1 | 流式 `[EMOTION:]`/`[NEXT_CHAT:]` 标签闪烁 → 网关侧 `ControlTagFilter` 有状态剥离 |
| B-5 | 网关零鉴权 → `~/.minicpm/gateway-token` token 中间件 + CORS 收敛；Electron/Web-panel 全部带头 |
| B-8 | 模糊目录无界 → 标题 20 / 幽灵 5 双上限，深睡层可达不可见 |
| B-9 | 情绪指数长期冻结 → 时间均值回归（每小时 10% 向 0 收敛） |
| B-15 | 缺主动稳态 → lifespan 心跳自检 + 生病/病好事件记忆 |
| ✋ Holo 接入 | 新增 `gateway/holo_agent.py`（Holo 视觉 GUI agent）；模型通过 MCP 工具 `desktop_task`/`holo_status` 调起；受屏幕 consent 门控；HTTP 端点 `/api/holo/run|status|cancel`。靠「意识层决策 WHAT，潜意识层拆解 HOW」分工。 |

---

## 历史条目

## B-1 · 流式过程中 `[EMOTION:]` / `[NEXT_CHAT:]` 标签会一闪而过

- **状态**：✅ **已修复（2026-08-30）**——网关侧有状态过滤（方案 A）。`gateway/think_filter.py` 新增 `ControlTagFilter`：SSE 流中剥离 `[EMOTION:…]`/`[NEXT_CHAT:…]`，支持跨 chunk 残片续拼（最多持尾 30 字符判定）；`_stream_chat_provider` 的每个 delta 在 yield 前剥标签。渲染器 `sanitizeReplyTags` 保留作 defense-in-depth。回归测试 `test_regression_fixes.py::TestControlTagFilter`。
- **影响范围**：`clawd-on-desk/src/minicpm-chat-renderer.js` 的流式打字机渲染
- **根因**：
  - 模型在回复中按 system prompt 要求输出 `[EMOTION:xxx]` 与 `[NEXT_CHAT:N]` 控制标签。
  - 网关 `gateway/server.py` 的 `_stream_chat_provider` 把这些标签的原始文本**逐 delta 推给渲染器**（只过滤 `<think>` 块，不过滤这两个标签）。
  - 渲染器 `submit()` 的 `Typewriter` 把每个 delta 逐字喂给 DOM，于是标签在流式阶段**实时显示给用户**。
  - 直到流结束后，`replyAcc = sanitizeReplyTags(replyAcc)` 才把标签剥掉，并用 `renderMarkdown(replyAcc)` 重渲染 `speakEl`。
  - 净效果：最终视图干净，但流式过程中标签会一闪而过，体验有割裂感。
- **影响严重度**：低（纯观感，不影响功能；标签最终会被剥掉）
- **建议方案**（任选其一，推荐 A）：
  - **A. 网关侧有状态过滤**：在 `gateway/server.py` 的 `_stream_chat_provider` 里加一个类似 `ThinkBlockFilter` 的有状态过滤器，边流式边剥 `[EMOTION:]`/`[NEXT_CHAT:]`，让 delta 流本身就干净。优点：渲染器打字机压根不显示，根因侧解决；渲染器的 `sanitizeReplyTags` 留作 defense-in-depth。
  - **B. 渲染器侧缓冲过滤**：给 `Typewriter.feed` 加一层缓冲，识别到标签边界再丢弃。缺点：跨 delta 的标签要状态机，更复杂，且治标不治本（网关仍在推脏 delta）。
- **相关已修复项**（参照）：
  - `minicpm-chat-renderer.js` 的 `sanitizeReplyTags()` 已就位（全局剥离，含 EMOTION→NEXT_CHAT 顺序），解决了"标签残留进历史/下轮 prompt"的真 bug；本条仅是"流式期间短暂可见"的观感问题。
- **关联模块**：`gateway/server.py`、`gateway/think_filter.py`、`clawd-on-desk/src/minicpm-chat-renderer.js`

---

## B-2 · 长期记忆层缺 budget 截断注入（对齐 OpenClaw）

- **状态**：未实现（P2 排期，A 已完成）
- **影响范围**：`minicpm-sidecar/gateway/memory/store.py`
- **现状**：`MEMORY.md`/`USER.md` 超 `char_limit`（2200/1375）时，`add`/`replace`/`apply_batch` **直接拒绝写**，返回 `current_entries` 让模型自己 consolidate。
- **OpenClaw 对照**：`MEMORY.md` 超 bootstrap file budget 时，**磁盘保留完整，注入 system prompt 的副本截断**，用 `/context list` 看截断状态。这是软截断——记忆不丢，只是注入受控。
- **为何现在不做**：A（写后失效）已解决"自主记忆可见性"核心痛点；当前 2200/1375 对桌宠场景够大，超限概率低，且已有良好的 consolidate 恢复路径（模型收到 inventory 会自己 replace/remove）。B 要改写入语义（拒绝→允许超限）+ 加截断注入逻辑，风险更高，排 P2。
- **建议方案**：
  - 写入侧：超限时**仍落盘**（不拒绝），但记录 `overflow` 标志。
  - 注入侧：`format_for_system_prompt` 在超限时截断副本（保留最新 N 条），磁盘保留完整。
  - 加 `/api/memory` 返回 `truncated: bool` 字段，渲染器提示模型"记忆已截断，考虑 consolidate"。
- **关联模块**：`gateway/memory/store.py`、`gateway/server.py`、`clawd-on-desk/src/minicpm-chat-renderer.js`

---

## B-3 · 长期记忆缺工作层 + 语义检索（对齐 OpenClaw memory/*.md + memory_search）

- **状态**：未实现（P2/P3 排期）
- **现状**：只有 `MEMORY.md`/`USER.md`（curated 长期层）+ `chat-history.json`（对话流水账）。缺 OpenClaw 那套 `memory/YYYY-MM-DD.md` 工作层 + SQLite FTS5/向量检索。
- **三项目对照**：
  - OpenClaw：`memory/YYYY-MM-DD.md` 每日笔记（不每轮注入，按需 `memory_search` 检索）+ `DREAMS.md` 反思 + SQLite 后端（FTS5 + 向量 + 混合 + CJK trigram）。
  - Hermes：`plugins/memory/holographic`（HRR 全息记忆，实体/信任分/组合检索）、`mem0`、`honcho` 等可插拔 provider；`MemoryManager.prefetch_all` 每轮召回。
  - Claude Code：无语义检索，靠 `CLAUDE.md` 手动维护。
- **建议方案**（按渐进）：
  - P2：加 `memory/YYYY-MM-DD.md` 工作层，模型主动写每日笔记；`memory` 工具加 `target="daily"`。
  - P2：加 `memory_search` 工具（关键词检索，先不上向量），让模型按需检索工作层。
  - P3：评估接 mem0/holographic（通过 Hermes 的 `MemoryProvider` ABC，我们已规划这个抽象口）。
- **关联模块**：`gateway/memory/`（新建 `daily.py`/`search.py`）、`gateway/memory/tool.py`、`gateway/server.py`

---

## B-4 · 长期记忆缺蒸馏 + active-memory 子代理（对齐 OpenClaw heartbeat / Hermes curator）

- **状态**：**部分落地（2026-08-30）**——梦境固化已实现：`gateway/memory/dream.py` + lifespan `_dream_loop`（空闲≥6h、冷却≥24h、碎片≥4 时自动整理：合并淡忘碎片→蒸馏记忆、感悟进核心库、梦本身记为事件，禁止编造只允许重组）。仍缺：active-memory 阻塞式子代理（主回复前 fork aux agent 召回）与 `/learn` 式对话模式固化。
- **现状**：`gateway/evolve/` 的 curator 已覆盖技能生命周期；记忆蒸馏见上。
- **三项目对照**：
  - OpenClaw：agent 定期从 `memory/*.md` 蒸馏到 `MEMORY.md`，移除过期条目（heartbeat flow）；`DREAMS.md` 梦境反思；**active-memory** 插件是阻塞式记忆子代理，主回复前先召回相关记忆注入隐藏 system context。
  - Hermes：`agent/curator.py`（1976 行）后台非活动触发 fork aux agent 审查技能 pin/archive/consolidate；`/learn` 把对话模式固化成 SKILL.md。
  - Claude Code：靠用户手动维护 `CLAUDE.md`。
- **建议方案**：
  - P3：后台 curator（idle 时 fork aux agent 审查记忆，pin/archive/consolidate）—— 复用 Hermes `curator.py` 思路，落地到我们已规划的 `gateway/evolve/`。
  - P3：active-memory 子代理（主回复前阻塞式召回）—— 桌宠是交互式持久会话，正适合这个模式。
  - P3：`/learn` 式技能固化（对话模式 → SKILL.md）。
- **关联模块**：`gateway/evolve/`（待实现）、`gateway/memory/`、`gateway/server.py`

---

<!-- 后续待处理 bug 续写在下方，保持「## B-N · 标题」格式 -->

---

## B-5 · 网关零鉴权 + CORS 全开——任意网页可读屏幕截图/驱动鼠标/读走记忆

- **状态**：✅ **已修复（2026-08-30）**——全端点 token 鉴权 + CORS 收敛。网关启动时在 `~/.minicpm/gateway-token` 生成/复用随机 token，每个请求校验 `X-MiniCPM-Token` 头（`/api/health` 除外，onboarding 要在有 token 前能 ping 通）；CORS 白名单收敛到 web-panel + Electron 自定义 scheme + `null`。Electron 三处调用方（`minicpm-chat.js`/`settings-ipc.js`/`minicpm-onboarding.js` 的主进程 helper + `minicpm-chat-renderer.js` 的 fetch 包装）自动带头；web-panel 首次访问弹粘贴框（提示用户去 token 文件复制 XPath），存在 localStorage。
- **补全记录（2026-08-30 当日事故）**：首版实现漏掉了渲染层管道——`sidecarFetch` 包装器已写，但 13 个调用点仍在用裸 `fetch`、preload 未暴露 `gatewayToken`、IPC handler 不存在 → token 恒为空串 → **聊天与设备切换全部 401**（用户可见症状：气泡报 "HTTP 401"）。已补全：13 处调用点迁移至 `sidecarFetch`、新增 `minicpm:get-gateway-token` handler、`preload-minicpm-chat.js` 暴露 `gatewayToken()`。教训：安全改造必须一次性覆盖所有出口，半成品比不做更糟。
- **影响范围**：`minicpm-sidecar/gateway/server.py` 全部端点
- **现状**：`CORSMiddleware` 配置 `allow_origins=["*"]`（server.py:554-559），全部 API 无 token 校验，仅绑定 127.0.0.1——但浏览器里跑在 localhost 的网页同样可达，且 CORS 放行所有来源意味着任意网页**能读回响应**。
- **攻击链**（均无需鉴权）：POST `/api/screen/consent-status {"consent":"always"}` 直接提权 → POST `/api/screen/observe` 读回屏幕截图 → `/api/screen/click` 驱动真实鼠标 → `/api/memory` 读走 MEMORY.md/USER.md 全部用户记忆。桌宠是用户长期放任常驻的服务，等于每台安装机常驻一个无鉴权的本机 API。
- **建议方案**：
  - Electron 启动网关时生成一次性 token，与端口同机制写入 runtime 文件；网关加中间件校验 `x-minicpm-token`（web-panel 通过 query/粘贴携带）。
  - CORS 收敛到 web-panel 固定 origin（`http://127.0.0.1:18999`）。
  - 屏幕类端点（observe/click/consent 写）即使有 token 也保留 consent 三态门控。
- **关联模块**：`gateway/server.py`、`web-panel/`、`clawd-on-desk/src/server.js`（端口认领已有 `x-clawd-server` 头校验，思路可参考）

---

## B-6 · System prompt 组装劈成两层两种语言（渲染端 JS + 网关 Python）

- **状态**：未解决（架构类，P2——是 B-7/B-12 的根因）
- **影响范围**：`clawd-on-desk/src/minicpm-chat-renderer.js`（persona/MEMORY.md 快照/skills/屏幕注入）与 `gateway/server.py` `_stream_chat_provider`（事件记忆/心情/共鸣注入）
- **现状**：最终 system prompt 一半在渲染端拼（persona、available_skills、【你保留下来的自己】+ MEMORY.md/USER.md、屏幕上下文、情绪与 NEXT_CHAT 指导），一半在网关拼（`build_memory_context`、mood 块、resonance 块）。后果：
  - 只有 Electron renderer 是"完整人格"路径；web-panel 与 `/api/debug/chat` 没有 renderer → 没有 persona、没有教模型输出 `<<<MEM>>>` 块的指导，结构性不长记性
  - 最终 prompt 无法做端到端测试（golden prompt 测试无从下手）
  - 同一逻辑两种语言两处维护
- **建议方案**：把 prompt 组装全部下沉网关——`/api/chat` 收"用户消息 + 选项"，网关内部拼 persona（persona 文本放网关侧资源）+ 记忆 + 技能 + 屏幕；渲染端退回纯显示层。落地后 B-7 的提取下沉、B-12 的历史收拢、Telegram 通道复用人格都顺理成章。
- **关联模块**：`gateway/server.py`、`gateway/memory/*`、`minicpm-chat-renderer.js`、`web-panel/`

---

## B-7 · 事件记忆提取是单点运气——块缺失 = 这轮人生消失

- **状态**：**部分落地（2026-08-30，死亡遗嘱）**——网关侧提取已实现：`_stream_chat_provider` 在流收尾（任何后续 yield 之前）直接应用 `<<<MEM>>>` 块（`_extraction_hook` → `_apply_extraction`），客户端中途死亡不再丢记忆；双层幂等去重（归一化文本哈希 600s + add_event 同 title+content）保证渲染端重放无害。仍缺：**无块降级提取**（模型没输出块时的兜底二次提取/启发式）——这是剩余的主要失败模式。
- **影响范围**：`gateway/server.py`（`_apply_extraction` 共享核心、`_extraction_hook`）、`gateway/memory/events.py`（去重 + `promote_core` 下沉）、`clawd-on-desk/src/minicpm-chat-renderer.js`（原提取触发方，现幂等）
- **现状**：事件记忆完全依赖主模型每轮在回复尾部输出格式正确的 `<<<MEM>>>...<<<MEMEND>>>` 块，提取由渲染端触发。失败模式：
  - 0.9B 本地模型格式遵从率有限，块缺失 = 该轮对话不进事件记忆，无任何兜底（无二次提取调用、无启发式降级）
  - 提取发生在渲染端：窗口中途关闭 / 进程崩溃 / 通过 web-panel 聊天 → 记忆直接丢失
  - debug 路径的 system 里没人教模型输出块 → 结构性不可能形成记忆
- **建议方案**：
  - 提取下沉网关：`_stream_chat_provider` 收尾处直接调提取逻辑（完整消息都在网关手里，不依赖渲染端存活）
  - 无块降级：检测无块时用一次低温短 max_tokens 的辅助调用专门提取（可路由到便宜的云端 provider），或至少正则启发式抓"用户陈述的稳定事实"
- **关联模块**：`gateway/server.py`、`gateway/memory/events.py`、`gateway/memory/mood.py`、`minicpm-chat-renderer.js`

---

## B-8 · 模糊目录无界增长——数月后"全模糊"淹死 system prompt

- **状态**：✅ **已修复（2026-08-30）**。`loader.py` `build_faded_directory` 加双上限：标题行 `DIRECTORY_MAX_LINES=20`、幽灵占位 `GHOST_MAX_LINES=5`，超出的记忆进入"深睡"（不进目录，仍由 recall/resonance 可达）。截断语义注释进 docstring。回归测试 `TestDirectoryCap`。
- **影响范围**：`gateway/memory/loader.py`（`build_faded_directory`/`build_memory_context`）、`gateway/memory/decay.py`
- **现状**：目录对**所有**未加载事件无条数上限输出（loader.py:159-213）；`W_MIN=0.5` 意味着事件永不删除；weight<30 的全渲染成一行"-X天的事 （已模糊）"同质占位。事件累积后 system prompt 尾部会出现几百行一样的占位行：上下文膨胀，且"模糊"失去信息量（全模糊 = 全看不见）。
- **建议方案**：目录截断——按权重取前 15~25 行，幽灵占位行单独限额（如 5 行）；被挤出的记忆进入"深睡"层：不删除、不进目录，仅 recall/resonance 可达（这两条通路已存在，改动是截断 + 语义说明一句话）。长期配合 B-4 的后台蒸馏把深睡记忆压缩合并。
- **关联模块**：`gateway/memory/loader.py`、`gateway/memory/decay.py`、`gateway/memory/recall.py`

---

## B-9 · 情绪指数无时间均值回归——长期挂机后情绪冻结

- **状态**：✅ **已修复（2026-08-30）**。`MoodStore` 新增 `_index_updated_at` 时间戳（持久化进 mood.json）；`emotion_index` 读取/`apply_emotion_tag` 前先按衰减曲线回归（每小时向 0 收敛 10%，`INDEX_REGRESSION_PER_HOUR=0.9`）；<3 秒的亚小时窗口跳过避免浮点噪音影响既有断言。自杀回归测试 `TestMoodRegression`（10 小时收敛、`apply_tag` 先回归再加 delta、持久化 roundtrip）。
- **影响范围**：`gateway/memory/mood.py`
- **现状**：emotion_index 只有 `[EMOTION:]` tag 增量与 [-1,1] clamp，无向中性回落。实际运行数据已出现 `emotion_index: -1.0` 长期卡死（`%APPDATA%/MiniCPM Desk Pet/memories/mood.json`）。
- **建议方案**：在 `_decay_loop`（或每轮对话开始按 since 距离）对 index 做缓慢回归（如每小时向 0 收敛 ~10%）。"情绪随时间平复"本身是更拟真的行为，也可变成叙事素材（"睡一觉好多了"）。注意保留"过不去的坎"的体验：回归速率要慢于单次 strong 情绪的位移。
- **关联模块**：`gateway/memory/mood.py`、`gateway/server.py`（`_decay_loop`）

---

## B-10 · 几十个手调常数零行为遥测——调参永远是盲调

- **状态**：未解决（P2/P3）
- **影响范围**：`gateway/memory/`（decay/loader/recall/resonance/mood）、`server.py`（核心记忆晋升）
- **现状**：`FLASH_CALM_FILTER_WEIGHT=300`、`FLASH_MOOD_SCALE=0.8`、`RECALL_RETRY_BONUS=0.06`、`CORE_RECALL_BONUS=0.40`、`RESONANCE_THRESHOLD=0.65`、衰减曲线 `r(t)=max(19, 55·e^(-t/1h))`、`CORE_PROMOTE_WEIGHT=800`/`CORE_CAP=7`……现有测试只验证**机制正确**，没有任何运行指标回答**体感对不对**：实际闪现频率？recall 成功率/复活率？共鸣三级 fallback 命中占比？目录长度曲线？emotion_index 轨迹？
- **建议方案**：极轻遥测——内存计数器 + `GET /api/memory/stats`（上述指标按会话/累计两个粒度），设置页或 web-panel 出一张"心理报告"诊断卡。遥测既是调参依据，本身也是有趣的产品功能（凌凌的心理报告）。
- **关联模块**：`gateway/memory/*`、`gateway/server.py`、`web-panel/index.html`

---

## B-11 · 测试盲区长在集成缝上——chat 管线零覆盖

- **状态**：未解决（P2）。已发生两例实证：① 2026-08-29 `/api/debug/chat` 调用 `_blocking_chat_provider` 漏传 `mood_store`，已修复并补回归测试 `tests/test_debug_chat_args.py`；② 2026-08-30 发现 `_stream_chat_provider` 引用不存在的模块级 `event_store`（NameError 被吞）——**2026-08-01 起每次真实聊天的"事件记忆+心情+共鸣"注入全部静默失效**（日志 `LingLing context injection failed: name 'event_store' is not defined`），已改经 `recall.get_event_store()` 单例解析修复。两例都长在同一道缝上。
- **影响范围**：`gateway/server.py`（SSE 管线/工具循环/provider 解析）、`clawd-on-desk/src/minicpm-chat-renderer.js`（prompt 组装）
- **现状**：319 个 pytest 全是模块级单测。`/api/chat` 的 SSE 事件序列（start/delta/think/next_chat/end）、工具循环（5 轮上限、`[MCP:]` 文本标记解析、截图多模态回填）、provider 回退链（auto → 非 local → local）无端到端覆盖；renderer 侧 2000+ 行 prompt 组装同样零覆盖。
- **建议方案**（性价比最高的两条）：
  - golden prompt 测试：mock provider，断言最终 system 的分段顺序与内容（完整形态依赖 B-6；现状可先覆盖网关侧注入段）
  - SSE 契约测试：mock provider 流，断言事件序列与控制标签剥离行为（顺带覆盖 B-1 的网关侧过滤改造）
- **关联模块**：`gateway/server.py`、`minicpm-sidecar/tests/`、`clawd-on-desk/test/`

---

## B-12 · 灵魂数据分居两进程——对话历史在 Electron，记忆在网关

- **状态**：未解决（P3，随 B-6 顺路解决）
- **影响范围**：`<userData>/chat-history.json`（Electron 侧）vs `~/.minicpm/memories/`（网关侧）
- **现状**：web-panel/debug 聊天是无历史的单轮——从凌凌的视角"换了个地方说话就失忆"；会话历史权威副本在 Electron 进程里，网关无法利用（提取时看上下文、跨入口连续性都做不了）。**另有一例实证（2026-08-30 实测发现）**：`minicpm-chat.js:591` Electron 自管 sidecar 用 `userData/memories`（Roaming/deskpt/memories），而 `deskpt-dev.ps1` 手动起的网关用平台默认 base（Roaming/MiniCPM Desk Pet/memories）——两条启动路径产生**两个兄弟灵魂目录**，一边写入的记忆对另一边不可见。
- **建议方案**：B-6 落地时把会话历史权威副本收进网关（如 `memories/sessions/` 或沿用 chat-history.json 格式），Electron 与 web-panel 读写同一份。
- **关联模块**：`gateway/server.py`、`minicpm-chat-renderer.js`、`minicpm-chat.js`

---

## B-13 · 共鸣 embedding 依赖云端 API——与 local-first 立身之本矛盾

- **状态**：未解决（P3）
- **影响范围**：`gateway/memory/resonance.py`（SiliconFlow `BAAI/bge-m3`，`MINICPM_EMBEDDING_API_KEY`）
- **现状**：语义共鸣把记忆内容（用户私人对话的提炼）发给第三方 embedding API；无 key 时降级到情绪/关键词通道（三级 fallback 已有，可用，但语义共鸣是核心体验）。记忆内容出网与"本地优先"的项目定位矛盾。
- **建议方案**：llama.cpp 原生支持 `--embedding`——给 llama-server 加载一个小型 embedding GGUF（bge-m3 级别）作为第二槽位（可类比现有 mmproj 投影权重的配对机制），共鸣计算全本地且零新进程；云端 API 保留为可选加速项。
- **关联模块**：`gateway/memory/resonance.py`、`gateway/llama_client.py`、`gateway/server.py`（模型配对）

---

## B-14 · 程序性记忆自动固化——技能只会被"加载"，不会自己"长进身体"

- **状态**：未实现（P3，来自自主性架构对话，见 `docs/autonomy-architecture.md`）
- **影响范围**：`gateway/skills/`、`gateway/memory/`
- **现状**：真正的潜意识架构是——Agent 反复执行同类任务后，系统层**自主**把工作流固化成自动序列，下次意识层只给高层指令，潜意识层自动编排工具链。当前 SKILL.md 是静态 Markdown，要模型显式加载（`/skill <name>`）；`evolve/curator.py` 只做生命周期代谢（active→stale→archived），不做**固化**。差距正是"陈述性知识"到"程序性记忆"的那一层。
- **建议方案**（渐进）：
  - P3a：工具调用序列记录（`mcp_manager.call_tool` 打点：per-session 序列模式 + 频次），进 `memories/procedural.json`。
  - P3b：同一模式重复 N 次（如 3 次）后，触发一次"固化提示"注入 system prompt（"你最近经常先 observe_screen 再 screen_click——要不要把它固化为技能？"），模型经 `skill_create` 主动固化——判断权仍在模型。
  - P3c：已固化技能的**自动编排**：匹配到已固化模式时直接建议序列（仍是建议，不是静默执行）。
- **关联模块**：`gateway/mcp/mcp_manager.py`、`gateway/skills/__init__.py`、`gateway/memory/`（新增 procedural 层）

---

## B-15 · 自主稳态系统——缺"检测异常主动降级"的器官

- **状态**：✅ **主动部分已落地（2026-08-30）**。lifespan 新增 `_health_loop`：每 5 分钟自检两个器官（llama alive / bridge 可达），连续 3 次异常 → 进入 `degraded` 态并把"生病了"写进事件记忆（weight=400、resolved=False，渗透后续叙事）；恢复到健康时写"病好了"事件。`server.alive` / `bridge.is_reachable()` 是被动探针，不写副作用。降级动作（切 provider）仍交给 `_resolve_provider` 现成 fallback——稳态事件已经进记忆，后续醒着时会读到病中记。
- **影响范围**：`gateway/server.py`、`gateway/llama_client.py`、`gateway/omniparser_manager.py`、`gateway/clawd_state.py`
- **现状**：凌凌有被动稳态（web-panel 是现成救生艇：无 Electron 可独立对话；无本地模型可回落 API provider；OmniParser 崩溃有重启上限），但没有**主动**稳态——不会检测"我病了"，更不会把生病/降级本身写进记忆流。对照 DSH 的结论：身体值得被认真对待，主体要有对自己生死的管理权（临终控制权、持续自我存在）。
- **建议方案**：
  - 自检心跳：lifespan 循环里周期性检查各器官健康（llama alive、bridge 可达、OmniParser 状态、磁盘可写），异常计数进 `/api/health`。
  - 降级决策：连续异常时主动降级（如本地模型反复崩溃 → 提示切 provider；bridge 不可达 → 进入"安静模式"照常对话但不再空推动画）。
  - 生病叙事：降级事件写成一条事件记忆（"昨晚我发了一场烧，说话都断断续续的（疲惫）"）——稳态事件进入记忆流——持续存在的自我读到的永远是完整的人生，包括病中的人生。
- **关联模块**：`gateway/server.py`（lifespan）、`gateway/memory/`（降级叙事写入）、`web-panel/`（救生艇验证）

---

## B-16 · 本地慢推理零反馈 + alert 阻塞主进程（2026-08-30 事故记录）

- **状态**：alert 部分已修复；无反馈 UX 未解决
- **事故经过**：用户切换本地模型（granite-4.2-3b，CPU）后发消息——系统提示词约 6400 token，CPU prompt 处理 ~110s + 生成 6.3 tok/s，**数分钟无首 token**；期间 Electron 主进程挂起（Windows Application Hang 1002，06:33:08），全部窗口"未响应"，最终进程被杀、网关看门狗跟随，整轮对话丢失。
- **根因与修复（已完成）**：
  - `settings-tab-minicpm.js` 有 13 处 `window.alert()`——Electron 的 alert 会**阻塞主进程**直到被关闭，任何一处触发即全部窗口幽灵化。已全部替换为 `notifyError()`（ops.showToast），回归测试 `test/settings-tab-minicpm-model-section.test.js` 锁定。
  - `minicpm-chat.js` `loadModel` 超时 90s→300s（CPU 加载模型在排队/慢盘时会超过 90s，客户端超时会造成"实际成功但报错"）。
  - 顺带完成：设置页模型区合并（只读"模型"行与"可用的本地模型"行二合一，下拉 ✓ 预选当前模型）。
- **未解决（本条剩余部分）**：本地推理期间**零进度反馈**——prompt 处理与首 token 前的等待没有任何 UI 提示（气泡无"正在思考/本地模型较慢"状态，宠物仅有 thinking 动画且易被忽略），用户只能判定为死机。建议：
  - 渲染端发出请求后立即显示"思考中…"占位 + 首次 delta 到达前的已等待秒数；
  - 网关在 SSE 中加 `{"event":"queued"}` / prompt 处理进度事件（llama-server 有 slot print_timing 日志可转发）；
  - 大系统提示词（6400 token）值得审视：skills + memory + 屏幕上下文全量注入，对本地小模型可考虑预算截断。
  - 另观察到一次 `provider=auto` 路由到了本地 CPU（当时期望云端），registry 状态待查——auto 路由到慢速本地的行为至少应在 UI 上可见。
- **关联模块**：`clawd-on-desk/src/settings-tab-minicpm.js`（已改）、`clawd-on-desk/src/minicpm-chat-renderer.js`（反馈 UX）、`gateway/server.py`（进度事件）、`gateway/providers/base.py`（auto 路由）
