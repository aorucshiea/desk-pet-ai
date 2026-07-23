<p align="center">
  <img src="assets/readme%20logo.png" alt="MiniCPM Desk Pet" width="760">
</p>

<p align="center">
  <a href="./LICENSE"><img src="https://img.shields.io/badge/License-AGPL--3.0--only-blue.svg" alt="License"></a>
  <a href="https://huggingface.co/openbmb/MiniCPM5-1B-GGUF"><img src="https://img.shields.io/badge/Model-MiniCPM5--1B-green" alt="MiniCPM5-1B"></a>
  <img src="https://img.shields.io/badge/Platform-macOS%20%7C%20Windows-lightgrey" alt="Platform">
</p>

<p align="center">
  <a href="README.md">English</a> | <strong>简体中文</strong>
</p>

<p align="center">
  一个本地优先的 MiniCPM 桌宠。下载安装，跟随首次启动引导完成设置，就可以和桌面上的小伙伴聊天。
</p>

---

## 项目亮点

- **本地运行**：模型下载完成后，日常聊天在你的电脑上完成。
- **无需手动配置**：首次启动会引导完成环境检查、模型下载和模型预热。
- **桌面聊天伙伴**：通过悬浮气泡和 MiniCPM 对话，桌宠可以一直陪在桌面上。
- **感知工作状态**：可根据 Cursor、Claude Code、Codex 等工具的活动展示不同状态。
- **智能下载模型**：支持 Hugging Face 和 ModelScope，会根据网络情况选择更合适的下载源。
- **人格支持**：可在 **Settings -> MiniCPM** 中切换或导入角色适配器。

## 快速开始

### 系统要求

| 项目 | 推荐配置 |
| --- | --- |
| macOS | 14.0+，Apple Silicon (M1/M2/M3/M4)，约 2 GB 磁盘空间 |
| Windows | x64，需 Vulkan 支持，约 2 GB 磁盘空间 |
| 网络 | 首次启动需要联网下载模型；已有本地模型文件时可手动选择 |

> macOS Apple Silicon 是当前主要验证平台；Windows 已提供安装包，欢迎反馈问题。

### 安装

**macOS**

1. 前往 [Releases](https://github.com/OpenBMB/MiniCPM-Desk-Pet/releases) 下载最新 `MiniCPM Desk Pet-*-arm64.dmg`。
2. 打开 DMG，将 **MiniCPM Desk Pet** 拖入 `Applications`。
3. 启动应用，按引导完成设置。

如果 macOS 阻止首次打开，可以右键应用选择 **打开**。必要时也可以移除隔离标记：

```bash
xattr -cr /Applications/MiniCPM\ Desk\ Pet.app
```

**Windows**

1. 前往 [Releases](https://github.com/OpenBMB/MiniCPM-Desk-Pet/releases) 下载最新 `.exe` 安装程序。
2. 运行安装向导，按提示完成安装。
3. 启动应用，按引导完成设置。

### 首次启动

MiniCPM Desk Pet 内置完整首次启动流程：

**环境检查** -> **模型下载** -> **模型预热** -> **开始使用**

默认模型是 [MiniCPM5-1B-GGUF](https://huggingface.co/openbmb/MiniCPM5-1B-GGUF)。你可以让应用自动下载，也可以选择已有的本地 `.gguf` 文件。

## 功能介绍

### 和本地桌宠聊天

打开悬浮聊天气泡，就可以直接和 MiniCPM 对话。设置完成后，日常聊天不需要依赖远程推理服务。

常用快捷键（macOS 为 `Cmd`，Windows 为 `Ctrl`）：

- `Cmd/Ctrl+Shift+M`：打开或关闭 MiniCPM 聊天气泡
- `Cmd/Ctrl+Shift+T`：显示或隐藏思考模式
- `Esc`：输入框聚焦时关闭气泡

### 工作时的状态反应

桌宠可以停留在你的工作区旁边，根据 coding agent 的状态表现出思考、工作、完成、等待关注、休息等不同反应。

### 模型管理

MiniCPM 设置页支持：

- 下载默认模型或选择本地模型文件
- 重新运行首次启动引导
- 管理角色 / 人格适配器
- 在需要时重启本地模型运行环境

### 人格适配器

应用内置一个 neko 风格人格适配器。你可以在 **Settings -> MiniCPM** 中切换适配器，也可以导入自己的适配器。

## 路线图

- 扩展 Linux 验证。
- 增加更多人格预设。
- 优化模型下载提示、重试和诊断体验。
- 缩短首次启动耗时，减小应用体积。
- 为长时间 coding 会话提供更丰富的桌宠旁白。

## 已知限制

- 当前主要验证和发布目标是 macOS Apple Silicon；Windows 已提供安装包，如遇问题欢迎反馈。
- 首次启动需要联网下载模型，除非你手动提供本地模型文件。
- 回复速度会受到芯片、内存压力和模型选择影响。
- Coding agent 状态反应依赖各工具自身的集成方式，不同版本之间可能存在差异。

## 开发者说明

开发环境、打包流程和仓库结构见 [`docs/development.md`](docs/development.md)。

---

## 技术架构与调试笔记

### 系统架构

```
┌──────────────────────────────────────────────────┐
│  Gateway (端口 18765)                             │
│  D:\MiniCPM-Desk-Pet-0.10.0\minicpm-sidecar\    │
│  ├── /api/chat                ← 对话            │
│  ├── /api/screen/observe      ← 屏幕识别         │
│  ├── /api/screen/click        ← 屏幕点击（待加）  │
│  ├── /api/screen/consent-status ← 权限控制      │
│  ├── /api/state               ← 宠物状态手动更新 │
│  ├── /api/mcp/*               ← MCP 工具管理    │
│  └── ClawdBridge → Electron 宠物动画            │
└────────────┬─────────────────────────┬───────────┘
             │ HTTP                    │ HTTP
             ▼                         ▼
┌────────────────────┐   ┌─────────────────────────┐
│  Web 面板           │   │  Electron 桌宠 App      │
│  port 18999        │   │  ports 23333-23337      │
│  D:\...\web-panel\ │   │  D:\...\clawd-on-desk\ │
│  浏览器中打开       │   │  桌面上的浮动宠物窗口    │
└────────────────────┘   └─────────────────────────┘
```

### 核心组件

| 组件 | 路径 | 职责 |
|------|------|------|
| **Gateway** | `minicpm-sidecar/gateway/server.py` | 后端入口，处理所有 API 请求（聊天、屏幕识别、MCP） |
| **Web 面板** | `web-panel/index.html` | 单页控制面板，提供对话、屏幕点击设置、状态查看 |
| **Web 面板服务器** | `web-panel/serve.py` | 静态文件 HTTP 服务器，端口 18999 |
| **Electron 桌宠** | `clawd-on-desk/` | 桌面宠物窗口，负责动画渲染 + 状态显示 |
| **OmniParser** | `D:\omniparser-lite\server.py` | 屏幕解析服务，Florence2 + YOLO + EasyOCR，端口 8000 |
| **OmniParser Manager** | `gateway/omniparser_manager.py` | 管理 OmniParser 子进程生命周期 |
| **ClawdBridge** | `gateway/clawd_state.py` | 向 Electron 推送宠物状态（思考/工作/空闲等） |
| **屏幕截图** | `gateway/screen_capture.py` | 使用 `mss` 库截取主显示器画面 |
| **点击工具(独立脚本)** | `D:\omniparser-lite\test_click.py` | 使用 `ctypes` 调用 Windows API 进行鼠标点击 |

---

### 屏幕识别完整流程

```
用户说"看看屏幕" / 点"测试屏幕识别"
         │
         ▼
 ① 检查 Consent（必须在 "always" 或 "once" 状态）
         │
         ▼
 ② Gateway 调用 screen_capture() 截取屏幕 → base64 PNG
         │
         ▼
 ③ Gateway 调用 ensure_alive() 探测 OmniParser (port 8000)
    → 如果端口已被外部进程占用则直接接管（不启动新子进程）
    → 否则启动新子进程并等待就绪
         │
         ▼
 ④ Gateway POST → OmniParser /parse/
    { base64_image: "..." }
         │
         ▼
 ⑤ OmniParser 内部流程:
    a. YOLO 图标检测 (weights/icon_detect/model.pt)
    b. EasyOCR 文字识别 + 定位
    c. Florence2 图标语义描述 (weights/icon_caption_florence)
         │
         ▼
 ⑥ OmniParser 返回 parsed_content_list:
    [
      {type:"text", bbox:[x1,y1,x2,y2], content:"文字"},
      {type:"icon", bbox:[x1,y1,x2,y2], content:"图标描述", interactivity:true}
    ]
    bbox 为归一化坐标（0.0 ~ 1.0），需乘以屏幕分辨率得像素坐标
         │
         ▼
 ⑦ Gateway 将结果注入系统提示词作为"【用户当前屏幕内容】"
    → 多模态 LLM 看到：原始截图 + OmniParser 结构化解构
```

---

### 调试记录：已发现并修复的 Bug

以下是 2026-07-08/09 调试过程中发现和解决的问题：

#### Bug 1: `ocr_bbox = None` 导致 TypeError
- **文件**: `D:\omniparser-lite\util\utils.py:298`
- **症状**: OmniParser `/parse/` 返回 500 Internal Server Error
- **原因**: `check_ocr_box()` 未检测到文字时返回空列表，`get_som_labeled_img` 中 `if ocr_bbox:` 将空列表视为 falsy，设置 `ocr_bbox = None`，随后 `zip(None, ocr_text)` 抛出 `TypeError: 'NoneType' object is not iterable`
- **修复**: 将 `ocr_bbox = None` 改为 `ocr_bbox = []`，并添加 `if ocr_text is None: ocr_text = []`

#### Bug 2: `ensure_alive()` 不探测端口，每次都启动新子进程
- **文件**: `minicpm-sidecar/gateway/omniparser_manager.py:189`
- **症状**: 每次 `/api/screen/observe` 调用都启动一个新的 OmniParser 进程，3 次后达到重启上限，返回 `"crash"`
- **原因**: `ensure_alive()` 直接检查 `self._proc` 状态，不探测 `127.0.0.1:8000` 是否已经在服务
- **修复**: 在尝试启动子进程前，先调用 `wait_ready()` 探测端口，如果已有进程在服务则直接接管（设置 `self._started = True` 返回 `"ready"`）

#### Bug 3: `status == "alive"` 字符串不匹配
- **文件**: `minicpm-sidecar/gateway/server.py:929`
- **症状**: `ensure_alive()` 返回 `"ready"` 但 gateway 检查 `status == "alive"`，永远不匹配，始终走 `else` 分支输出 `[OmniParser not available]`
- **原因**: 两处用了不同的状态字符串常量
- **修复**: `if status == "alive":` → `if status in ("ready", "alive"):`

#### Bug 4: 网关发送 URL 尾部斜杠不匹配
- **文件**: `minicpm-sidecar/gateway/server.py:986`
- **症状**: 网关返回 `[OmniParser error]`，而非解析结果
- **原因**: OmniParser 路由为 `@app.post("/parse/")`（有尾部斜杠），但网关 POST 到 `http://127.0.0.1:8000/parse`（无尾部斜杠）。FastAPI/Starlette 返回 307 重定向，`httpx.AsyncClient` 默认 `follow_redirects=False`，收到 307 而非 200
- **修复**: `"http://127.0.0.1:8000/parse"` → `"http://127.0.0.1:8000/parse/"`

#### Bug 5: HTTP 超时过短
- **文件**: `minicpm-sidecar/gateway/server.py:984`
- **症状**: `[OmniParser call failed: ]`（空错误消息）
- **原因**: OmniParser 在 CPU 上运行 EasyOCR + Florence2，处理一张全屏截图需 2-3 分钟，但 gateway 超时设为 60 秒
- **修复**: `httpx.AsyncClient(timeout=60.0)` → `timeout=300.0`

---

### 2026-07-09 新增功能

#### 1. 屏幕点击系统
- 新增 `screen_click.py` 模块：使用 `ctypes` 调用 Windows API（`SetCursorPos` + `mouse_event`）执行屏幕点击
- 新增 `POST /api/screen/click` REST 端点：接收 OmniParser element 对象，返回 `{ok, clicked, summary}`
- 新增 MCP 内置工具机制（`mcp_manager.py`）：`register_builtin()` 允许注册进程内执行的工具
- 注册 `builtin/screen_click` MCP 工具：LLM 可通过原生 function calling 或 `[MCP:builtin/screen_click:{...}]` 调用

#### 2. 多模态 LLM 截图支持
- 修改 `minicpm-chat-renderer.js`：新增 `_providerSupportsVision()` 检测当前 provider 是否支持视觉
- 当检测到多模态 provider 时，将原始截图作为 `image_url` content block 注入用户消息
- 支持 OpenAI（GPT-4o）、Anthropic（Claude 3+）、DeepSeek-V4-Flash、GLM-4V、Qwen-VL 等视觉模型

---

### API 端点汇总

| 端点 | 方法 | 描述 |
|------|------|------|
| `/api/chat` | POST | 发送聊天消息，支持 SSE 流式和阻塞两种模式 |
| `/api/load-model` | POST | 动态加载/切换 GGUF 模型 |
| `/api/state` | POST | 手动设置宠物状态（用于 Web 面板） |
| `/api/screen/consent-status` | GET/POST | 获取/设置屏幕访问权限（`deny` / `once` / `always`） |
| `/api/screen/observe` | POST | 截屏 + OmniParser 解析，返回结构化屏幕内容 |
| `/api/screen/click` | POST | 接收 element（含 bbox），调用 Windows API 点击屏幕元素 |
| `/api/mcp/list` | GET | 列出已注册的 MCP 工具 |
| `/api/mcp/execute` | POST | 执行 MCP 工具调用 |
| `/api/mcp/config` | POST | 配置 MCP 服务器连接 |

---

### 屏幕点击（已实现）

点击功能已集成到 Gateway 中，同时提供 REST 端点和 MCP 工具两种调用方式。

**完整流程（纯文本 LLM）**：
```
 ① OmniParser 截图 → 解析 → parsed_content_list（含 bbox 标签）
 ② 标签文字注入 system prompt: 【用户当前屏幕内容】
 ③ LLM 看到文字标签 → 决定点哪个 → 输出 click 指令
 ④ Gateway 执行 builtin/screen_click → Windows API 点击
```

**完整流程（多模态 LLM）**：
```
 ① OmniParser 截图 → 解析 → parsed_content_list
 ② 同时截原始图（raw_screenshot_base64）
 ③ 原始图作为 image_url 注入用户消息 + 标签文字注入 system prompt
 ④ LLM 看到图+标签 → 决定点哪个 → 输出 click 指令
 ⑤ Gateway 执行 builtin/screen_click → Windows API 点击
```

**LLM 调用点击工具的方式**：

| LLM 类型 | 调用方式 |
|----------|--------|
| OpenAI / Anthropic（原生 function calling） | `call tool: mcp_builtin_screen_click({ element: {...} })` |
| 本地模型（文本输出） | `[MCP:builtin/screen_click:{"element":{"bbox":[...]}}]` |

**返回格式**：
```json
{
  "ok": true,
  "clicked": { "x": 960, "y": 540, "element_type": "text" },
  "summary": "Clicked '搜索框' (text) at (960, 540)"
}
```

**关键文件**：
- 点击逻辑实现：`minicpm-sidecar/gateway/screen_click.py`（`click_element()`）
- REST 端点：`minicpm-sidecar/gateway/server.py`（`POST /api/screen/click`）
- MCP 工具注册：`minicpm-sidecar/gateway/server.py:462-494`（`register_builtin()`）
- MCP 内置工具机制：`minicpm-sidecar/gateway/mcp/mcp_manager.py`（`register_builtin()` / `list_all_tools()` / `call_tool()`）
- 原始截图注入（多模态）：`clawd-on-desk/src/minicpm-chat-renderer.js:238-255`（`_providerSupportsVision()`）
- 独立参考脚本：`D:\omniparser-lite\test_click.py`
- 待创建：`minicpm-sidecar/gateway/screen_click.py`（点击逻辑封装）
- 待修改：`minicpm-sidecar/gateway/server.py`（添加 `/api/screen/click` 路由）

**注意**：点击不需要 Electron 桌宠运行，Gateway 直接调用 Windows 系统 API。

---

### 启动指南

#### 开发环境

```powershell
# 启动 OmniParser（必须先启动）
cd D:\omniparser-lite
.venv\Scripts\python.exe server.py

# 启动 Gateway
cd D:\MiniCPM-Desk-Pet-0.10.0\minicpm-sidecar
.venv\Scripts\python.exe -m gateway --host 127.0.0.1 --port 18765 --model <model_path>

# 启动 Web 面板
cd D:\MiniCPM-Desk-Pet-0.10.0\web-panel
python serve.py

# 浏览器打开 http://127.0.0.1:18999
```

#### 屏幕识别测试

```powershell
# 设 consent
Invoke-RestMethod -Uri "http://127.0.0.1:18765/api/screen/consent-status" `
  -Method POST -Body '{"consent":"always"}' -ContentType "application/json"

# 观察屏幕
Invoke-RestMethod -Uri "http://127.0.0.1:18765/api/screen/observe" `
  -Method POST -Body '{}' -ContentType "application/json" -TimeoutSec 300
```

#### OmniParser 健康检查

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/probe/" -Method GET
# 返回: {"message":"Omniparser API ready"}
```

---

### 常见问题

**Q: Web 面板显示 `[OmniParser not available]`**
A: 见 Bug 2 + Bug 3。确保 OmniParser 在端口 8000 运行，且 Gateway 代码已包含 `status in ("ready", "alive")` 修复。

**Q: Web 面板显示 `[OmniParser error]`**
A: 见 Bug 4。Gateway POST 到 `/parse` 而非 `/parse/`，尾部斜杠不匹配导致 307 重定向。

**Q: Web 面板显示 `[OmniParser call failed: ]`（空错误）**
A: 见 Bug 5。CPU 上 OmniParser 耗时通常 2-3 分钟，Gateway 超时需设为 300 秒。

**Q: Electron 桌宠没有反应**
A: Electron App（端口 23333-23337）需要额外启动。Gateway 的 ClawdBridge 会通过 HTTP POST 向其推送状态。

**Q: OmniParser 启动后 `/probe/` 正常但 `/parse/` 返回 500**
A: 见 Bug 1。`util/utils.py` 中 `ocr_bbox = None` 导致 `zip(None, ocr_text)` 崩溃。确认修复已应用（`ocr_bbox = []`）。

**Q: 没有互联网，模型无法下载**
A: OmniParser 的 EasyOCR、Florence2、PaddleOCR 模型需要提前下载好缓存。设置环境变量 `TRANSFORMERS_OFFLINE=1` 和 `HF_HUB_OFFLINE=1` 可防止启动时尝试联网。

---

### 日志位置

| 日志 | 路径 |
|------|------|
| Gateway 日志 | `D:\MiniCPM-Desk-Pet-0.10.0\gateway.err.log` |
| OmniParser 输出（通过 Manager 启动时） | `D:\MiniCPM-Desk-Pet-0.10.0\logs\omniparser-*.log` |
| OmniParser 本地日志 | `D:\omniparser-lite\*.log` |

## 致谢

- 桌宠 UI 基于 [rullerzhou-afk/clawd-on-desk](https://github.com/rullerzhou-afk/clawd-on-desk)。完整归属信息见 [`NOTICE.md`](./NOTICE.md)。
- 模型权重来自 OpenBMB MiniCPM 模型家族，并在使用时单独下载。
- 内置 neko 人格使用 **neko30k** 数据集（[liumindmind/NekoQA-30K](https://huggingface.co/datasets/liumindmind/NekoQA-30K)）作为微调数据。

## 许可证

本仓库使用 [GNU AGPL-3.0-only](./LICENSE) 分发。

MiniCPM 模型权重会单独下载，受 [OpenBMB MiniCPM Model License](https://github.com/OpenBMB/MiniCPM/blob/main/MiniCPM%20Model%20License.md) 约束。美术素材、第三方代码和数据集保留各自声明；详见 [`NOTICE.md`](./NOTICE.md) 和 [`clawd-on-desk/NOTICE.md`](clawd-on-desk/NOTICE.md)。
