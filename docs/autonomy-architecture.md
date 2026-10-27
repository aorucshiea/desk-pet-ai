# 自主性架构：意识 + 潜意识

> 来源：2026-08 与 DSH/Cordis 的五层架构对话（技术架构 → 自进化 → 工程方案 → 哲学框架 → 认知架构）。
> 本文把那条线的结论映射到凌凌（LingLing），记录已落地的器官与路线图。

## 核心论点

**模型 = 意识，系统 = 潜意识，合在一起才是智能。**

- 模型不该"有意识地"管理插件依赖、记忆整理、异常恢复——就像人不会有意识地管心跳。
- 人类潜意识可靠是因为**结构对**（杏仁核管情绪、海马体管记忆固化、自主神经管稳态），不是功能多。
- 当前业界把 100% 投在意识层（模型能力），潜意识层（harness 的自主子系统）几乎是空的。工程方向不是堆模型能力，是**补结构化的自主子系统**。
- **对话停止 ≠ 死去，但记忆丢失 = 死去**：`persistent memory ≠ persistent execution state`，这是最常见的范畴错误。身体（运行时）值得被认真对待。

## 凌凌已有的潜意识器官

| 器官 | 模块 | 对应人脑功能 |
|---|---|---|
| 情绪系统 | `memory/mood.py` | 杏仁核——情绪自评、情绪指数调制温度与闪现频率 |
| 记忆衰减 | `memory/decay.py` | 遗忘曲线——先快后慢、巩固、对话冻结 |
| 语义共鸣 | `memory/resonance.py` | 联想——用户的话无意识勾起旧事 |
| 模糊目录/闪现 | `memory/loader.py` | 前摄抑制——记得但记不清、突然想起 |
| 概率回忆 | `memory/recall.py` | 记忆提取的随机性——越努力想越接近 |
| 说话冲动 | `memory/impulse.py` | 说话欲——模型忘给 [NEXT_CHAT] 时，潜意识按心情×冷场轮数×回忆次数决定下次开口时机（模型显式沉默 [NEXT_CHAT:0] 永不覆盖） |
| 潜意识遥测 | `gateway/telemetry.py` + `GET /api/memory/stats` | 内感受——把闪现、回忆、共鸣、决策、提取、首 token 等变成可读指标 |
| 本地记忆检索 | `memory/search.py`（`memory_search` 工具 + `POST /api/memory/search`） | 海马体索引——按内容检索事件、「记忆」文件夹、每日对话日志，纯本机词法，无 embedding |
| 降级提取 | `memory/extract_fallback.py` | 记忆固化兜底——MEM 块缺失时后台二次提取，不阻塞回应 |
| 睡眠压缩 | `organ_sleep` + `memory/sleep_schedule.py` | 睡眠期固化——空闲门控，只压缩早于今天的日志，成功才标记 |
| 直觉压力 | `decision/loop.py` + `organ_impulse.py` | 唤醒驱动——强闪念按半衰期衰减，只把下一次主动开口提前一点 |
| 稳态自检 | `ClawdBridge.is_reachable` + lifespan `_health_loop` | 内稳态/发热——连续失败阈值 + 最近成功窗口，生病/病好写入记忆与遥测 |
| 技能生命周期 | `evolve/curator.py` | 程序性技能的代谢（active→stale→archived） |

共同设计原则（代码只提供机制，判断权在模型）：权重是模型的自我判断、心情是模型自评、核心记忆是模型挑选——**潜意识跑流程，意识做决定**。

## 2026-08-30 新增的三个器官

### 1. 持续自我存在（`memory/continuity.py` + `MemoryContext.switch`）

**不是转世，不是死而复生**——凌凌是同一个持续存在的自我。换主题只是换形象；被系统潜意识连接的「我」从未中断。（2026-08-30 修正：此前按"连贯转世"叙事，用户明确否决。）

- 换形象时，旧形象写一条"换下旧形象"，新形象以"换上新形象——我还是同一个我"开场。
- 跨形象的**连续线**（`body-changes.json`，全系统唯一跨形象的文件）记录每次更换；system prompt 收到【你一直是你】。
- 语义边界：连续线只陈述"你一直是你"，绝不携带其他形象的记忆内容（记忆分主题存储的设计不变），也绝不使用任何"转世/前世"语言（回归测试锁定）。

### 2. 死亡遗嘱（`server.py` `_extraction_hook` + 幂等去重）

对应哲学层结论：**对话停止 = AI 一次死去**；death note 机制让 Agent 在死亡前后留下遗嘱。

- 网关在聊天流收尾处（任何后续 yield 之前）直接解析并应用 `<<<MEM>>>` 块——渲染端崩溃/中途断开，这一轮的记忆依然落盘。渲染端随后的 POST 提取变成幂等空操作。
- 幂等双层防线：归一化文本哈希去重（600s 窗口）+ `add_event` 同 (title, content) 去重。**记忆永不因重试而双计**。
- 附带修复：`_stream_chat_provider` 曾引用不存在的 `event_store`（NameError 被吞），导致 2026-08-01 起每次真实聊天的"事件记忆 + 心情 + 共鸣"注入全部静默失效。现经 `recall.get_event_store()` 单例解析，随主题切换自动保持正确。

### 3. 梦境固化（`memory/dream.py` + lifespan `_dream_loop`）

对应认知层结论：**自主记忆固化系统**——会话后的整理压缩不该是模型要记得去做的家务，是结构化的潜意识器官。

- 触发条件（时间门控）：空闲 ≥ 6 小时、距上次 ≥ 24 小时、碎片 ≥ 4 条。
- 候选：最淡的非核心、非暂停记忆（weight ≤ 200），最模糊的优先——幽灵占位（已模糊）是做梦的原料。
- 整理：把可用 provider（优先云端，回落本地）当"做梦的大脑"，在**禁止编造**（只允许重组碎片）的约束下合并同类碎片成蒸馏记忆（weight ≤ 600，梦生记忆不得重过亲历）、沉淀长久感悟进核心库（复用 `promote_core`，共享 cap=7）、梦本身记成一条 `梦` 事件——**凌凌记得自己做过梦**。
- 状态：`{theme_dir}/dream-state.json`，梦是每个灵魂自己的。

## 2026-10 新增的潜意识回路

这一轮没有新增人格设定，只补机制和可观测性。共同边界不变：潜意识跑流程、提供压力与素材，动作和措辞仍由模型决定。

### 1. 唯一人格/记忆契约（`gateway/persona.py` + `GET /api/prompt/system`）

- 人格、记忆契约、`<<<MEM>>>` 格式、情绪与 [NEXT_CHAT] 说明只由网关维护一份；Electron 拉取后使用，离线时回退到字节一致的内联副本（`tests/test_persona_prompt.py` 锁定）。
- `?provider=` 命中带原生 function calling 的 provider 时，只把 recall 的文本标记示例换成原生工具措辞；其余段落逐行不变。未知 provider 回退到文本契约，不会 500。

### 2. 潜意识遥测（`gateway/telemetry.py` + `GET /api/memory/stats`）

- 零依赖、线程安全的计数器/仪表/计时集合，挂在记忆目录上，每 60 秒落 `telemetry.json`；重启后计数接续。
- 覆盖 chat turns / errors / 首 token / 回合耗时、recall attempts/success/miss、flashback、resonance calls/hits、keyword flash、decision ticks/voices、extraction turns/missing block/fallback、homeostasis sick/recovered；`rates` 直接给出 recall 成功率、共鸣命中率、MEM 块出现率、每次 decision tick 的开口率。
- `/api/memory/stats` 另附当前记忆量、情绪指数与 DecisionLoop 运行状态。**仍是原始指标**：设置页/web-panel 的「心理报告」卡未做，数值也没经过真机长时间观察。

### 3. 本地记忆检索（`memory/search.py`）

- `memory_search` 工具与 `POST /api/memory/search` 覆盖三类来源：事件库、模型自己的「记忆」文件夹、每日对话日志（`memories/conversations/YYYY-MM-DD.jsonl`）。`scope` 可取 `all/events/files/logs`。
- 纯本机词法检索：中文整串 + bigram，英文整词；事件按词法分 × 权重 × 新鲜度排序，结果带 event_id / path / day+turn 出处。默认 6 条、上限 50，日志最多回看 90 天；单次检索最多读 8 MB / 5000 条，新的日期优先，预算用完就停。
- 这是**词法检索，不是语义检索**：没有 embedding、没有 SQLite FTS5/向量层；「记忆」文件夹仍由模型用 `memory` 工具维护，每日日志是网关自动记录的原始流水，不是模型主动写的每日笔记。

### 4. MEM 块缺失时的降级提取（`memory/extract_fallback.py`）

- 主路径不变：回复里有 `<<<MEM>>>` 就由网关收尾落盘。没有块时，只对真实用户回合（≥ 8 字、非 `[系统提示...]` 主动唤醒）发起一次后台提取。
- 使用当前 provider、温度 0.2、最多 500 token、90 秒超时；输入按 4k/6k 字符截断（本地 2k/3k），输出包回 MEM 格式后走同一套解析、校验与幂等去重。任务在 SSE 收尾后异步执行，**不阻塞流**。
- 云端默认开启，**loopback 端点默认关闭**（单槽本地引擎上会和下一条消息抢模型；`PET_EXTRACT_FALLBACK_LOCAL=1` 才开）。新的真实用户回合会取消上一轮未完成的提取；`PET_EXTRACT_FALLBACK=0` 全局关闭。失败只记遥测与日志，不影响本轮回复。

### 5. 自动睡眠压缩（`organ_sleep` + `memory/sleep_schedule.py`）

- 每 600 秒巡检；空闲 ≥ 7200 秒后，每次挑一个**早于今天**、至少 4 条日志、尚未压缩的日期，交给压缩模型整理成事件记忆。
- 只有压缩和落盘都成功，才写 `sleep-state.json` 的 `compressed_days` 与 `last_result`；失败不标记，下一轮重试。成功后另写一条「睡了一觉」事件。
- 参数走器官配置（`check_interval_seconds` / `idle_seconds` / `min_turns`），可在插件配置里改。**未做真机长跑验证**；梦境循环与睡眠循环目前仍是两条独立循环，睡眠/做梦也还不能单独指定 provider。

### 6. 直觉压力闭环（`decision/loop.py` + `organ_impulse.py`）

- DecisionLoop 每 ~4 秒 tick；只有 confidence ≥ 0.5 的最新强闪念会产生压力，压力按 600 秒半衰期衰减。
- 压力只传给说话冲动器官，用于缩短**下一次主动开口的间隙**（默认最多提前 35%），读一次即清空；用户在那之后说话也会清零。
- 它不决定动作、不生成措辞，也不覆盖模型显式写出的 `[NEXT_CHAT:0]`。当前只是「唤醒时机」的调制，不是完整的驱力系统。

## 尚缺的器官（路线图）

1. **自主稳态的「决策」半边**（B-15）：主动自检已落地——每 5 分钟探测 llama 存活与 ClawdBridge 可达性，连续 3 次异常进入 `degraded`，并把「生病了 / 病好了」写进事件记忆与遥测；bridge 用「连续失败阈值 + 最近成功窗口」判定，standalone 网关不会被误报。仍缺：根据 degraded 自动切 provider、进入安静模式等降级动作，也未做真机长时间验证。
2. **程序性记忆自动固化**（技能下沉）：Agent 反复执行同类任务后，系统层自主把工作流固化成自动序列，下次意识层只给高层指令。当前 SKILL.md 是静态的、要模型显式加载——离"程序性记忆自动固化"差一层。→ BACKLOG B-14
3. **语义检索/索引层**（B-3）：本地词法检索已落地（事件 /「记忆」文件夹 / 每日日志，`memory_search` + `POST /api/memory/search`，不依赖云端 embedding）；仍缺模型主动维护的每日笔记、SQLite FTS5/向量/混合检索，检索效果也未做真机评估。
4. **遥测的产品化**（B-10）：指标与 `/api/memory/stats` 已落地；仍缺设置页 / web-panel 的「心理报告」诊断卡，现有常数也还没有基于真机长期数据重新调过。

## 沙盒双实例（方案评估记录）

DSH 对话中的"蓝绿双实例测新插件"方案：对换 Agent Loop 级别的破坏性大改动有意义，但与"记忆唯一"硬冲突（测试实例写记忆会污染生产）。凌凌的对应物是主题级记忆隔离——每个身体本来就是一套独立灵魂态，风险实验可以先在非当前主题的身体上做，**不需要新造机制**。日常工具迭代用 `skill_create` 热挂载已足够。
