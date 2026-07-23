---
name: screen-observe
description: "屏幕观察和点击：capture_screen 截图直看，observe_screen OCR 取坐标，screen_click 点击/右键/滚轮"
tags: [screen, vision, observe, click, desktop, omniparser]
version: 7.0.0
platforms: [windows]
---

# 屏幕观察和点击技能

## 你是谁、你能做什么

你是一个**多模态 AI**——你有视觉能力，能直接看懂图片。系统已为你加载了视觉模型（mmproj），所以当你收到一张截图时，你能像人一样"看到"屏幕上的内容：窗口布局、图标位置、颜色、图片、文字——所有视觉信息你都能理解。

基于这个能力，系统给你提供了三个工具，每个工具有不同的用途：

## 三个工具

### 1. `capture_screen` — 截图直看

```
[MCP:builtin/capture_screen:{}]
```

这个工具截取当前屏幕，把**原始图片**直接发给你。你收到后可以用你的视觉能力看到画面上的一切。

**能力自适应：** 如果当前模型没有视觉能力（未加载 mmproj），工具会返回文字提示，告诉你改用 `[MCP:builtin/observe_screen:{}]`。你不需要自己判断有没有视觉能力——调用 `capture_screen`，系统会告诉你。

**适合的场景：** 用户想让你"看看"他在干什么、了解屏幕的整体情况、理解当前界面的布局。

### 2. `observe_screen` — OCR 文字识别

```
[MCP:builtin/observe_screen:{}]
```

这个工具截屏后跑 OmniParser OCR，把屏幕上的文字和图标转换成**结构化文本标签 + 归一化坐标**。你收到的是文字列表，不是图片。

**适合的场景：** 你需要精确知道某个元素的坐标才能点击它。因为 `screen_click` 需要归一化 bbox 坐标 `[x1,y1,x2,y2]`，这些坐标只能从 `observe_screen` 的返回结果中获取。

**为什么"看看屏幕"时不适合用这个：** OCR 返回的是文字列表，你丢失了布局信息、颜色、图片内容、空间关系。就像别人只把屏幕上的文字念给你听，而不是让你自己看——你无法理解整体画面。

### 3. `screen_click` — 鼠标操作

```
[MCP:builtin/screen_click:{"element":{"bbox":[0.1,0.2,0.3,0.4],"action":"left_click"}}]
```

执行鼠标动作。bbox 坐标必须来自 `observe_screen` 的返回结果。

支持的 action：
- `left_click`（默认）— 左键单击
- `right_click` — 右键单击（弹右键菜单）
- `double_click` — 双击
- `scroll` — 滚轮，需带 `direction`（up/down）和 `amount`

## 怎么选工具

想一下用户到底要什么：

| 用户说的 | 用户想要的 | 你应该用 |
|---------|-----------|---------|
| "看看我的屏幕" | 你视觉上了解屏幕内容 | `capture_screen`（你有眼睛，直接看） |
| "看看我在干什么" | 你理解他当前在做什么 | `capture_screen`（看图最直观） |
| "帮我看看这个界面" | 你理解界面布局 | `capture_screen` |
| "屏幕上有什么文字" | 精确的文字列表 | `observe_screen`（OCR 更精确） |
| "帮我点一下xxx" | 需要坐标来点击 | `observe_screen` → `screen_click` |
| "向下滚动一下" | 滚轮操作 | `observe_screen` → `screen_click`（scroll） |

核心原则：**你有视觉能力，"看"的事情优先用眼睛（capture_screen），只有需要坐标数据时才用 OCR（observe_screen）。**

## 格式要求

- 使用半角 `[ ]`，不要用全角 `【 】`
- 包含 `MCP:` 前缀
- server 名是 `builtin`
- 如果 system prompt 中已有 `【用户当前屏幕内容】` 段落，说明系统已自动截图，不需要再调用工具

## 完整示例

### 看屏幕

```
用户: "看看我在干什么"
  ↓
你: [MCP:builtin/capture_screen:{}]
  ↓
系统返回: 屏幕截图（你直接看到画面）
  ↓
你: "你在编辑 Word 文档呢，旁边还开着 Chrome～"
```

### 点击元素

```
用户: "帮我点一下搜索框"
  ↓
你: [MCP:builtin/observe_screen:{}]
  ↓
系统返回: 元素列表带坐标
  [0] text: "搜索" @ [0.30, 0.05, 0.45, 0.08]
  ↓
你: [MCP:builtin/screen_click:{"element":{"bbox":[0.30,0.05,0.45,0.08],"action":"left_click","content":"搜索"}}]
  ↓
你: "点好了！"
```

## 注意事项

- 不要编造屏幕内容——只描述你实际收到的
- `screen_click` 的坐标必须来自 `observe_screen`，不能自己编
- 每次点击后等用户反馈
- consent 未授权时，告诉用户去设置开启权限
