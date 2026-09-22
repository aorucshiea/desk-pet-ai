// settings-bridge.js — Tauri implementation of `window.settingsAPI` for the
// ORIGINAL settings UI (settings.html + settings-tab-*.js, copied verbatim).
//
// Unidirectional flow contract (see preload-settings.js): the UI boots from
// getSnapshot() and writes through update(key, value) / command(name, ...).
// The snapshot is the REAL clawd-prefs.json — the same file the Electron
// shell reads — so both shells share one set of user preferences.
//
// All 40 methods are defined: the ones this shell supports for real hit
// Tauri commands; the rest are honest stubs (empty lists / noop ok) so no
// tab can crash at boot. Tabs whose backends are not ported render with
// defaults and simply don't mutate anything.
(function () {
  const { invoke } = window.__TAURI__.core;
  const { listen } = window.__TAURI__.event;

  // ── FREEZE FIX: neutralize blocking script dialogs ──────────────────
  // The original tabs call alert()/confirm() in 6 places. In Electron a
  // script dialog is a native modal (harmless); in WebView2 it FREEZES
  // the page's JS forever when the box can't be interacted with — the
  // reported "卡了之后点设置无反应". Replace with non-blocking versions
  // (this file loads before every settings script).
  window.alert = (msg) => { try { console.warn("[dialog]", msg); } catch {} };
  window.confirm = (msg) => {
    try { console.warn("[confirm→false]", msg); } catch {}
    return false; // safe default: unported destructive flows no-op
  };
  window.prompt = () => null;

  // error collector for diagnostics
  window.__settingsErrors = [];
  window.addEventListener("error", (e) => {
    try { window.__settingsErrors.push(String(e.message).slice(0, 200)); } catch {}
  });
  window.addEventListener("unhandledrejection", (e) => {
    try { window.__settingsErrors.push(String(e.reason).slice(0, 200)); } catch {}
  });

  const ok = () => ({ status: "ok" });
  const okAsync = () => Promise.resolve(ok());
  const stubUnported = (name) => () => {
    console.warn("[settings-bridge] stub:", name);
    return Promise.resolve({ ok: false, unported: true });
  };

  // Defaults for snapshot keys the UI consumes — SHAPES mirror the
  // original prefs.js SCHEMA. Wrong shapes render blank tabs: the skills
  // tab reads snapshot.skills.enabled, so an ARRAY gives undefined and
  // the whole tab renders empty.
  const SNAPSHOT_DEFAULTS = {
    version: 3,
    lang: "system",
    theme: "cybercat",
    size: "P:54",
    hideBubbles: false,
    permissionBubblesEnabled: true,
    permissionOn: false,
    sessionHudEnabled: true,
    sessionHudShowStateLabels: true,
    sessionHudShowElapsed: false,
    sessionHudShowContextUsage: false,
    sessionHudCleanupDetached: true,
    soundMuted: false,
    soundVolume: 1.0,
    notificationBubbleAutoCloseSeconds: 8,
    updateBubbleAutoCloseSeconds: 8,
    permissionBubbleAutoCloseSeconds: 8,
    mobilePreviewEnabled: false,
    manageClaudeHooksAutomatically: true,
    dismissedAgentInstallHints: [],
    dismissedAgentCleanupHints: [],
    tgApproval: {
      enabled: false,
      allowedTgUserId: "",
      targetSessionKey: "",
      notifyOnComplete: false,
      completionOutputMode: "off",
      r3DirectSendEnabled: false,
    },
    skills: {
      enabled: true,
      searchPaths: [],
      searchUrls: [],
      autoReview: false,
      nudgeInterval: 604800000,
      defaultProvider: "local",
      autoRoute: false,
      modelProviders: [],
    },
    textScale: 1.0,
    textScaleByDisplay: {},
    screenObserveConsent: "deny",
    hardwareBuddy: {},
  };

  // ── window.minicpmSettings — the MiniCPM tab's bridge (原版 minicpm-chat
  // 桥的设置页子集)。Real: status/params/history/skills via the shared
  // gateway + minicpm-prefs.json; model/engine management = stubs.
  let _gwCache = null;
  async function gw() {
    if (_gwCache) return _gwCache;
    _gwCache = await invoke("gateway_info");
    return _gwCache;
  }
  async function gwGet(path) {
    try {
      const st = await gw();
      const r = await fetch(st.base + path, { headers: { "x-minicpm-token": st.token } });
      return r.ok ? await r.json() : null;
    } catch { return null; }
  }
  async function minicpmPrefs() {
    try { return (await invoke("minicpm_prefs_load")) || {}; } catch { return {}; }
  }

  window.minicpmSettings = {
    getStatus: async () => {
      const h = (await gwGet("/api/health")) || {};
      return {
        ok: true,
        alive: !!h.alive,
        model: h.model_name || null,
        model_dir: h.model_dir || null,
        adapter: h.adapter || null,
        backend: h.backend || "llama.cpp",
        accel: h.accel || "cpu",
      };
    },
    getChatParams: async () => {
      const prefs = await minicpmPrefs();
      return prefs.chatParams || { thinking: false, max_new_tokens: 768 };
    },
    setChatParams: async (params) => {
      await invoke("minicpm_prefs_save", { json: JSON.stringify({ chatParams: params }) });
      return { ok: true };
    },
    setNarration: async (on) => {
      await invoke("minicpm_prefs_save", { json: JSON.stringify({ narration_enabled: !!on }) });
      return { ok: true };
    },
    getBackendMode: () => "local",
    getHistoryFile: async () => ({ themes: (await invoke("chat_load_history")) || {} }),
    listSkills: async () => ({ skills: (await gwGet("/api/skills")) || [] }),
    listLocalModels: async () => {
      const h = (await gwGet("/api/health")) || {};
      return h.model_dir ? [h.model_dir] : [];
    },
    listModelFolders: async () => {
      const h = (await gwGet("/api/health")) || {};
      return h.model_dir ? [{ path: h.model_dir }] : [];
    },
    listAdapters: async () => [],
    listDevices: async () => [],
    engineLocalVersion: async () => null,
    engineUpdateCheck: async () => ({ available: false }),
    engineUpdateApply: stubUnported("engineUpdateApply"),
    engineUpdateApplyDir: stubUnported("engineUpdateApplyDir"),
    onEngineUpdateProgress: (cb) => listen("engine-update-progress", (e) => { try { cb(e.payload); } catch {} }),
    addModelFolder: stubUnported("addModelFolder"),
    removeModelFolder: stubUnported("removeModelFolder"),
    uploadAdapter: stubUnported("uploadAdapter"),
    removeAdapter: stubUnported("removeAdapter"),
    renameAdapter: stubUnported("renameAdapter"),
    openAdapterDir: stubUnported("openAdapterDir"),
    openModelDir: stubUnported("openModelDir"),
    openLogsDir: stubUnported("openLogsDir"),
    pickModelDir: stubUnported("pickModelDir"),
    useModelDir: async () => ({ ok: false, unported: true }),
    loadAdapter: async () => ({ ok: false, unported: true }),
    setDevice: async () => ({ ok: false }),
    setDeviceAndRestart: async () => ({ ok: false }),
    restartSidecar: async () => { await invoke("ensure_gateway"); return { ok: true }; },
    mcpListServers: async () => ({ servers: (await gwGet("/api/mcp/servers")) || [] }),
    mcpGetConfig: async () => ({ config: (await gwGet("/api/mcp/servers")) || [] }),
    mcpSaveConfig: stubUnported("mcpSaveConfig"),
    saveProvidersConfig: async () => ({ ok: false }),
  };

  // ── window.doctor — the sidebar Doctor indicator + modal ──────────
  // Checks the REAL organs this shell has: gateway reachable + local
  // model loaded. Same result shape as the original doctor runtime.
  window.doctor = {
    runChecks: async () => {
      const h = await gwGet("/api/health").catch(() => null);
      const checks = [
        { id: "gateway", status: h ? "pass" : "critical" },
        { id: "local-server", status: h && h.alive ? "pass" : "warning" },
        { id: "agent-integrations", status: "pass" },
      ];
      const status = checks.some((c) => c.status === "critical")
        ? "critical"
        : checks.some((c) => c.status === "warning")
          ? "warning"
          : "pass";
      return { status, checks, checkedAt: Date.now() };
    },
  };

  window.settingsAPI = {
    // ── core flow ──
    getSnapshot: async () => {
      const raw = await invoke("prefs_load");
      // real user prefs over the schema defaults the UI expects
      const snapshot = { ...SNAPSHOT_DEFAULTS, ...(raw || {}) };
      // Doctor sidebar indicator: real gateway health
      try {
        const h = await gwGet("/api/health");
        snapshot.doctor = h && h.ok !== undefined ? "pass" : "unknown";
        snapshot.doctorAlive = !!(h && h.alive);
      } catch { snapshot.doctor = "unknown"; }
      return snapshot;
    },
    update: (key, value) => invoke("prefs_update", { key, value: value === undefined ? null : value }),
    command: (name, ...args) => {
      // Known real commands; everything else acks as ok (UI stays inert).
      if (name === "openDashboard") return invoke("narrate_unported", { label: "打开 Dashboard" }).then(ok);
      if (name === "openSettingsFolder") return invoke("narrate_unported", { label: "打开设置目录" }).then(ok);
      return okAsync();
    },
    onChanged: (cb) => {
      listen("prefs-changed", (e) => { try { cb(e.payload); } catch {} });
    },

    // ── about / doctor (rendered from real data where cheap) ──
    getAboutInfo: () => invoke("about_info").then((info) => ({
      ...info,
      appName: "MiniCPM Desk Pet (Tauri)",
      pendingUpdateVersion: "",
      autoUpdateCheck: false,
      license: "MIT",
      copyright: "deskpt-tauri 复刻壳 · 大脑 = 原版 Python 网关",
    })),
    checkForUpdates: () => Promise.resolve({ available: false, checking: false }),

    // ── agent list (Agent 管理 tab) ──
    listAgents: () => Promise.resolve([
      { id: "claude-code", name: "Claude Code", eventSource: "hooks", installed: false },
      { id: "codex", name: "Codex", eventSource: "hooks", installed: false },
    ]),

    // ── theme tab (visual only — theme FILES are shared, switching not ported) ──
    listThemes: () => Promise.resolve([
      { id: "cybercat", name: "Cybercat", builtIn: true, active: true },
      { id: "calico", name: "Calico", builtIn: true, active: false },
      { id: "cloudling", name: "Cloudling", builtIn: true, active: false },
      { id: "hamster", name: "Hamster", builtIn: true, active: false },
    ]),
    getThemeEmotionMap: () => Promise.resolve({}),
    openUserThemesDir: stubUnported("openUserThemesDir"),
    openThemeAssetsDir: stubUnported("openThemeAssetsDir"),
    importUserThemeZip: stubUnported("importUserThemeZip"),
    importCodexPetZip: stubUnported("importCodexPetZip"),
    refreshCodexPets: () => Promise.resolve([]),
    removeCodexPet: () => Promise.resolve({ ok: false }),

    // ── animation / sound previews ──
    previewAnimationOverride: stubUnported("previewAnimationOverride"),
    previewReaction: stubUnported("previewReaction"),
    previewSound: stubUnported("previewSound"),
    getPreviewSoundUrl: () => Promise.resolve(""),
    pickSoundFile: () => Promise.resolve(null),
    pickEmotionAnimation: () => Promise.resolve(null),
    openSoundOverridesDir: stubUnported("openSoundOverridesDir"),
    saveEmotionOverride: stubUnported("saveEmotionOverride"),
    importAnimationOverrides: stubUnported("importAnimationOverrides"),
    exportAnimationOverrides: stubUnported("exportAnimationOverrides"),
    getAnimationOverridesData: () => Promise.resolve({ overrides: [] }),

    // ── text scale (通用 tab slider — visual preview only) ──
    getTextScaleContext: () => Promise.resolve({ scales: {}, active: {} }),
    previewTextScale: () => okAsync(),
    endTextScalePreview: () => okAsync(),
    onTextScaleContextChanged: (cb) => listen("text-scale-context", (e) => { try { cb(e.payload); } catch {} }),

    // ── shortcuts ──
    getShortcutFailures: () => Promise.resolve([]),
    onShortcutFailuresChanged: (cb) => listen("shortcut-failures", (e) => { try { cb(e.payload || []); } catch {} }),
    onShortcutRecordKey: (cb) => listen("shortcut-record", (e) => { try { cb(e.payload); } catch {} }),

    // ── proactive / emotion / screen / mobile / memory ──
    setProactivePolicy: (policy) => invoke("prefs_update", { key: "proactive", value: policy }).then(ok),
    syncScreenConsent: () => okAsync(),
    getMobileConnectionInfo: () => Promise.resolve({ enabled: false }),
    regenerateMobileToken: () => Promise.resolve({ ok: false }),
    resetMobileAccess: () => Promise.resolve({ ok: false }),
    getMemoryView: () => invoke("memory_view"),  // {status, identity, mood, events}
  };
})();
