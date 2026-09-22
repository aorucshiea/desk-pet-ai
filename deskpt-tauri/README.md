# deskpt-tauri — Tauri/Rust 桌宠外壳复刻

原项目（Electron + Python 网关）**原样保留**，这个目录是独立的 Tauri 复刻壳。
目标：外观与 Electron 原版一致，体积从 740MB 依赖降到个位数 MB。

## 复刻策略：原版 UI 文件原样跑，Rust 只当"Electron main"

原版的渲染层就是纯 Web 内容，直接搬进来跑（verbatim copy）：

- `minicpm-chat.html` + `minicpm-chat-renderer.js` + i18n —— 聊天气泡，
  像素级同款（tail、ask/speak/think 状态、流式打字机、思考折叠、自适应尺寸）
- `context-menu.html` —— 右键菜单（同款玻璃拟态样式 + 完整菜单项）
- `themes/cybercat/` —— 像素机器猫 GIF 资产 + theme.json（idle/拖拽/戳
  的反应动画全用原版文件，布局公式照抄 renderer.js 的 objectScale）

Rust 侧实现这些页面依赖的桥：

- `ui/minicpm-bridge.js` —— `window.minicpm` 全量桥（38 个方法全覆盖：
  核心走 Tauri invoke，未移植功能是安全 stub，渲染器怎么调都不会崩）
- `ui/pet-render.js` —— 由 theme.json 驱动的迷你状态机（idle/拖拽/戳）
- `ui/hit.html` —— 输入窗口（拖拽/点击爆发/右键菜单），渲染窗口点穿透
  ——和原版一样的双窗口模型
- `src/main.rs` —— 五窗口管理（pet-render / pet-hit / chat / menu）+
  网关 sidecar 自检自启 + 原版契约的 chat_start（gateway 健康 → 读原版
  minicpm-prefs.json → 找 *.gguf → /api/load-model，冷启动可能要几分钟）

## 与 Electron 版共享的东西（同一个我）

- **大脑**：同一个 Python 网关（sidecar 拉起，`MINICPM_MEMORY_DIR` 指向
  同一份 `%APPDATA%/deskpt/memories`）——记忆/心情/事件/身体感受全共享
- **对话历史**：同一份 `%APPDATA%/deskpt/chat-history.json`（按主题分桶）
- **本地模型**：同一个（prefs 里的 model_dir → /api/load-model）
- **身体感受上报**：同一个 `/api/pet/interaction`

## 已验证

- 像素猫 idle/委屈（戳）/拖拽反应 GIF 渲染 ✓
- 聊天气泡原版样式 + 中文 i18n（"问问看…"）✓
- 右键菜单原版样式完整菜单项 ✓
- 聊天全链路：原版渲染器 → 桥 → 网关 → 本地模型流式回复 ✓
- 拖拽上报 → 事件记忆落库 ✓

## 原版设置窗口（完整复刻，2026-09-19）

右键菜单"设置"打开的是**原版 settings.html 全家**（settings-renderer.js +
全部 20 个分区 tab，1.7 万行原版 JS 原样运行），带标题栏的正常大窗口：

- `settings-bridge.js` 实现全部 40 个 `window.settingsAPI` 方法：核心流
  （getSnapshot/update/command）真实工作，其余诚实 stub
- **快照就是原版的 clawd-prefs.json**——两个壳共享同一份用户设置
- 实测生效：大小滑条（P:16→小 / P:54→中，实时缩放猫身）、语言切换
- Doctor/更新/主题导入/快捷键录制/移动端等后端未移植的分区正常渲染，
  操作返回安全空值

## 未移植（原版有、这个壳还没有）

完整动画状态机（working/thinking/sleeping 等 Agent 状态联动）、眼睛跟随、
设置窗口、Dashboard、Session HUD、主动说话（NEXT_CHAT/说话冲动）、屏幕
视觉权限、MCP 工具、极简模式、i18n 切换、自动更新。菜单里对应项点了
只关菜单。


## 自进化插件系统（cordis 借鉴，2026-09-19）

网关侧新增 `gateway/petplugins.py` —— cordis 式插件容器（万物皆插件）：

- **插件** = 记忆目录下 `plugins/*.py`，入口 `apply(ctx)`；首启动会播种
  `self_note.py` 示例
- **ctx 子上下文**：每个插件独立隔离，注册的一切随卸载自动回收（可逆性）
- **依赖注入**：`inject = ["mood", "events", "memory"]` 声明服务，缺服务跳过；
  主题切换时服务自动换店
- **事件总线**：`ctx.on/emit`（theme_switched / plugins_changed / ...）
- **热重载**：5 秒轮询，模型写完插件即生效（绕过了 importlib 同秒
  __pycache__ 陷阱——这是实测踩出来的）
- **自进化闭环**：模型用 `pet_forge_plugin(name, code)` 工具铸造新插件 →
  热加载 → 下一轮对话就能用自己长出的新工具。`pet_list_plugins` /
  `pet_unload_plugin` 管理器官

## 身体移动（走路）

模型可以控制自己的身体：

- `[WALK:dx,dy]` —— 网关从流中捕获（think_filter 扩展）→ SSE `walk` 事件 →
  壳步进动画移动（clamp 在工作区内，走路期间播放拖拽反应 GIF）
- `[WALK_DESKTOP]` —— 跳到另一个虚拟桌面（documented IVirtualDesktopManager
  COM，目标桌面 GUID 从其他窗口嗅探）
- 系统提示注入【身体与进化】块告知模型这些能力

## 虚拟桌面重复修复

置顶窗口被 Windows 显示在所有虚拟桌面上（Win+Tab 看到两个桌宠）：
轮询 IsWindowOnCurrentVirtualDesktop，桌宠不在当前桌面时自动隐藏、
回来时恢复（用户手动"隐藏桌宠"不会被覆盖）。

## 运行

```powershell
# 依赖：Rust stable-msvc + WebView2 + MSVC Build Tools（本机已具备）
cd deskpt-tauri\src-tauri
cargo run               # 调试
cargo build --release   # 发布（约 11MB，含 7.8MB 主题 GIF）
```

环境变量：`DESKPT_GATEWAY_DIR`（网关目录）、`DESKPT_MEMORY_DIR`（记忆目录）、
`DESKPT_MODEL_DIR`（本地模型）、`DESKPT_THEME`（默认 cybercat）、
`DESKPT_LANG`（默认 zh）。
