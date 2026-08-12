"use strict";

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("minicpm", {
  // Sidecar lifecycle
  start: (opts) => ipcRenderer.invoke("minicpm:start", opts),
  status: () => ipcRenderer.invoke("minicpm:status"),
  getActiveThemeId: () => ipcRenderer.invoke("minicpm:get-active-theme-id"),

  // Bubble window controls
  resize: (width, height) => ipcRenderer.invoke("minicpm:resize", { width, height }),
  setChatAnchor: (bottomY) => ipcRenderer.invoke("minicpm:set-chat-anchor", { bottomY }),
  hideWindow: () => ipcRenderer.invoke("minicpm:hide-window"),
  showWindow: () => ipcRenderer.invoke("minicpm:show-window"),
  focusWindow: () => ipcRenderer.invoke("minicpm:focus-window"),
  openContextMenu: () => ipcRenderer.send("minicpm:open-context-menu"),

  // Updater
  updateStatus: () => ipcRenderer.invoke("minicpm:update-status"),
  updateApply:  () => ipcRenderer.invoke("minicpm:update-apply"),
  engineUpdateApply: () => ipcRenderer.invoke("minicpm:engine-update-apply"),

  // Chat generation parameters (shared with Settings tab)
  getChatParams: () => ipcRenderer.invoke("minicpm:get-chat-params"),

  // Adapter (LoRA) load/unload — same IPC handler the Settings tab
  // uses, so chat-based switching ("切到猫娘") persists the user's
  // choice to prefs and shares the 90s timeout + bubble notification
  // pipeline. Pass `null` to unload.
  loadAdapter: (pathOrNull) => ipcRenderer.invoke("minicpm-settings:load-adapter", { path: pathOrNull }),

  // Skills discovery (Phase 1)
  listSkills: () => ipcRenderer.invoke("minicpm:list-skills"),
  getSkill: (name) => ipcRenderer.invoke("minicpm:get-skill", { name }),

  // Model providers (Phase 2)
  listProviders: () => ipcRenderer.invoke("minicpm:list-providers"),
  mcpListServers: () => ipcRenderer.invoke("minicpm:mcp-list-servers"),
  mcpExecute: (serverName, toolName, args) =>
    ipcRenderer.invoke("minicpm:mcp-execute", { serverName, toolName, args }),

  // Provider prefs (Phase 2)
  getProviderPrefs: () => ipcRenderer.invoke("minicpm:get-provider-prefs"),

  // Screen observation consent
  setScreenObserveConsent: (value) => ipcRenderer.invoke("minicpm:set-screen-consent", value),

  // Chat history persistence (save/load conversation history to userData)
  saveHistory: (data) => ipcRenderer.invoke("minicpm:save-history", data),
  loadHistory: () => ipcRenderer.invoke("minicpm:load-history"),

  // Long-term memory snapshot (frozen MEMORY.md + USER.md from the sidecar)
  getMemory: () => ipcRenderer.invoke("minicpm:get-memory"),

  // External distillation: /learn prompt + skill creation
  getLearnPrompt: (request) => ipcRenderer.invoke("minicpm:learn-prompt", request),
  createSkill: (payload) => ipcRenderer.invoke("minicpm:create-skill", payload),

  // i18n: initial fetch + live updates
  getI18n: () => ipcRenderer.invoke("minicpm:get-i18n"),
  onLangChange: (cb) => {
    const listener = (_e, payload) => { try { cb(payload || {}); } catch {} };
    ipcRenderer.on("minicpm:lang-change", listener);
    return () => ipcRenderer.removeListener("minicpm:lang-change", listener);
  },

  // Messages from main → renderer
  onOpen:           (cb) => ipcRenderer.on("minicpm:cmd-open",            (_e, payload) => cb(payload || {})),
  onDismiss:        (cb) => ipcRenderer.on("minicpm:cmd-dismiss",         () => cb()),
  onReset:          (cb) => ipcRenderer.on("minicpm:cmd-reset",           () => cb()),
  onToggleThinking: (cb) => ipcRenderer.on("minicpm:cmd-toggle-thinking", () => cb()),
  onUpdateStatus:   (cb) => ipcRenderer.on("minicpm:update-status",       (_e, p) => cb(p || {})),
  onUpdateApplying: (cb) => ipcRenderer.on("minicpm:update-applying",     (_e, p) => cb(p || {})),
  onNarrate:        (cb) => ipcRenderer.on("minicpm:narrate",             (_e, p) => cb(p || {})),
  onCmdReply:       (cb) => ipcRenderer.on("minicpm:cmd-reply",           (_e, p) => cb(p || {})),
  onEditMode:       (cb) => ipcRenderer.on("minicpm:edit-mode",           (_e, p) => cb(p || {})),
  onClearHistory:   (cb) => ipcRenderer.on("minicpm:clear-history",        ()  => cb()),
  onThemeChanged:   (cb) => ipcRenderer.on("minicpm:theme-changed",         (_e, p) => {
    try { cb(p || {}); } catch {}
  }),
  onProactivePolicy: (cb) => ipcRenderer.on("minicpm:set-proactive-policy", (_e, p) => {
    try { cb(p || {}); } catch {}
  }),
});
