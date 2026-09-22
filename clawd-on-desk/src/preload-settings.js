"use strict";

// ── Settings panel preload ──
//
// Surface: window.settingsAPI
//
//   getSnapshot()                       Promise<snapshot>
//   update(key, value)                  Promise<{ status, message? }>
//   command(action, payload)            Promise<{ status, message? }>
//   listAgents()                        Promise<Array<{id, name, ...}>>
//   onChanged(cb)                       cb({ changes, snapshot? }) — fires for
//                                       every settings-changed broadcast
//   onAnimationPreviewPosterReady(cb)   cb({ themeId, filename, previewImageUrl,
//                                       previewPosterCacheKey }) — incremental
//                                       animation override preview poster
//
// All writes go through the main-process "settings:update" handler, which
// routes through the controller. The renderer never owns state — it always
// re-renders from the snapshot delivered via onChanged broadcasts (or the
// initial getSnapshot() call). This is the unidirectional flow contract from
// plan-settings-panel.md §4.2.

const { contextBridge, ipcRenderer } = require("electron");

const listeners = new Set();
const shortcutFailureListeners = new Set();
const shortcutRecordKeyListeners = new Set();
const remoteSshStatusListeners = new Set();
const remoteSshProgressListeners = new Set();
const hardwareBuddyStatusListeners = new Set();
const textScaleContextListeners = new Set();
ipcRenderer.on("settings-changed", (_event, payload) => {
  for (const cb of listeners) {
    try { cb(payload); } catch (err) { console.warn("settings onChanged listener threw:", err); }
  }
});
ipcRenderer.on("shortcut-failures-changed", (_event, payload) => {
  for (const cb of shortcutFailureListeners) {
    try { cb(payload); } catch (err) { console.warn("shortcut failure listener threw:", err); }
  }
});
ipcRenderer.on("shortcut-record-key", (_event, payload) => {
  for (const cb of shortcutRecordKeyListeners) {
    try { cb(payload); } catch (err) { console.warn("shortcut record listener threw:", err); }
  }
});
ipcRenderer.on("remoteSsh:status-changed", (_event, payload) => {
  for (const cb of remoteSshStatusListeners) {
    try { cb(payload); } catch (err) { console.warn("remoteSsh status listener threw:", err); }
  }
});
ipcRenderer.on("remoteSsh:progress", (_event, payload) => {
  for (const cb of remoteSshProgressListeners) {
    try { cb(payload); } catch (err) { console.warn("remoteSsh progress listener threw:", err); }
  }
});
ipcRenderer.on("hardwareBuddy:status-changed", (_event, payload) => {
  for (const cb of hardwareBuddyStatusListeners) {
    try { cb(payload); } catch (err) { console.warn("hardwareBuddy status listener threw:", err); }
  }
});
// Fired by the settings-window runtime whenever the window's effective text
// scale was re-resolved (display move, topology change, commit) — the
// committed percent lives main-side, so the slider must re-pull it.
ipcRenderer.on("settings:text-scale-context-changed", () => {
  for (const cb of textScaleContextListeners) {
    try { cb(); } catch (err) { console.warn("text scale context listener threw:", err); }
  }
});

contextBridge.exposeInMainWorld("settingsAPI", {
  getSnapshot: () => ipcRenderer.invoke("settings:get-snapshot"),
  getShortcutFailures: () => ipcRenderer.invoke("settings:getShortcutFailures"),
  getAnimationOverridesData: () => ipcRenderer.invoke("settings:get-animation-overrides-data"),
  openThemeAssetsDir: () => ipcRenderer.invoke("settings:open-theme-assets-dir"),
  previewAnimationOverride: (payload) => ipcRenderer.invoke("settings:preview-animation-override", payload),
  previewReaction: (payload) => ipcRenderer.invoke("settings:preview-reaction", payload),
  pickSoundFile: (payload) => ipcRenderer.invoke("settings:pick-sound-file", payload),
  previewSound: (payload) => ipcRenderer.invoke("settings:preview-sound", payload),
  openSoundOverridesDir: () => ipcRenderer.invoke("settings:open-sound-overrides-dir"),
  beginSizePreview: () => ipcRenderer.invoke("settings:begin-size-preview"),
  previewSize: (value) => ipcRenderer.invoke("settings:preview-size", value),
  endSizePreview: (value) => ipcRenderer.invoke("settings:end-size-preview", value),
  previewTextScale: (value) => ipcRenderer.invoke("settings:preview-text-scale", value),
  endTextScalePreview: () => ipcRenderer.invoke("settings:end-text-scale-preview"),
  getTextScaleContext: () => ipcRenderer.invoke("settings:get-text-scale-context"),
  onTextScaleContextChanged: (cb) => {
    if (typeof cb !== "function") return () => {};
    textScaleContextListeners.add(cb);
    return () => textScaleContextListeners.delete(cb);
  },
  exportAnimationOverrides: () => ipcRenderer.invoke("settings:export-animation-overrides"),
  importAnimationOverrides: () => ipcRenderer.invoke("settings:import-animation-overrides"),
  enterShortcutRecording: (actionId) => ipcRenderer.invoke("settings:enterShortcutRecording", actionId),
  exitShortcutRecording: () => ipcRenderer.invoke("settings:exitShortcutRecording"),
  update: (key, value) => ipcRenderer.invoke("settings:update", { key, value }),
  getPreviewSoundUrl: () => ipcRenderer.invoke("settings:get-preview-sound-url"),
  command: (action, payload) => ipcRenderer.invoke("settings:command", { action, payload }),
  openDashboard: () => ipcRenderer.send("settings:open-dashboard"),
  listAgents: () => ipcRenderer.invoke("settings:list-agents"),
  detectAgentInstallations: () => ipcRenderer.invoke("settings:detect-agent-installations"),
  getAboutInfo: () => ipcRenderer.invoke("settings:get-about-info"),
  checkForUpdates: () => ipcRenderer.invoke("settings:check-for-updates"),
  // Screen consent sync + Memory viewer — used by the Screen Click and
  // Memory tabs (settingsAPI surface, not minicpmSettings).
  syncScreenConsent: (value) => ipcRenderer.invoke("settings:sync-screen-consent", value),
  getMemoryView: () => ipcRenderer.invoke("settings:get-memory-view"),
  getHardwareBuddyStatus: () => ipcRenderer.invoke("settings:get-hardware-buddy-status"),
  testHardwareBuddyApproval: () => ipcRenderer.invoke("settings:test-hardware-buddy-approval"),
  getQuickCommandPresets: () => ipcRenderer.invoke("settings:get-quick-command-presets"),
  sendQuickCommand: (payload) => ipcRenderer.invoke("settings:send-quick-command", payload),
  openExternal: (url) => ipcRenderer.invoke("settings:open-external", url),
  listThemes: () => ipcRenderer.invoke("settings:list-themes"),
  getThemeEmotionMap: (themeId) => ipcRenderer.invoke("settings:get-theme-emotion-map", { themeId }),
  pickEmotionAnimation: (payload) => ipcRenderer.invoke("settings:pick-emotion-animation", payload),
  saveEmotionOverride: (payload) => ipcRenderer.invoke("settings:save-emotion-override", payload),
  setProactivePolicy: (policy) => ipcRenderer.invoke("settings:set-proactive-policy", { policy }),
  openUserThemesDir: () => ipcRenderer.invoke("settings:open-user-themes-dir"),
  importUserThemeZip: () => ipcRenderer.invoke("settings:import-user-theme-zip"),
  refreshCodexPets: () => ipcRenderer.invoke("settings:refresh-codex-pets"),
  openCodexPetsDir: () => ipcRenderer.invoke("settings:open-codex-pets-dir"),
  importCodexPetZip: () => ipcRenderer.invoke("settings:import-codex-pet-zip"),
  removeCodexPet: (themeId) => ipcRenderer.invoke("settings:remove-codex-pet", themeId),
  confirmRemoveTheme: (themeId) =>
    ipcRenderer.invoke("settings:confirm-remove-theme", themeId),
  getMobileConnectionInfo: () => ipcRenderer.invoke("settings:mobile-connection-info"),
  regenerateMobileToken: () => ipcRenderer.invoke("settings:regenerate-mobile-token"),
  resetMobileAccess: () => ipcRenderer.invoke("settings:reset-mobile-access"),
  onChanged: (cb) => {
    if (typeof cb === "function") listeners.add(cb);
  },
  onAnimationPreviewPosterReady: (cb) => {
    if (typeof cb !== "function") return () => {};
    const listener = (_event, payload) => {
      try { cb(payload); } catch (err) { console.warn("animation preview poster listener threw:", err); }
    };
    ipcRenderer.on("settings:animation-preview-poster-ready", listener);
    return () => ipcRenderer.removeListener("settings:animation-preview-poster-ready", listener);
  },
  onShortcutFailuresChanged: (cb) => {
    if (typeof cb !== "function") return () => {};
    shortcutFailureListeners.add(cb);
    return () => shortcutFailureListeners.delete(cb);
  },
  onShortcutRecordKey: (cb) => {
    if (typeof cb !== "function") return () => {};
    shortcutRecordKeyListeners.add(cb);
    return () => shortcutRecordKeyListeners.delete(cb);
  },
  onHardwareBuddyStatusChanged: (cb) => {
    if (typeof cb !== "function") return () => {};
    hardwareBuddyStatusListeners.add(cb);
    return () => hardwareBuddyStatusListeners.delete(cb);
  },
});

// ── MiniCPM settings tab API ──
contextBridge.exposeInMainWorld("minicpmSettings", {
  getStatus: () => ipcRenderer.invoke("minicpm-settings:get-status"),
  listAdapters: () => ipcRenderer.invoke("minicpm-settings:list-adapters"),
  loadAdapter: (path) => ipcRenderer.invoke("minicpm-settings:load-adapter", { path }),
  checkUpdate: () => ipcRenderer.invoke("minicpm-settings:check-update"),
  applyUpdate: () => ipcRenderer.invoke("minicpm-settings:apply-update"),
  setNarration: (enabled) => ipcRenderer.invoke("minicpm-settings:set-narration", { enabled }),
  getChatParams: () => ipcRenderer.invoke("minicpm-settings:get-chat-params"),
  setChatParams: (params) => ipcRenderer.invoke("minicpm-settings:set-chat-params", { params }),
  resetChatParams: () => ipcRenderer.invoke("minicpm-settings:reset-chat-params"),
  getBubblePos: () => ipcRenderer.invoke("minicpm-settings:get-bubble-pos"),
  setBubblePos: (pos) => ipcRenderer.invoke("minicpm-settings:set-bubble-pos", { pos }),
  resetBubblePos: () => ipcRenderer.invoke("minicpm-settings:reset-bubble-pos"),
  enterBubbleEdit: () => ipcRenderer.invoke("minicpm-settings:enter-bubble-edit"),
  exitBubbleEdit: (save) => ipcRenderer.invoke("minicpm-settings:exit-bubble-edit", { save: !!save }),
  getBackendMode: () => process.env.MINICPM_BACKEND || null,
  listDevices: () => ipcRenderer.invoke("minicpm-settings:list-devices"),
  setDevice: (device) => ipcRenderer.invoke("minicpm-settings:set-device", { device }),
  setDeviceAndRestart: (device) => ipcRenderer.invoke("minicpm-settings:set-device-and-restart", { device }),
  restartSidecar: () => ipcRenderer.invoke("minicpm-settings:restart-sidecar"),
  getModelDir: () => ipcRenderer.invoke("minicpm-settings:get-model-dir"),
  pickModelDir: () => ipcRenderer.invoke("minicpm-settings:pick-model-dir"),
  listLocalModels: () => ipcRenderer.invoke("minicpm-settings:list-local-models"),
  useModelDir: (path) => ipcRenderer.invoke("minicpm-settings:use-model-dir", { path }),
  listModelFolders: () => ipcRenderer.invoke("minicpm-settings:list-model-folders"),
  addModelFolder: () => ipcRenderer.invoke("minicpm-settings:add-model-folder"),
  removeModelFolder: (folder) => ipcRenderer.invoke("minicpm-settings:remove-model-folder", { folder }),
  resetModelDir: () => ipcRenderer.invoke("minicpm-settings:reset-model-dir"),
  rerunOnboarding: () => ipcRenderer.invoke("minicpm-settings:rerun-onboarding"),
  relaunchApp: () => ipcRenderer.invoke("minicpm-settings:relaunch-app"),
  getLogsInfo: () => ipcRenderer.invoke("minicpm-settings:get-logs-info"),
  openLogsDir: () => ipcRenderer.invoke("minicpm-settings:open-logs-dir"),
  getResources: () => ipcRenderer.invoke("minicpm-settings:get-resources"),
  openModelDir: () => ipcRenderer.invoke("minicpm-settings:open-model-dir"),
  // llama.cpp engine (binary) self-update — Settings → Model tab.
  engineLocalVersion: () => ipcRenderer.invoke("settings:engine-local-version"),
  engineUpdateCheck: () => ipcRenderer.invoke("settings:engine-update-check"),
  engineUpdateApply: () => ipcRenderer.invoke("settings:engine-update-apply"),
  engineUpdateApplyDir: () => ipcRenderer.invoke("settings:engine-update-apply-dir"),
  onEngineUpdateProgress: (cb) => {
    if (typeof cb !== "function") return () => {};
    const handler = (_e, p) => { try { cb(p || {}); } catch {} };
    ipcRenderer.on("minicpm:engine-update-progress", handler);
    return () => ipcRenderer.removeListener("minicpm:engine-update-progress", handler);
  },
  getAdapterDir: () => ipcRenderer.invoke("minicpm-settings:get-adapter-dir"),
  openAdapterDir: () => ipcRenderer.invoke("minicpm-settings:open-adapter-dir"),
  getAdapterManifest: () => ipcRenderer.invoke("minicpm-settings:get-adapter-manifest"),
  uploadAdapter: (payload) => ipcRenderer.invoke("minicpm-settings:upload-adapter", payload || {}),
  renameAdapter: (payload) => ipcRenderer.invoke("minicpm-settings:rename-adapter", payload || {}),
  removeAdapter: (payload) => ipcRenderer.invoke("minicpm-settings:remove-adapter", payload || {}),
  // Phase 2: skills + MCP (routed through the same IPC handlers the chat uses)
  listSkills: () => ipcRenderer.invoke("minicpm:list-skills"),
  getSkill: (name) => ipcRenderer.invoke("minicpm:get-skill", { name }),
  listProviders: () => ipcRenderer.invoke("minicpm:list-providers"),
  mcpListServers: () => ipcRenderer.invoke("minicpm:mcp-list-servers"),
  mcpExecute: (serverName, toolName, args) =>
    ipcRenderer.invoke("minicpm:mcp-execute", { serverName, toolName, args }),
  getProviderPrefs: () => ipcRenderer.invoke("minicpm:get-provider-prefs"),
  getChatHistory: () => ipcRenderer.invoke("minicpm:get-chat-history"),
  getHistoryFile: () => ipcRenderer.invoke("minicpm-settings:get-history-file"),
  clearChatHistory: () => ipcRenderer.invoke("minicpm:clear-chat-history"),
  saveProvidersConfig: (providers) => ipcRenderer.invoke("minicpm:save-providers-config", { providers }),
  mcpGetConfig: () => ipcRenderer.invoke("minicpm:mcp-get-config"),
  mcpSaveConfig: (servers) => ipcRenderer.invoke("minicpm:mcp-save-config", { servers }),
});

contextBridge.exposeInMainWorld("doctor", {
  runChecks: () => ipcRenderer.invoke("doctor:run-checks"),
  getReport: () => ipcRenderer.invoke("doctor:get-report"),
  testConnection: (durationMs) => ipcRenderer.invoke("doctor:test-connection", { durationMs }),
  openClawdLog: () => ipcRenderer.invoke("doctor:open-clawd-log"),
});

// ── Remote SSH (Phase 2) ──
//
// Surface: window.remoteSsh
//
//   listStatuses()                 Promise<{ status, statuses: Array<state> }>
//   status(profileId)              Promise<{ status, state }>
//   connect(profileId)             Promise<{ status, state? }>
//   disconnect(profileId)          Promise<{ status, state? }>
//   deploy(profileId)              Promise<{ status, message?, step? }>
//   authenticate(profileId)        Promise<{ status, terminal?, message? }>
//   openTerminal(profileId)        Promise<{ status, terminal?, message? }>
//   onStatusChanged(cb)            cb({ profileId, status, ... })
//   onProgress(cb)                 cb({ profileId, step, status, message? })
//
// Profile CRUD goes through the existing settingsAPI.command pathway
// (action: "remoteSsh.add" | "remoteSsh.update" | "remoteSsh.delete") so all
// writes flow through settings-controller as the single source of truth.
contextBridge.exposeInMainWorld("remoteSsh", {
  listStatuses: () => ipcRenderer.invoke("remoteSsh:list-statuses"),
  status: (profileId) => ipcRenderer.invoke("remoteSsh:status", profileId),
  connect: (profileId) => ipcRenderer.invoke("remoteSsh:connect", profileId),
  disconnect: (profileId) => ipcRenderer.invoke("remoteSsh:disconnect", profileId),
  deploy: (profileId) => ipcRenderer.invoke("remoteSsh:deploy", profileId),
  authenticate: (profileId) => ipcRenderer.invoke("remoteSsh:authenticate", profileId),
  openTerminal: (profileId) => ipcRenderer.invoke("remoteSsh:open-terminal", profileId),
  onStatusChanged: (cb) => {
    if (typeof cb !== "function") return () => {};
    remoteSshStatusListeners.add(cb);
    return () => remoteSshStatusListeners.delete(cb);
  },
  onProgress: (cb) => {
    if (typeof cb !== "function") return () => {};
    remoteSshProgressListeners.add(cb);
    return () => remoteSshProgressListeners.delete(cb);
  },
});
