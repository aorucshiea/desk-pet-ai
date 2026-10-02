"use strict";

const core = globalThis.ClawdSettingsCore;

// `keywords` exist so the sidebar search can find a page by the words the
// user actually has in their head ("换模型", "API key", "主动说话"), not by
// the tab's title. Settings pages live or die by search: the user knows
// WHAT they want before they know WHICH tab holds it.
const SIDEBAR_TABS = [
  { id: "general", labelKey: "sidebarGeneral", available: true, keywords: "通用 常规 启动 开机 自启 语言 general startup language" },
  // Model *management* (which local file / engine) lives on "pet"; model
  // *serving* (LM Studio, Ollama, cloud APIs) lives on "providers". They
  // used to be merged into one page — the provider block kept landing
  // underneath the model cards, which read as duplicate/confusing UI.
  { id: "pet", labelKey: "sidebarModels", available: true, keywords: "模型 本地模型 内置引擎 推理引擎 桌宠内置 llama gguf 模型文件 引擎更新 换模型 下载 model engine local file" },
  { id: "providers", labelKey: "sidebarProviders", available: true, keywords: "服务商 供应 模型供应 供应源 LM Studio Ollama API OpenAI 云 接口 key 密钥 provider api cloud endpoint" },
  // "agents" tab intentionally removed: this is a desktop pet, not a
  // coding-agent companion. See AGENTS.md / hooks/ for the removed
  // integration layer.
  // One "Theme" page (picker only) and one "Animation map" page (map rows +
  // animation/sound overrides, wrapped above).
  { id: "theme", labelKey: "sidebarTheme", available: true, keywords: "主题 外观 皮肤 形象 立绘 theme skin appearance" },
  { id: "animMap", labelKey: "sidebarAnimMap", available: true, keywords: "动画 动作 动画映射 声音 音效 animation motion sound" },
  { id: "emotion", labelKey: "sidebarEmotion", available: true, keywords: "情绪 表情 心情 情感 emotion mood feeling" },
  { id: "proactive", labelKey: "sidebarProactive", available: true, keywords: "主动 主动说话 搭话 搭讪 冲动 开口 proactive impulse initiate" },
  { id: "shortcuts", labelKey: "sidebarShortcuts", available: true, keywords: "快捷键 热键 按键 组合键 shortcut hotkey keybinding" },
  { id: "physics", labelKey: "sidebarPhysics", available: true, keywords: "物理 重力 掉落 地板 任务栏 桌面物理 physics gravity floor taskbar" },
  { id: "skills", labelKey: "sidebarSkills", available: true, keywords: "技能 记忆技能 学会 skill learn" },
  { id: "screenclick", labelKey: "sidebarScreenClick", available: true, keywords: "屏幕 看图 截图 屏幕点击 视觉 识别 画面 screen click vision ocr capture" },
  { id: "mcp", labelKey: "sidebarMcp", available: true, keywords: "MCP 工具 外部工具 服务器 mcp tool server" },
  { id: "evolve", labelKey: "sidebarEvolve", available: true, keywords: "自进化 插件 器官 内核 空间图 潜意识 evolve plugin organ kernel forge" },
  { id: "memory", labelKey: "sidebarMemory", available: true, keywords: "记忆 长期记忆 事件 回忆 遗忘 memory event recall decay" },
  { id: "history", labelKey: "sidebarHistory", available: true, keywords: "历史 记录 对话历史 聊天记录 history chat log" },
  { id: "about", labelKey: "sidebarAbout", available: true, keywords: "关于 版本 更新 升级 许可 about version update license" },
];

function getTabIcon(tabId) {
  const icons = globalThis.ClawdSettingsIcons;
  if (icons && typeof icons.getIcon === "function") return icons.getIcon(tabId);
  return "";
}

// Current sidebar search text — kept outside renderSidebar because the
// sidebar is rebuilt from scratch on every render.
let _sidebarQuery = "";

function renderSidebar() {
  const sidebar = document.getElementById("sidebar");
  if (!sidebar) return;
  sidebar.innerHTML = "";

  // ── Search: the primary way to find a setting ────────────────────────
  // 14 tabs is far past the point where anyone memorises the hierarchy, so
  // the box comes first and the tree becomes the fallback. Rebuilt on every
  // render (the sidebar is wiped), with the query and caret preserved.
  const search = document.createElement("div");
  search.className = "sidebar-search";
  const input = document.createElement("input");
  input.type = "search";
  input.className = "sidebar-search-input";
  input.placeholder = core.helpers.t("sidebarSearchPlaceholder");
  input.value = _sidebarQuery;
  const caret = _sidebarQuery.length;
  input.addEventListener("input", () => {
    _sidebarQuery = input.value || "";
    renderSidebar();
    const next = document.querySelector(".sidebar-search-input");
    if (next) {
      next.focus();
      next.setSelectionRange(next.value.length, next.value.length);
    }
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && _sidebarQuery) {
      _sidebarQuery = "";
      renderSidebar();
    }
  });
  search.appendChild(input);
  sidebar.appendChild(search);
  if (caret >= 0 && document.activeElement !== input && _sidebarQuery) {
    input.focus();
    input.setSelectionRange(caret, caret);
  }

  const q = _sidebarQuery.trim().toLowerCase();
  const matches = (tab) => {
    if (!q) return true;
    const label = core.helpers.t(tab.labelKey) || "";
    return (label + " " + (tab.keywords || "")).toLowerCase().includes(q);
  };

  // The Doctor / diagnostics sidebar entry was removed — it is an
  // upstream coding-agent feature; this build is a plain desktop pet.
  let shown = 0;
  for (const tab of SIDEBAR_TABS) {
    if (!matches(tab)) continue;
    shown += 1;
    const item = document.createElement("div");
    item.className = "sidebar-item";
    if (!tab.available) item.classList.add("disabled");
    if (tab.id === core.state.activeTab) item.classList.add("active");
    const labelText = tab.label ? tab.label : core.helpers.t(tab.labelKey);
    item.innerHTML =
      `<span class="sidebar-item-icon">${getTabIcon(tab.id)}</span>` +
      `<span class="sidebar-item-label">${core.helpers.escapeHtml(labelText)}</span>` +
      (tab.available ? "" : `<span class="sidebar-item-soon">${core.helpers.escapeHtml(core.helpers.t("sidebarSoon"))}</span>`);
    if (tab.available) {
      item.addEventListener("click", () => {
        core.ops.selectTab(tab.id);
      });
    }
    sidebar.appendChild(item);
  }
  if (!shown) {
    const empty = document.createElement("div");
    empty.className = "sidebar-search-empty";
    empty.textContent = core.helpers.t("sidebarSearchEmpty");
    sidebar.appendChild(empty);
  }
}

function renderPlaceholder(parent) {
  const div = document.createElement("div");
  div.className = "placeholder";
  div.innerHTML =
    `<div class="placeholder-icon">${getTabIcon("placeholder")}</div>` +
    `<div class="placeholder-title">${core.helpers.escapeHtml(core.helpers.t("placeholderTitle"))}</div>` +
    `<div class="placeholder-desc">${core.helpers.escapeHtml(core.helpers.t("placeholderDesc"))}</div>`;
  parent.appendChild(div);
}

function renderContent() {
  const content = document.getElementById("content");
  if (!content) return;
  core.ops.clearMountedControls();
  content.innerHTML = "";
  const tab = core.tabs[core.state.activeTab];
  if (tab && typeof tab.render === "function") {
    tab.render(content, core);
  } else {
    renderPlaceholder(content);
  }
}

core.ops.installRenderHooks({
  sidebar: renderSidebar,
  content: renderContent,
});

globalThis.ClawdSettingsTabGeneral.init(core);
// agents tab removed — see SIDEBAR_TABS above. Must stay guarded anyway:
// a bare `.init()` on a missing global aborts the rest of this file.
if (globalThis.ClawdSettingsTabAgents) globalThis.ClawdSettingsTabAgents.init(core);
globalThis.ClawdSettingsTabTheme.init(core);
globalThis.ClawdSettingsTabAnimMap.init(core);
globalThis.ClawdSettingsTabAnimOverrides.init(core);
globalThis.ClawdSettingsTabEmotion.init(core);
if (globalThis.ClawdSettingsTabProactive) globalThis.ClawdSettingsTabProactive.init(core);
globalThis.ClawdSettingsTabShortcuts.init(core);
if (globalThis.ClawdSettingsTabTelegramApproval) globalThis.ClawdSettingsTabTelegramApproval.init(core);
globalThis.ClawdSettingsTabAbout.init(core);
if (globalThis.ClawdSettingsTabRemoteSsh) globalThis.ClawdSettingsTabRemoteSsh.init(core);
if (globalThis.ClawdSettingsTabDeskPet) globalThis.ClawdSettingsTabDeskPet.init(core);
if (globalThis.ClawdSettingsTabScreenClick) globalThis.ClawdSettingsTabScreenClick.init(core);
if (globalThis.ClawdSettingsTabSkills) globalThis.ClawdSettingsTabSkills.init(core);
if (globalThis.ClawdSettingsTabProviders) globalThis.ClawdSettingsTabProviders.init(core);
if (globalThis.ClawdSettingsTabMcp) globalThis.ClawdSettingsTabMcp.init(core);
if (globalThis.ClawdSettingsTabEvolve) globalThis.ClawdSettingsTabEvolve.init(core);
if (globalThis.ClawdSettingsTabMemory) globalThis.ClawdSettingsTabMemory.init(core);
if (globalThis.ClawdSettingsTabHistory) globalThis.ClawdSettingsTabHistory.init(core);
if (globalThis.ClawdSettingsTabPhysics) globalThis.ClawdSettingsTabPhysics.init(core);
// ── Split "Theme" and "Animation map" back into two pages ─────────────
// The theme page hosts only the theme picker again. The animation-map page
// hosts the map rows plus the animation/sound-override section (each with
// its own container; the overrides tab owns timers via onExit).
if (core.tabs.animMap && core.tabs.animOverrides) {
  const animMapRender = core.tabs.animMap.render;
  const overridesRender = core.tabs.animOverrides.render;

  core.tabs.animMap.render = (parent) => {
    animMapRender(parent);
    const ovHolder = document.createElement("div");
    ovHolder.id = "themeAnimOverridesSection";
    parent.appendChild(ovHolder);
    overridesRender(ovHolder);
  };

  const animMapPatch = core.tabs.animMap.patchInPlace;
  core.tabs.animMap.patchInPlace = (changes, ctx) => {
    if (core.tabs.animOverrides.patchInPlace && core.tabs.animOverrides.patchInPlace(changes, ctx)) return true;
    return typeof animMapPatch === "function" ? animMapPatch(changes, ctx) : false;
  };

  const overridesExit = core.tabs.animOverrides.onExit;
  if (typeof overridesExit === "function") {
    core.tabs.animMap.onExit = (c) => overridesExit(c);
  }
}

// Model management ("pet") and model serving ("providers") are separate
// pages now. The old tab-merge wrapper appended the whole API-provider
// section inside the Models page — the provider block kept landing
// underneath the model cards and read as duplicate UI, so it is gone.
// "providers" owns the Active Model card, the provider list and the add
// form; nothing is appended across page boundaries any more.

if (window.settingsAPI && typeof window.settingsAPI.onChanged === "function") {
  window.settingsAPI.onChanged((payload) => core.ops.applyChanges(payload));
}

if (window.settingsAPI && typeof window.settingsAPI.onAnimationPreviewPosterReady === "function") {
  window.settingsAPI.onAnimationPreviewPosterReady((payload) => core.ops.applyAnimationPreviewPoster(payload));
}

if (window.settingsAPI && typeof window.settingsAPI.onShortcutRecordKey === "function") {
  window.settingsAPI.onShortcutRecordKey((payload) => core.ops.handleShortcutRecordKey(payload));
}

if (window.settingsAPI && typeof window.settingsAPI.onShortcutFailuresChanged === "function") {
  window.settingsAPI.onShortcutFailuresChanged((failures) => core.ops.applyShortcutFailures(failures));
}

if (window.settingsAPI && typeof window.settingsAPI.getShortcutFailures === "function") {
  window.settingsAPI.getShortcutFailures().then((failures) => {
    core.ops.applyShortcutFailures(failures);
  }).catch((err) => {
    console.warn("settings: getShortcutFailures failed", err);
  });
}

if (window.settingsAPI && typeof window.settingsAPI.getSnapshot === "function") {
  window.settingsAPI.getSnapshot().then((snapshot) => {
    core.ops.applyBootstrap(snapshot);
  });
}

if (window.settingsAPI && typeof window.settingsAPI.listAgents === "function") {
  window.settingsAPI.listAgents().then((list) => {
    core.ops.applyAgentMetadata(list);
  }).catch((err) => {
    console.warn("settings: listAgents failed", err);
    core.ops.applyAgentMetadata([]);
  });
}
