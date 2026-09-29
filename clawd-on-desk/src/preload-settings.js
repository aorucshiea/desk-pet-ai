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
  // Memory tabs (settingsAPI surface, not petSettings).
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

// ── DeskPet settings tab API ──
contextBridge.exposeInMainWorld("petSettings", {
  getStatus: () => ipcRenderer.invoke("pet-settings:get-status"),
  listAdapters: () => ipcRenderer.invoke("pet-settings:list-adapters"),
  loadAdapter: (path) => ipcRenderer.invoke("pet-settings:load-adapter", { path }),
  checkUpdate: () => ipcRenderer.invoke("pet-settings:check-update"),
  applyUpdate: () => ipcRenderer.invoke("pet-settings:apply-update"),
  setNarration: (enabled) => ipcRenderer.invoke("pet-settings:set-narration", { enabled }),
  getChatParams: () => ipcRenderer.invoke("pet-settings:get-chat-params"),
  setChatParams: (params) => ipcRenderer.invoke("pet-settings:set-chat-params", { params }),
  resetChatParams: () => ipcRenderer.invoke("pet-settings:reset-chat-params"),
  getBubblePos: () => ipcRenderer.invoke("pet-settings:get-bubble-pos"),
  setBubblePos: (pos) => ipcRenderer.invoke("pet-settings:set-bubble-pos", { pos }),
  resetBubblePos: () => ipcRenderer.invoke("pet-settings:reset-bubble-pos"),
  enterBubbleEdit: () => ipcRenderer.invoke("pet-settings:enter-bubble-edit"),
  exitBubbleEdit: (save) => ipcRenderer.invoke("pet-settings:exit-bubble-edit", { save: !!save }),
  getBackendMode: () => process.env.PET_BACKEND || null,
  listDevices: () => ipcRenderer.invoke("pet-settings:list-devices"),
  setDevice: (device) => ipcRenderer.invoke("pet-settings:set-device", { device }),
  setDeviceAndRestart: (device) => ipcRenderer.invoke("pet-settings:set-device-and-restart", { device }),
  restartSidecar: () => ipcRenderer.invoke("pet-settings:restart-sidecar"),
  // Plugin kernel (Settings → Evolve): introspection + unload/rescan.
  pluginsState: () => ipcRenderer.invoke("settings:plugins-state"),
  pluginsUnload: (name) => ipcRenderer.invoke("settings:plugins-unload", { name }),
  pluginsLoad: (name) => ipcRenderer.invoke("settings:plugins-load", { name }),
  pluginsConfig: () => ipcRenderer.invoke("settings:plugins-config"),
  pluginsSetConfig: (name, values) => ipcRenderer.invoke("settings:plugins-set-config", { name, values }),
  getModelDir: () => ipcRenderer.invoke("pet-settings:get-model-dir"),
  pickModelDir: () => ipcRenderer.invoke("pet-settings:pick-model-dir"),
  listLocalModels: () => ipcRenderer.invoke("pet-settings:list-local-models"),
  useModelDir: (path) => ipcRenderer.invoke("pet-settings:use-model-dir", { path }),
  listModelFolders: () => ipcRenderer.invoke("pet-settings:list-model-folders"),
  addModelFolder: () => ipcRenderer.invoke("pet-settings:add-model-folder"),
  addModelFile: () => ipcRenderer.invoke("pet-settings:add-model-file"),
  setEngineParams: (params) => ipcRenderer.invoke("pet-settings:set-engine-params", { params }),
  getEnginePrefs: () => ipcRenderer.invoke("pet-settings:get-engine-prefs"),
  removeModelFolder: (folder) => ipcRenderer.invoke("pet-settings:remove-model-folder", { folder }),
  resetModelDir: () => ipcRenderer.invoke("pet-settings:reset-model-dir"),
  rerunOnboarding: () => ipcRenderer.invoke("pet-settings:rerun-onboarding"),
  relaunchApp: () => ipcRenderer.invoke("pet-settings:relaunch-app"),
  getLogsInfo: () => ipcRenderer.invoke("pet-settings:get-logs-info"),
  openLogsDir: () => ipcRenderer.invoke("pet-settings:open-logs-dir"),
  getResources: () => ipcRenderer.invoke("pet-settings:get-resources"),
  openModelDir: () => ipcRenderer.invoke("pet-settings:open-model-dir"),
  // llama.cpp engine (binary) self-update — Settings → Model tab.
  engineLocalVersion: () => ipcRenderer.invoke("settings:engine-local-version"),
  engineUpdateCheck: () => ipcRenderer.invoke("settings:engine-update-check"),
  engineStart: () => ipcRenderer.invoke("settings:engine-start"),
  engineStop: () => ipcRenderer.invoke("settings:engine-stop"),
  engineParams: () => ipcRenderer.invoke("settings:engine-params"),
  engineBenchmark: (nPredict) => ipcRenderer.invoke("settings:engine-benchmark", { nPredict }),
  // Holo GUI agent (H Company) — screenshot-driven desktop control.
  holoStatus: () => ipcRenderer.invoke("settings:holo-status"),
  holoRun: (task) => ipcRenderer.invoke("settings:holo-run", { task }),
  holoCancel: () => ipcRenderer.invoke("settings:holo-cancel"),
  holoTest: (baseUrl, apiKey) => ipcRenderer.invoke("settings:holo-test", { baseUrl, apiKey }),
  engineUpdateApply: () => ipcRenderer.invoke("settings:engine-update-apply"),
  engineUpdateApplyDir: () => ipcRenderer.invoke("settings:engine-update-apply-dir"),
  onEngineUpdateProgress: (cb) => {
    if (typeof cb !== "function") return () => {};
    const handler = (_e, p) => { try { cb(p || {}); } catch {} };
    ipcRenderer.on("pet:engine-update-progress", handler);
    return () => ipcRenderer.removeListener("pet:engine-update-progress", handler);
  },
  getAdapterDir: () => ipcRenderer.invoke("pet-settings:get-adapter-dir"),
  openAdapterDir: () => ipcRenderer.invoke("pet-settings:open-adapter-dir"),
  getAdapterManifest: () => ipcRenderer.invoke("pet-settings:get-adapter-manifest"),
  uploadAdapter: (payload) => ipcRenderer.invoke("pet-settings:upload-adapter", payload || {}),
  renameAdapter: (payload) => ipcRenderer.invoke("pet-settings:rename-adapter", payload || {}),
  removeAdapter: (payload) => ipcRenderer.invoke("pet-settings:remove-adapter", payload || {}),
  // Phase 2: skills + MCP (routed through the same IPC handlers the chat uses)
  listSkills: () => ipcRenderer.invoke("pet:list-skills"),
  getSkill: (name) => ipcRenderer.invoke("pet:get-skill", { name }),
  listProviders: () => ipcRenderer.invoke("pet:list-providers"),
  mcpListServers: () => ipcRenderer.invoke("pet:mcp-list-servers"),
  mcpExecute: (serverName, toolName, args) =>
    ipcRenderer.invoke("pet:mcp-execute", { serverName, toolName, args }),
  getProviderPrefs: () => ipcRenderer.invoke("pet:get-provider-prefs"),
  getChatHistory: () => ipcRenderer.invoke("pet:get-chat-history"),
  getHistoryFile: () => ipcRenderer.invoke("pet-settings:get-history-file"),
  clearChatHistory: () => ipcRenderer.invoke("pet:clear-chat-history"),
  saveProvidersConfig: (providers) => ipcRenderer.invoke("pet:save-providers-config", { providers }),
  mcpGetConfig: () => ipcRenderer.invoke("pet:mcp-get-config"),
  mcpSaveConfig: (servers) => ipcRenderer.invoke("pet:mcp-save-config", { servers }),
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
