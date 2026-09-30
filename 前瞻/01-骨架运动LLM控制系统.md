# 01 · 骨架运动 LLM 控制系统

- **状态**：草案 v1（2026-09-30，待拍板）
- **提出**：机长；**设计**：Kimi（k3）
- **一句话**：LLM 不逐帧驱动骨骼——建一套分层神经系统，LLM 只当大脑皮层，小脑由开源决策模型（Laya）承担，脊髓是代码。

---

## 1. 起点：一个必须先钉死的矛盾

| 事实 | 数值 |
|---|---|
| 本地模型推理带宽 | ~100–200 tok/s，每轮回复总量有限 |
| 骨骼动画所需控制带宽 | 60fps × N 关节 × 每关节数参数 ≈ 每秒数千浮点数 |

**LLM 逐帧驱动骨架在数学上不成立，哲学上也不该成立。** 项目自己的原则已经给出答案（`docs/autonomy-architecture.md`：潜意识跑流程，意识做决定）——逐帧姿态是脊髓的事，不是大脑的事。

「真正的骨架运动 LLM 控制系统」的正确形态是**分层神经系统**，LLM 只占最高层。

## 2. 分层架构

```
L3 大脑皮层  主 LLM（0.9B GGUF）：对话 / 意图 / 叙事
               ↓ 意图标记（[POSE:curious] 等，与说话同流，低频）
L2 小脑      organ_body 插件 ──HTTP──▶ Laya 决策 sidecar（独立进程，322M）
               state JSON（情绪/体感/最近交互/冲动分）
               + 类型化问题（姿态 choice / 能量 score / 小动作 noul）
               → 单次前向 ~33ms → 目标姿态 + 校准置信度
               置信度低于阈值 → 保持当前姿态（不动就是最好的动作）
L1 脊髓      Electron 渲染进程：弹簧阻尼插值 / 约束 / 注视追踪（60fps，纯代码）
L0 骨骼      theme.json 声明的参数化骨架（骨骼层级 + 蒙皮锚点 + 姿态基元）
```

分层的回报：

- **可逆性天然成立**：L1/L0 挂了 → 退回现有 GIF 状态机；Laya sidecar 不在 → organ_body 回退静态规则姿态表；organ_body 被 unload → POSE 标记无人消费，聊天不受影响。**三层降级，每层都无感。**
- **器官同构**：`organ_body` 就是插件内核里的一个普通器官——可热编辑、可回滚，模型将来能用 `pet_forge_plugin` 改写自己的运动神经（自进化闭环的自然延伸）。
- **打对话流为零**：Laya 只做潜意识决策，永不进入主模型 prompt——人格污染面为零。

## 3. 小脑选型：Laya（2026-09-18 开源）

> 事实来源：2026-09-30 联网检索（官网 `laya.convaiinnovations.com`、PyPI、HuggingFace、多个独立中文二手来源交叉一致）。在此之前还有闭源的 Jev（TypeSafe AI，API 计费 $0.042/M tokens，150–276ms 延迟，无开放权重）——不符合 local-first，仅作参照。

### 3.1 关键参数

| 项 | 值 |
|---|---|
| 许可 | Apache 2.0，权重+代码全开源 |
| 架构 | 非自回归双向编码器（ModernBERT-large / mmBERT-base），**不做文本生成** |
| 检查点 | `laya`（英文 421M，512 ctx）· `laya-multilingual`（多语言 322M，1024 ctx，100+ 语言含中文路由）· `laya-typed-decisions` |
| 延迟 | 33ms 单前向/批次 7.2ms 每问题（T4；**CPU 实测值待钉**） |
| 安装 | `pip install laya`（PyPI 0.3.22）；extras: `serve`（自带 FastAPI）、`onnx`（onnxruntime 推理）、`mcp`（MCP server 形态） |
| 多语言 | 内置 Router 按 Unicode 脚本亚毫秒路由，中文走 mmBERT 检查点 |

### 3.2 三个决策原语（单次前向同时评估多个问题）

| 原语 | 输出 | 小脑里的用途 |
|---|---|---|
| `choice` | 选项 key + 概率分布 + 校准置信度 | 「现在该是什么姿态」从 ≤20 个姿态基元中选 |
| `score` | 序数等级期望（0–N）+ 分布 | 动作能量 / 速度 / 张力参数 |
| `noul` | 校准 P(true) | 「现在要不要动一下」「现在开口合适吗」 |

### 3.3 官方自曝的限制 → 我们的设计约束

1. **choice 选项 >20 退化**（Banking77：0.87 → 0.425）→ 姿态基元库必须 ≤20，或做两级层级（先选姿态族、再选具体姿态）。
2. **零样本近随机**（typed-decisions 0.35；0.766 是微调后的数）→ 需要合成数据微调。我们的结构性优势：姿态基元库是代码定义的，训练对（场景描述 → 姿态标签）可以程序生成。官方提供 4 小时 Kaggle 微调 notebook。
3. **校准温度需按问题类型拟合** → 微调后顺手做（ECE 0.466 → 0.081）。

### 3.4 接入方式（已定结论）

- **独立 sidecar 进程**，gateway HTTP 调用。先例现成：`llama_client.py` / `omniparser_manager.py` 均为「gateway 管理子进程」模式，照抄。
- **gateway 保持无 torch 设计不变**（`pet-sidecar/README.md` 写明的原则）。Laya 进程崩了重启即可，gateway 无感。
- `laya[mcp]` extra 是备忘项：将来也可以作为主模型可直接调用的 MCP 工具注册进 `mcp_manager`——但那是「意识主动问小脑」，与器官自主调用是两条通路，不冲突。

### 3.5 超额收益（分阶段再做，第一次只接 organ_body）

现在硬编码手调公式的潜意识器官——`organ_impulse` 说话冲动（心情×冷场轮数×回忆次数）、共鸣强度分级——本质都是「小状态 → 离散决策」，是 Laya 的同类场景。**纪律：一次只改一个变量**，先钉稳 organ_body 再扩散。

## 4. 意识侧协议：扩展文本标记体系

沿用已验证先例（`gateway/think_filter.py` 的 `ControlTagFilter`：跨 chunk 续拼、持尾缓冲、防流式闪烁，B-1 修过的坑）：

| 标记 | 语义 | 消费方 |
|---|---|---|
| `[POSE:name]` | 请求进入某姿态基元 | organ_body（与 Laya 决策做置信度仲裁） |
| `[GAZE:x,y]` | 注视点 | L1 脊髓（眼睛/头部骨骼约束） |
| `[ACT:...]` | 复杂编舞（暂保留，默认走「模型写临时 dance 器官」的 forge 路径） | 插件内核 |

理由：文本标记与说话同流、零额外轮次开销——「边说边比划」是活体感的来源。工具调用（MCP/`ctx.tool`）要抢工具循环，留给「模型主动查小脑」的场景。

## 5. 骨骼资产：theme.json 的声明式扩展（草案）

```jsonc
{
  "skeleton": {
    "bones": [ {"id":"root"}, {"id":"spine","parent":"root","len":60}, "…" ],
    "binds":  { "spine.tip": "tail-trail" },
    "poses":  { "curious": {/*…*/}, "cower": {/*…*/} },
    "fallbackState": "idle"
  }
}
```

GIF 状态机**永远保留为 fallbackState**——骨骼系统全灭也不影响今天的行为（可逆性底线的直接落实）。首个落地候选：**zero 主题**（水滴生物：圆身 + 尾迹本来就是 4–6 骨骼的曲线，pixel 风 GIF 旋转会糊，赛璐璐风天然适合骨骼化）。

## 6. 与现有工程的咬合点（实测过的缝）

| 改动点 | 位置 | 性质 |
|---|---|---|
| 标记剥离扩展 | `gateway/think_filter.py` ControlTagFilter | 纯增量 |
| SSE 事件通道 | `server.py` WALK surface（~2916 行）同类 pose/gaze 事件 | 增量 |
| system prompt 身体语言规范 | `server.py:2871`【身体与进化】块 | ⚠️ 与 P3a 撞点：此块正是 P3a 要抽出的 prompt 段落——协议规范先写成独立常量，P3a 落地时自然带走 |
| 运动器官 | 新插件 `organ_body.py`（inject: mood / conversation_state），标准内核流程 | 全新插件，不碰 server.py 主体 |
| 渲染层 | `clawd-on-desk/src/renderer.js` 旁挂 skeleton-render 模块；theme 无 skeleton 键时完全不动 | **Qoder 地盘，动手前先上板打招呼** |

刻意设计：网关侧改动全部走「标记 filter + 新插件」，不碰 `build_app` 主循环——避开作用域坑雷区，与 P3 拆分同向不相绞。

## 7. 风险与待实测清单

1. **0.9B 主模型标记遵从率**（B-7 教训）：标记必须极简；非法标签绝不显示给用户；姿态名非法时 organ_body 静默回退 idle。
2. **Laya CPU 延迟**：33ms 是 T4 数字；本机 llama-server 同跑时的争抢必须实测，超 ~200ms 就不能进对话同步路径，只能周期性自主调用。
3. **Laya 中文姿态语义的真实准确率**：多语言版零样本到底几斤几两，用 spike 脚本钉。
4. **透明分层窗口 + Canvas/WebGL 合成性能**（Windows 透明窗有前科，见 clawd AGENTS.md 的 Do-Not-Revisit）：S1 先实测帧率再铺开。
5. **体积**：多语言权重 ~647MB + torch 生态（独立 sidecar 承接，不污 gateway；后续可评估 `laya[onnx]` 路径瘦身）。

## 8. 阶段计划

| 阶段 | 内容 | 验收 |
|---|---|---|
| S0 | 本文档定稿 | 双人 review |
| S0.5 | **Laya spike**（`.tmp-verify/` 内，不碰仓库）：装包、下多语言检查点、中文姿态选择 20 行脚本、CPU 延迟与零样本准确率 | 4 个数字钉死 |
| S1 | Electron 骨骼渲染最小原型（zero 主题 4–6 骨骼 + 弹簧物理，不接 LLM） | 视觉验收：惯性感/squash&stretch |
| S2 | organ_body 插件 + Laya sidecar 管理器 + 姿态标记通道 | 端到端 + 三级降级路径逐一演练 |
| S3 | 对话侧语言能力（prompt 段落并进 P3a 结构）+ few-shot | 0.9B 遵从率实测 |
| S4 | 情绪→体态纯代码通路（organ_embody，不经 Laya，保底活体感：模型不说话身体也在表达） | 无语境身体表达演示 |

## 9. 可逆性审查（固定栏目）

| 失败点 | 回退 |
|---|---|
| Laya sidecar 崩溃/未装 | organ_body 回退静态规则姿态表 |
| 微调后决策质量仍差 | 同上，配置文件一键切回 |
| 骨骼渲染异常 | theme.json `fallbackState` → GIF 状态机 |
| organ_body 被 unload | 标记无人消费，对话零影响 |
| 任何一步 | 全部是新增文件/旁挂模块，无侵入性修改可整笔 revert |
