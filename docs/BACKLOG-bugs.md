# 待处理 Bug（Backlog）

本文件记录已诊断、但暂未修复的已知瑕疵与新功能缺口。每条注明根因、影响、建议方案，便于后续排期。

---

## B-1 · 流式过程中 `[EMOTION:]` / `[NEXT_CHAT:]` 标签会一闪而过

- **状态**：未修复（已知瑕疵）
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

- **状态**：未实现（P3 排期）
- **现状**：记忆只增不减，无后台蒸馏/审查。`gateway/evolve/` 是 13 行占位。
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
