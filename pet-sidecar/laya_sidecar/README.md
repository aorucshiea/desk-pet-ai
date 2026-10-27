# Laya 决策 sidecar（可选下载）

给桌面宠物提供本地 **System 1 思考预算路由**：输入一条用户消息（`state`），让 Laya 在 `fast / normal / deep / max` 中选择一档。服务是标准 Jev 风格 HTTP API：

```
POST http://127.0.0.1:8765/v1/systemone
```

这是 **可选下载 sidecar** 的基础，不内置进 Electron 安装包。gateway 侧的路由器
（`gateway/decision/laya_router.py`，`PET_LAYA_URL` / `PET_LAYA_TIMEOUT` /
`PET_LAYA_ENABLED`）已经接入；尚未接入的是 Electron 设置页的下载/启停 UI。

## 目录

| 文件 | 作用 |
|---|---|
| `bootstrap.ps1` / `bootstrap.sh` | 首次安装：选 Python 3.10–3.12、建 `.venv`、装 `laya[onnx,serve]==0.4.15`、下载/复用模型、可选导出 ONNX |
| `start.ps1` / `start.sh` | 启动 `laya-serve`（127.0.0.1:8765），等待 `/health`，再发一次 `/v1/systemone` 预热；支持停止/状态 |
| `provision.py` | 复用已有模型、HF/ModelScope 下载、模型目录校验（不导入 torch） |
| `export_onnx.py` | 可选 ONNX 导出（需要 torch；`laya-serve` 不使用） |
| `.venv/` | 由 bootstrap 创建；本 smoke 没有在 sidecar 里重装，使用 `.tmp-laya/venv` |
| `models/` | 可选模型下载目录；本 smoke 复用 `.tmp-laya/models`，没有复制 |
| `run/` | PID、端口、日志、warmup 响应；可随时删除 |

## 体积

| 产物 | 实测体积 | 说明 |
|---|---:|---|
| `laya-multilingual` 模型目录 | **678 MB**（647 MiB） | `model.safetensors` 644 MB + tokenizer 34 MB + 配置 |
| sidecar `.venv`（Windows，`laya[onnx,serve]==0.4.15`） | **956 MB**（911 MiB） | 其中 torch 约 539 MiB；transformers 118 MiB；onnxruntime 48 MiB；ONNX 导出用的 onnx/onnxscript 约 34 MiB |
| fp32 ONNX | **1.29 GB**（1.20 GiB） | spike 已导出；sidecar 默认不复用/不复制，只引用或可选重导 |
| torch 运行态合计 | **约 1.63 GB** | venv + 模型，不含 ONNX |
| 加上 ONNX 后 | **约 2.92 GB** | venv + 模型 + ONNX |

用户侧选择“可选下载”是合理的：只装模型和 venv 已经约 1.6 GB；ONNX 还会再加 1.29 GB。上表的 venv 依赖系统 Python 3.12；如果要完全自包含分发，再加约 30–100 MB 的 Python 运行时，torch 方案实际约 **1.7 GB**，带 ONNX 约 **3.0 GB**。

## 先决定：torch 是不是硬依赖？

**是。laya 0.4.15 上游没有 torch-free 的 ONNX 运行路径。** 具体证据：

- `laya-0.4.15.dist-info/METADATA` 有 `Requires-Dist: torch>=2.0.0`，没有 extra 能去掉它。
- `laya[onnx]` 只增加 `onnx`、`onnxruntime`、`onnxscript`，**不会移除 torch**，也不包含 HTTP server。
- `laya[serve]` 只增加 `fastapi`、`uvicorn`、`python-multipart`。HTTP 服务本身需要 `laya[serve]`；本 sidecar 为了可选 ONNX 换成 `laya[onnx,serve]`，bootstrap 固定为 0.4.15。
- 顶层 `import laya` 本身是 lazy 的，在屏蔽 torch 时可以成功；但 `laya.onnx_agent` 在模块顶层 `from laya.common import (...)`，而 `laya/common.py:14` 是 `import torch`。实测（用 meta path 阻止 `torch` 导入）：

  ```
  FAIL import laya.onnx_agent: ImportError
    laya/onnx_agent.py:16  from laya.common import (...)
    laya/common.py:14      import torch
  ```

- 即使绕过这一层，`ONNXAgent.__init__` 还会 `from .agent import _fix_tokenizer_config`，而 `laya/agent.py` 顶层也 `import torch`。
- `laya-serve` 内部是 `Router` / `Agent` 的 torch 推理路径，没有 ONNX backend。`onnxruntime` 本身能在无 torch 环境 import（已验证），但 laya 的输入构造和封装在上游代码里绑定了 torch。

**结论：** 按 0.4.15 原样，不能只装 ONNX + tokenizer + onnxruntime 运行。要 torch-free 只能 fork/vendor 一份 ONNX-only runner（复制 `build_sequence` / `_collate_items_numpy` 等纯 NumPy 路径，绕开 `laya.common` 和 `laya.agent`），不属于本次范围，也不建议在没有测试的情况下上线。

### 如果硬要算 torch-free 的最小体积

理论上，直接跑同一张 ONNX 图需要：

| 组件 | 体积 |
|---|---:|
| ONNX fp32 | 1.288 GB |
| tokenizer | 34 MB |
| onnxruntime（CPU） | 48 MiB（约 50 MB） |
| Python 3.12 运行时 | 约 30 MB |
| **合计** | **约 1.40 GB** |

如果还用 `transformers.AutoTokenizer`，再加 transformers 约 124 MB、tokenizers/numpy/huggingface_hub 约 50 MB，约 **1.58 GB**。这仍然不含 torch，但需要自定义 runner；**上游 laya 0.4.15 做不到**。现实可分发体积是上面的 1.63 GB（torch + 模型），带 ONNX 则 2.92 GB。

## 安装

### Windows（主路径）

```powershell
cd E:\deskpetai\pet-sidecar\laya_sidecar
powershell -NoProfile -ExecutionPolicy Bypass -File .\bootstrap.ps1
```

常用参数：

```powershell
# 只看状态，不安装/不下载
powershell -NoProfile -ExecutionPolicy Bypass -File .\bootstrap.ps1 -Check

# 国内网络用 ModelScope（或 auto：先 HF，失败自动回退 ModelScope）
powershell -NoProfile -ExecutionPolicy Bypass -File .\bootstrap.ps1 -Source modelscope

# 可选导出 ONNX（一次性，~1.3 GB；已有 .tmp-laya ONNX 会跳过且不复制）
powershell -NoProfile -ExecutionPolicy Bypass -File .\bootstrap.ps1 -ExportOnnx

# 指定 Python 3.10–3.12 和 pip 源
powershell -NoProfile -ExecutionPolicy Bypass -File .\bootstrap.ps1 `
  -Python C:\Python312\python.exe `
  -PipIndexUrl https://pypi.tuna.tsinghua.edu.cn/simple

# 使用官方 PyPI（默认是清华镜像）
powershell -NoProfile -ExecutionPolicy Bypass -File .\bootstrap.ps1 -OfficialPyPI
```

- 只接受 **64 位 Python 3.10 / 3.11 / 3.12**。3.13+ 没有验证；脚本会拒绝。
- 如果机器只有 uv 管理的 Python，脚本会尝试 `uv python find 3.12/3.11/3.10`。
- 默认 pip 源是清华镜像；`-PipIndexUrl`、`-OfficialPyPI` 或环境变量 `LAYA_PIP_INDEX_URL` 可覆盖。
- 安装失败会抛错并以非 0 退出；不会打印“成功”。模型下载中断会保留 `.part`，下次继续。
- HF 被墙时：`$env:HF_ENDPOINT="https://hf-mirror.com"`，或 `-Source modelscope`。

### macOS / Linux

```bash
cd pet-sidecar/laya_sidecar
./bootstrap.sh
./bootstrap.sh --check
./bootstrap.sh --source modelscope
./bootstrap.sh --export-onnx
```

`bootstrap.sh` 会创建 `.venv/bin/python`；Windows 上是 `.venv/Scripts/python.exe`。脚本按 `python3.12` → `3.11` → `3.10` → `uv python find` 的顺序找解释器。

### 模型路径与“不复制”

解析顺序（bootstrap 和 start 一致）：

1. `-ModelPath` / `--model` / `LAYA_MODEL_PATH`
2. `pet-sidecar/laya_sidecar/models/laya-multilingual`
3. `<repo>/.tmp-laya/models/laya-multilingual`（本机 spike 缓存，仅开发复用）

如果第 3 项存在，bootstrap/start 直接引用，**不会把 644 MB（614 MiB）safetensors 或 1.29 GB ONNX 复制进 sidecar**。第 2 项不存在时才下载；下载源：

- HuggingFace：`convaiinnovations/laya-multilingual`
- ModelScope：`convaiinnovations/laya-multilingual`

`provision.py` 下载的文件是 `config.json`、`rl_agent_config.json`、`model.safetensors`、`encoder/config.json`、`tokenizer/tokenizer.json`、`tokenizer/tokenizer_config.json`。下载后会校验：权重 ≥ 600 MB、tokenizer ≥ 30 MB、配置能解析。它不导入 torch。

ONNX 解析：先看 `laya_sidecar/models/laya-multilingual.onnx`，再看 `.tmp-laya/laya-multilingual.onnx`。存在就跳过，**不复制**。`-ExportOnnx` 只在两边都不存在时才在 sidecar 里生成新文件。

## 启动 / 停止 / 健康检查

### Windows（给 gateway 用的准确命令）

```powershell
# 启动；阻塞到 /health 和 /v1/systemone 预热都成功才 exit 0
powershell -NoProfile -ExecutionPolicy Bypass -File "E:\deskpetai\pet-sidecar\laya_sidecar\start.ps1" -Port 8765

# 状态：0=健康，1=未运行，2=进程在但健康失败
powershell -NoProfile -ExecutionPolicy Bypass -File "E:\deskpetai\pet-sidecar\laya_sidecar\start.ps1" -Status

# 停止；会杀进程树，并删除 PID/端口文件
powershell -NoProfile -ExecutionPolicy Bypass -File "E:\deskpetai\pet-sidecar\laya_sidecar\start.ps1" -Stop

# 健康检查
curl.exe -sS http://127.0.0.1:8765/health
```

要点：

- 默认 `127.0.0.1:8765`，只监听回环；不碰 `18765` / `23333`。
- `start.ps1` 在内部设置 `LAYA_EXTRA_MODELS={"zh-tier":"<model path>"}`、`LAYA_MODELS=zh-tier`、`LAYA_DEFAULT_MODEL=zh-tier`、`LAYA_PRELOAD=1`、`LAYA_DEVICE=cpu`、`LAYA_THREADS=4`（可用环境变量覆盖）。
- 启动脚本会等待 `/health`，然后发一条英文 `ping` 到 `/v1/systemone`；只有 HTTP 200 且 `answers.thinking.choice` 存在才 exit 0。失败会杀掉进程树、删除 PID/端口文件、打印日志尾部并 exit 1。
- PID 文件 `run/laya-serve.pid` 记录的是 **uv venv 的 `python.exe` launcher**；真正监听端口的是它的子进程（实测 launcher PID 53804 / listener 83256）。停止必须走 `-Stop`，不要直接 `Stop-Process` PID，否则可能留下子进程。脚本用 `taskkill /T /F` 杀整棵树。
- 日志：`run/laya-serve.out.log`、`run/laya-serve.err.log`；上次运行的日志保留为 `.prev`。`run/laya-serve.port` 记录实际端口，`-Stop`/`-Status` 不带 `-Port` 时会读它。
- 冷启动（模型从磁盘读）明显更慢；本机热缓存实测启动 + 预热约 **12.1 s**。gateway 的超时至少给 240 s。

### macOS / Linux

```bash
./start.sh                 # 启动 + 健康 + 预热
./start.sh --status        # 0=健康，1=未运行，2=进程在但健康失败
./start.sh --stop
./start.sh --foreground    # 调试：前台运行，不写 PID
./start.sh --port 8765 --python /path/to/.venv/bin/python --model /path/to/model
```

Git Bash 下 `start.sh` 的进程管理不如 `start.ps1` 可靠；Windows 请用 PowerShell。

## gateway 路由器调用

建议流程：

1. `GET /health` 或 `start.ps1 -Status`；健康就直接用。
2. 不健康且未运行 → `start.ps1`（异步/带 240 s 超时）。
3. 返回 exit 1 或 `/health` 失败 → 不要路由，回退规则/默认档；不要伪造 Laya 成功。
4. 正常时 POST：

```bash
curl -sS -X POST http://127.0.0.1:8765/v1/systemone \
  -H "Content-Type: application/json; charset=utf-8" \
  --data-binary @request.json
```

`request.json`：

```json
{
  "model": "zh-tier",
  "state": "这段 Python 报错 KeyError 是什么原因，怎么改",
  "questions": {
    "thinking": {
      "type": "choice",
      "instructions": "这条用户消息需要多少思考预算？按回答它所需的工作量选择一档。",
      "criteria": {
        "fast": "打招呼、寒暄、确认、极短的一句话，不需要推理",
        "normal": "普通聊天、简单事实问题、日常建议，一两句话就能回答",
        "deep": "写代码、排查报错、分析推理、多步任务，需要认真思考",
        "max": "明确要求深入或长篇，或系统设计、复杂架构，需要最大思考预算"
      }
    }
  }
}
```

响应（节选，中文 smoke 实测）：

```json
{
  "answers": {
    "thinking": {
      "type": "choice",
      "choice": "deep",
      "probabilities": {"fast": 0.032, "normal": 0.1982, "deep": 0.7618, "max": 0.0081},
      "confidence": 0.5116,
      "answer_confidence": 0.7618
    }
  },
  "usage": {"input_tokens": 116, "output_tokens": 0},
  "routing": {"model": "zh-tier", "repo": "E:\\deskpetai\\.tmp-laya\\models\\laya-multilingual"}
}
```

- 决策看 `answers.thinking.choice`；概率看 `probabilities`；阈值建议用 `answer_confidence`（这是被选选项的概率），不要拿 `confidence` 当同一含义（`confidence` 是归一化熵）。
- 响应头 `Server-Timing: inference;dur=...` 和 `X-Inference-Time-Ms` 可用于埋点。
- 请求体 `model` 必须是 `zh-tier`（`start` 注册的名字），不是 `multilingual`；否则 Router 会走默认仓库解析。
- 出错：400=请求体/选项非法；422=参数/模型问题；503=并发满；500=推理失败。gateway 应回退，不要重试风暴。
- gateway 当前在每轮真实用户消息上同步等 Laya，最多 400 ms；本机实测 **218–359 ms**。不需要时设
  `PET_LAYA_ENABLED=0` 直接走规则。

## CPU 延迟与 smoke 结果

环境：Windows，CPU-only，`laya` 0.4.15，torch 2.14.1+cpu，Python 3.12.12；sidecar 复用 `.tmp-laya/venv` 和 `.tmp-laya/models`，没有复制模型/ONNX。`LAYA_THREADS=4`，端口 `8765`（测试时 18765/23333 均未使用）。

5 次连续 curl（页面缓存已热）：

| prompt | HTTP | choice | server p50 | server 范围 | wall p50 | `answer_confidence` |
|---|---:|---|---:|---:|---:|---:|
| 早上好呀 | 200 ×5 | **fast** ×5 | **128.4 ms** | 120.2–171.6 ms | 131 ms | 0.8976 |
| 这段 Python 报错 KeyError 是什么原因，怎么改 | 200 ×5 | **deep** ×5 | **123.2 ms** | 120.0–155.5 ms | 126 ms | 0.7618 |

说明：

- 这是 **torch `laya-serve`** 的端到端服务端耗时；模型常驻、4 线程、页面缓存热。
- 第一次从磁盘加载模型后的同机 p50 约 **258–272 ms**（首轮/冷页缓存）；冷启动首轮会更慢。README 的体积/延迟都按热缓存给。
- spike 里 ONNX `ONNXAgent` 直跑 p50 约 **77–116 ms**（1.5–2× 于 torch），但 `laya-serve` 不提供 ONNX 后端，不能直接拿来当服务。
- 8 线程对照：smoke1 p50 277.8 ms、smoke2 242.9 ms，混合结果，没有稳定优势；默认 4。`LAYA_THREADS` 可覆盖。
- 模型常驻内存实测：**1.6 GB working set / 2.2 GB private**（listener 进程）。8 GB 机器同时跑 gateway/Electron 会偏紧。
- 模型质量：spike 12 条四分类 **exact 66.7%**，within-1 91.7%；多轮在 58–67% 区间。清晰短句稳定，但“千万级并发限流系统设计”这类长 prompt 在 spike 里被误判为 `fast`。**不要把 Laya 当唯一路由器**；建议规则/二分类（要不要 deep）优先，Laya 做 tie-break，并保留 fallback。

### 本 smoke 实际步骤

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "E:\deskpetai\pet-sidecar\laya_sidecar\start.ps1" `
  -Port 8765 `
  -Python "E:\deskpetai\.tmp-laya\venv\Scripts\python.exe" `
  -ModelPath "E:\deskpetai\.tmp-laya\models\laya-multilingual" `
  -TimeoutSec 240
```

然后两条中文 prompt 各 curl 5 次；`start.ps1 -Status` 返回 0；`start.ps1 -Stop` 返回 0，PID/端口文件删除，8765 无监听，无残留 `python -m laya.serve`。另测了 8766 自定义端口 + 不带 `-Port` 的 `-Stop`（靠 `run/laya-serve.port`）也通过。

**未做**：sidecar `.venv` 的完整 `bootstrap` 端到端安装（会重装约 1 GB 并下载模型）；`bootstrap -Check`、脚本解析、`provision.py verify`、已有模型跳过、ModelScope 小文件下载、已有 ONNX 跳过已验证。完整安装路径需要在干净机器上再跑一次。

## 隐私

- 推理完全本地；模型从本地目录加载，`start` 设置 `HF_HUB_OFFLINE=1`，正常请求不会访问外网。
- 只有安装/下载阶段访问 HuggingFace/ModelScope/pip。没有遥测。
- 服务只绑定 `127.0.0.1:8765`；脚本移除了 `LAYA_API_KEY`，默认不鉴权，本机任意进程都能调用。不要把端口暴露到局域网。
- `laya-serve` 的 uvicorn access log 只记录路径/状态码，不记录 prompt body；`run/warmup.json` 只保存启动时英文 ping 的响应。gateway 如果记录用户文本，属于 gateway 的日志策略。
- 模型 license 为 Apache-2.0（以 HF/ModelScope 模型卡为准）。

## 已知限制与残余风险

1. **torch-free 不可行**（上游 0.4.15）；ONNX 只是可选导出产物，当前没有 ONNX server。
2. **模型质量有限**：四分类 exact 58–67%，长/设计类 prompt 会误判；必须规则兜底。
3. **内存 2.2 GB private**，低内存机器可能 OOM；`LAYA_MAX_LOADED=1` 已限制只常驻一个 checkpoint。
4. **启动 ~12 s（热缓存）**，冷启动更久；gateway 必须用 health gating，不能在请求路径里同步冷启动。
5. **uv venv 的 launcher/child PID 分离**：PID 文件是 launcher；直接用 `Stop-Process` 可能留下 listener。请用 `-Stop` / `--stop`。
6. **模型下载无 SHA256 校验**：`provision.py` 只校验尺寸/JSON；权重损坏会在加载时失败。需要强校验可后续加 safetensors SHA。
7. **端口冲突**：默认 8765；如果被别的服务占用，`start` 会拒绝启动并报 PID。可用 `-Port`，但 gateway 必须一致。
8. **接入状态**：gateway 路由器已接入（`PET_LAYA_*`，默认启用）；Electron 设置页 / 下载 UI 尚未接入。
   `deskpt-tauri` 不在当前开发范围。
9. **bootstrap 完整安装未在本机端到端跑**；只验证了检查/解析/复用/下载小文件/ONNX 跳过。
10. **ONNX 导出脚本未在本 smoke 重新执行**（spike 已有 1.29 GB 产物，脚本检测到后跳过）；导出逻辑与 `.tmp-laya/export_onnx_spike.py` 等价（5 输入、opset 17、fp32、dynamic axes）。

## 故障排查

| 现象 | 处理 |
|---|---|
| `No 64-bit Python 3.10-3.12 found` | 安装 Python 3.12 64 位，或 `-Python` / `--python` 指定 |
| pip 很慢/超时 | 默认清华镜像；或 `-PipIndexUrl`、`-OfficialPyPI`；重试会复用 pip 缓存 |
| HF 下载失败 | `-Source modelscope`，或 `HF_ENDPOINT=https://hf-mirror.com` |
| 模型目录不完整 | 重跑 bootstrap；ModelScope `.part` 会续传，HF snapshot 也会续传 |
| 启动 exit 1 | 看 `run/laya-serve.err.log` 尾部；start 已自动打印最后 40 行 |
| 端口被占 | `start.ps1 -Stop` 清理自己的；别人的端口用 `-Port` 换一个 |
| `-Status` 返回 2 | 进程在但 `/health` 不通；先 `-Stop`，再 start，并看日志 |
| 停止后仍有 python | 用 `-Stop`（taskkill /T）；不要只杀 launcher PID |
