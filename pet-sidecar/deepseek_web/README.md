# DeepSeek Web Bridge（可选）

用**已经登录的 DeepSeek 网页版**给桌宠当免费 LLM：不填 API key，消耗的是网页版账号自己的额度。

> ⚠️ **先读这一节**
>
> - 这是浏览器自动化，不是官方 API。网页 DOM、登录态、免费额度/限流规则随时会变，**不适合作为唯一或生产 provider**。
> - 用网页账号跑自动化可能违反 DeepSeek 的服务条款，存在账号受限的风险；请只在自己的账号上低频试用。
> - 首次会新开一个网页会话；之后同一会话只追加新消息，不再重发硬 system prompt。每轮仍消耗一条网页消息；桌宠的主动搭话也会消耗。频繁使用请切回 API 或本地模型。
> - 浏览器标签页必须保持打开（可以不在前台，但 Chrome 对后台标签的定时器有节流，建议别最小化整个窗口）。关掉标签页/退出浏览器，桌宠就会超时。

## 它怎么工作

```
Desk Pet 网关
  │ OpenAI /v1/chat/completions（流式）
  ▼
bridge.py 127.0.0.1:8766
  │ 任务队列（长轮询）
  ▼
浏览器扩展（chat.deepseek.com 标签页）
  │ 新建对话（仅首次）→ 填输入 → 发送 → 等回复稳定
  ▼
把回复文本 POST 回 bridge → 网关收到 delta/end
```

网关不需要改代码：它把 `bridge.py` 当成一个 OpenAI 兼容 provider。

## 安装

### 1. 启动 bridge

需要 Python 3.8+：

```bash
cd pet-sidecar/deepseek_web
python bridge.py
```

Windows 也可以双击 `start_bridge.bat`。看到：

```
deepseek-web bridge on http://127.0.0.1:8766
```

保持这个窗口开着。

### 2. 安装浏览器扩展

1. 打开 `chrome://extensions`（Edge 是 `edge://extensions`）
2. 打开 **开发者模式 / Developer mode**
3. **加载解压缩扩展 / Load unpacked**
4. 选择本目录下的 `extension/`（不是 `deepseek_web/` 根目录）
5. 打开/刷新 `https://chat.deepseek.com/`，确认已登录

扩展加载后每秒向 bridge 轮询一次任务；只对 `chat.deepseek.com` 和本地 mock 页生效。**改过 `extension/` 里的文件后，必须在 `chrome://extensions` 点一次扩展卡片上的“重新加载”，再刷新 DeepSeek 标签页**；只刷新页面不会更新 content script（当前版本 `0.4.2`，页面根节点会写 `data-ds-bridge-version`）。如果同时开了多个 DeepSeek 标签页，扩展优先用当前激活的那个；旧的隐藏副本不会再偷偷接任务。扩展被重载后，后台会自动向已打开的标签页补注入 `content.js`。

### 3. 自测

先不接桌宠，直接验证 bridge：

```bash
curl -sS http://127.0.0.1:8766/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-web","messages":[{"role":"user","content":"你好，只回复：桥接成功"}],"stream":false}'
```

也可以打开 `http://127.0.0.1:8766/mock` 用一个本地假页面测试扩展链路（不需要登录 DeepSeek）。

### 4. 接入桌宠

在 `~/.pet/providers.json` 的 `providers` 里加：

```json
"deepseek-web": {
  "apiKey": "local",
  "baseUrl": "http://127.0.0.1:8766/v1",
  "model": "deepseek-web",
  "thinking": false,
  "functionCalling": false,
  "toolsEnabled": true,
  "contextWindow": 65536,
  "images": true
}
```

然后重启网关/桌宠（或在设置里重新加载 provider）。在 **设置 → 网页模型** 里点「设为当前模型」；也可以先在聊天里输入 `/model deepseek-web` 试一轮。

- `functionCalling: false`：网页版不会走原生 function calling；桌宠继续用文本 `[MCP:...]` 契约。
- `toolsEnabled: true`：把桌宠的完整工具表注入 prompt，网页版可以像 API 模型一样控制桌宠（走路/看屏幕/记忆/世界刚体等）；如果只想聊天，改成 `false` 会只保留核心记忆工具。
- `images: true`：扩展会把截图通过 DeepSeek 网页的文件上传入口作为图片附件发送；上传入口失效会报错，可改用 `observe_screen` 或切到 API/视觉 provider。
- `contextWindow` 只是给 UI/路由看的，网页端实际上下文由 DeepSeek 决定。

桌宠的 **设置 → 网页模型** 页面会检查 Bridge / 扩展状态、启动 Bridge、发一条测试消息、一键设为当前模型，并显示今日网页消息数。

默认 provider 是 `deepseek-web` 时，桌宠启动会先确保 Bridge 在跑（用 `PET_PYTHON` 或 sidecar 的 `.venv` Python，detached），然后最多等约 10 秒 `extension_connected`；等不到就跳过这次开机唤醒并记日志，不会弹错误气泡。每日上限默认 **100 条/天**，按每个 `/v1/chat/completions` 请求计（一次工具调用通常 2 条），超限返回 429 并提示切回 API/本地；可直接在 **设置 → 网页模型** 改，保存到 `~/.pet/deepseek-web-config.json`，重启后保留；没有配置文件时用 `PET_WEB_DAILY_LIMIT`（默认 100，`0` 不限）。

### 网页提示词压缩

同一个网页会话里，**只有第一轮发送 system + 可见历史**；后续只追加新的 user/tool 消息，assistant 回复已经在页面里，不会重发。compact prompt 只影响第一轮，因此硬提示词不会每轮重复：

- 首次仍用 compact 的 canonical persona（1.8k，完整版 4.3k）、技能名、工具名和最近 8 条历史；
- 同一会话后续请求只发 `【用户】...` 或 `【工具结果】...`，不携带 system；
- 主动生命轮（每 30 秒一次“生命在继续”）的文本完全相同，不能靠文本重叠判断新旧：只把**最后一条**当新轮，首次建会话时也折叠成一条，避免把 8 条相同指令塞进网页；
- 页面 URL 变化（手动新建/刷新到新会话）、请求失败、30 轮、120k 字符会重置，下一次重新发完整 system；
- 开机唤醒/压缩等 `tools_enabled=false` 的请求是临时轮，不会成为会话基线（否则后续 delta 会缺少工具指令）；
- 设置页「网页会话」显示当前是“新会话”还是“复用中 · N 轮”。

实测同一轮：`system_chars 15922 → 7851`、`~tokens 10309 → 4587`；开机唤醒 `5163 chars / 3281 tokens / tools=0`，不再出现“唤醒时连调两次 memory”的循环。重复生命轮从“每轮重发 3–8k 字硬提示词”变成只发最后一条 `【用户】生命在继续`。

### 看屏幕

DeepSeek 网页支持图片附件，扩展会把截图通过页面隐藏的 `input[type=file]` 上传（`DataTransfer` + `change`），所以 web provider 配 `images:true` 时模型能直接收到原图，而不是只收到描述。

- 先开启 **设置 → 屏幕点击 → 总是允许查看屏幕**。没开时 `capture_screen` 会走逐次授权，拒绝后返回“用户未授权”。
- 开启后 `[MCP:builtin/capture_screen:{}]` 会截图，扩展把图片作为附件上传到新的 DeepSeek 会话；模型直接看图。
- 需要精确坐标时用 `[MCP:builtin/observe_screen:{}]`（OmniParser OCR），或 `screen_click`。
- 如果页面改版导致上传入口找不到，`content.js` 会报错；可改用 `observe_screen` 的 OCR，或切到 API/本地视觉 provider。

`pet_world_scout` / `pet_world_bodies` 是桌面窗口刚体的视锥列表（方向/距离），**不是截图工具**；用户说“看屏幕”时模型应调用 `capture_screen`。

### 专用浏览器（推荐）

普通 Chrome 后台标签会被定时器节流。用一个独立 profile + 关闭节流的参数：

```bat
"C:\Program Files\Google\Chrome\Application\chrome.exe" ^
  --user-data-dir="%USERPROFILE%\.deepseek-chrome" ^
  --disable-background-timer-throttling ^
  --disable-renderer-backgrounding ^
  --disable-backgrounding-occluded-windows ^
  --disable-features=CalculateNativeWinOcclusion ^
  https://chat.deepseek.com/
```

这个 profile 只需要第一次手动加载扩展；之后启动会保留。标签页可以放在后台，但**不要关掉**。

## 文件

| 文件 | 作用 |
|---|---|
| `bridge.py` | OpenAI 兼容 HTTP 服务 + 任务队列 + mock 页；零第三方依赖 |
| `extension/manifest.json` | MV3 扩展，匹配 DeepSeek 与本地 mock |
| `extension/background.js` | 轮询 bridge、把任务转给标签页、回传结果 |
| `extension/content.js` | 新建对话、填输入、上传图片附件、发送、等待回复、提取文本 |
| `start_bridge.bat` | Windows 启动脚本 |

## 已知限制

- **不是流式**：bridge 等网页回复完整结束后再按 chunk 回放；首个 delta 的延迟 = 网页生成时间 + 1~2 秒。
- **会话复用**：同一网页会话只追加新消息，不重发 system；页面新建/刷新、请求失败或达到轮数/字符上限会自动重置为一次完整首轮。
- **DOM 依赖**：DeepSeek 改版会导致输入框/发送按钮/消息选择器失效；`content.js` 里有候选选择器，改版时需要更新。
- **页面会短暂显示内部文本**：心声/MEM/DSTATE 是协议元数据，桌宠气泡会过滤；DeepSeek 网页本身会先显示原始回复，扩展读取后会把它们从 DOM 清掉/移除。
- **截图 8–12 秒**：一次截图要经过“模型决定调用 → 图片上传到网页 → 模型看图再回答”两轮模型 + 上传，不是卡死；扩展已用上传 spinner 轮询替代固定等待，仍受 DeepSeek 网页生成速度限制。
- **工具执行前会先显示状态**：`_执行 capture_screen..._` 现在在工具真正运行**之前**发出，长工具（截图上传/OCR/插件编译）期间气泡不会空着；最终回答前会把这行状态从气泡正文里去掉。
- **后台标签节流**：Chrome 可能把隐藏标签的 `setInterval` 降到 1 分钟；如果发现很慢，把 DeepSeek 标签保持在前台或独立窗口。
- **安全边界**：bridge 只监听 `127.0.0.1`；POST 强制 `Content-Type: application/json`，且不响应 CORS 预检，普通网页无法静默调用。它**不读取也不保存**浏览器 cookie；但本机其他进程仍可调用它，请只在可信机器上运行。
- **额度**：DeepSeek 网页版免费额度/限流由平台决定；高频使用可能触发验证码或临时限制。

## 故障排查

- `browser did not answer in time`：标签页没开、没登录、扩展没加载，或 DeepSeek 正在生成。
- `页面没有产生新回复`：发送按钮选择器失效，或网页要求验证码；在标签页里手动发一条试试。
- `/health` 返回 `queued` 一直增长：扩展没在轮询；检查 `chrome://extensions` 里扩展是否启用，刷新 DeepSeek 标签页。
- 回复为空：DeepSeek 可能在“思考”区块里；`content.js` 已剔除 think/reasoning 子树，若改版需更新选择器。
