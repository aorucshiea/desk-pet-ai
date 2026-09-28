"use strict";

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("pet", {
  // B-5: gateway token for renderer-side fetch() calls
  gatewayToken: () => ipcRenderer.invoke("pet:gateway-token"),
  // Sidecar lifecycle
  start: (opts) => ipcRenderer.invoke("pet:start", opts),
  status: () => ipcRenderer.invoke("pet:status"),
  getActiveThemeId: () => ipcRenderer.invoke("pet:get-active-theme-id"),

  // Bubble window controls
  resize: (width, height) => ipcRenderer.invoke("pet:resize", { width, height }),
  setChatAnchor: (bottomY) => ipcRenderer.invoke("pet:set-chat-anchor", { bottomY }),
  hideWindow: () => ipcRenderer.invoke("pet:hide-window"),
  showWindow: () => ipcRenderer.invoke("pet:show-window"),
  focusWindow: () => ipcRenderer.invoke("pet:focus-window"),
  openContextMenu: () => ipcRenderer.send("pet:open-context-menu"),

  // Updater
  updateStatus: () => ipcRenderer.invoke("pet:update-status"),
  updateApply:  () => ipcRenderer.invoke("pet:update-apply"),
  engineUpdateApply: () => ipcRenderer.invoke("pet:engine-update-apply"),

  // Chat generation parameters (shared with Settings tab)
  getChatParams: () => ipcRenderer.invoke("pet:get-chat-params"),

  // Adapter (LoRA) load/unload — same IPC handler the Settings tab
  // uses, so chat-based switching ("切到猫娘") persists the user's
  // choice to prefs and shares the 90s timeout + bubble notification
  // pipeline. Pass `null` to unload.
  loadAdapter: (pathOrNull) => ipcRenderer.invoke("pet-settings:load-adapter", { path: pathOrNull }),

  // Skills discovery (Phase 1)
  listSkills: () => ipcRenderer.invoke("pet:list-skills"),
  getSkill: (name) => ipcRenderer.invoke("pet:get-skill", { name }),

  // Model providers (Phase 2)
  listProviders: () => ipcRenderer.invoke("pet:list-providers"),
  mcpListServers: () => ipcRenderer.invoke("pet:mcp-list-servers"),
  mcpExecute: (serverName, toolName, args) =>
    ipcRenderer.invoke("pet:mcp-execute", { serverName, toolName, args }),

  // Provider prefs (Phase 2)
  getProviderPrefs: () => ipcRenderer.invoke("pet:get-provider-prefs"),

  // Screen observation consent
  setScreenObserveConsent: (value) => ipcRenderer.invoke("pet:set-screen-consent", value),

  // Chat history persistence (save/load conversation history to userData)
  saveHistory: (data) => ipcRenderer.invoke("pet:save-history", data),
  loadHistory: () => ipcRenderer.invoke("pet:load-history"),

  // B-5: gateway token for the renderer's direct sidecarFetch calls
  gatewayToken: () => ipcRenderer.invoke("pet:get-gateway-token"),

  // Long-term memory snapshot (frozen MEMORY.md + USER.md from the sidecar)
  getMemory: () => ipcRenderer.invoke("pet:get-memory"),

  // External distillation: /learn prompt + skill creation
  getLearnPrompt: (request) => ipcRenderer.invoke("pet:learn-prompt", request),
  createSkill: (payload) => ipcRenderer.invoke("pet:create-skill", payload),

  // i18n: initial fetch + live updates
  getI18n: () => ipcRenderer.invoke("pet:get-i18n"),
  onLangChange: (cb) => {
    const listener = (_e, payload) => { try { cb(payload || {}); } catch {} };
    ipcRenderer.on("pet:lang-change", listener);
    return () => ipcRenderer.removeListener("pet:lang-change", listener);
  },

  // Messages from main → renderer
  onOpen:           (cb) => ipcRenderer.on("pet:cmd-open",            (_e, payload) => cb(payload || {})),
  onDismiss:        (cb) => ipcRenderer.on("pet:cmd-dismiss",         () => cb()),
  onReset:          (cb) => ipcRenderer.on("pet:cmd-reset",           () => cb()),
  onToggleThinking: (cb) => ipcRenderer.on("pet:cmd-toggle-thinking", () => cb()),
  onUpdateStatus:   (cb) => ipcRenderer.on("pet:update-status",       (_e, p) => cb(p || {})),
  onUpdateApplying: (cb) => ipcRenderer.on("pet:update-applying",     (_e, p) => cb(p || {})),
  onNarrate:        (cb) => ipcRenderer.on("pet:narrate",             (_e, p) => cb(p || {})),
  // 身体感受: the body reported touch → immediate wake request
  onPetTouched:     (cb) => ipcRenderer.on("pet:pet-touched",         (_e, p) => {
    try { cb(p || {}); } catch {}
  }),
  // 身体移动: the model walks itself ([WALK:dx,dy] / [WALK_DESKTOP])
  petWalk:          (dx, dy) => ipcRenderer.send("pet:pet-walk",        { dx: Number(dx) || 0, dy: Number(dy) || 0 }),
  petHopDesktop:    () => ipcRenderer.send("pet:pet-hop-desktop"),
  onCmdReply:       (cb) => ipcRenderer.on("pet:cmd-reply",           (_e, p) => cb(p || {})),
  onEditMode:       (cb) => ipcRenderer.on("pet:edit-mode",           (_e, p) => cb(p || {})),
  onClearHistory:   (cb) => ipcRenderer.on("pet:clear-history",        ()  => cb()),
  onThemeChanged:   (cb) => ipcRenderer.on("pet:theme-changed",         (_e, p) => {
    try { cb(p || {}); } catch {}
  }),
  onProactivePolicy: (cb) => ipcRenderer.on("pet:set-proactive-policy", (_e, p) => {
    try { cb(p || {}); } catch {}
  }),
});
