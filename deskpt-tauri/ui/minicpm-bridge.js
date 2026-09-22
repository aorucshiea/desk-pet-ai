// minicpm-bridge.js — Tauri implementation of the `window.minicpm` bridge
// that the ORIGINAL minicpm-chat-renderer.js expects (copied verbatim from
// the Electron shell). Real implementations for the core chat flow; safe
// no-op stubs for everything the shell does not port yet — the renderer
// guards most calls, but a complete surface means no code path can crash.
(function () {
  const { invoke } = window.__TAURI__.core;
  const { listen } = window.__TAURI__.event;

  const GW_BASE = "http://127.0.0.1:18765";

  function onEvent(name) {
    return (cb) => {
      listen(name, (e) => {
        try { cb(e.payload || {}); } catch {}
      });
    };
  }

  window.minicpm = {
    // ── core: shell + gateway ──
    start: async () => {
      try {
        // Same contract as the original: returns only after the gateway is
        // up AND the local model is loaded (may take minutes on cold start).
        return await invoke("chat_start");
      } catch (e) {
        return { ok: false, error: String(e) };
      }
    },
    gatewayToken: async () => {
      try { return (await invoke("gateway_info")).token || ""; } catch { return ""; }
    },
    resize: (width, height) => invoke("chat_resize", { width, height }),
    showWindow: () => invoke("chat_show"),
    hideWindow: () => invoke("chat_hide"),
    focusWindow: () => invoke("chat_focus"),
    setChatAnchor: (bottomY) => invoke("chat_anchor", { bottomY: bottomY === null ? null : Number(bottomY) }),
    getActiveThemeId: async () => ({ ok: true, themeId: "cybercat" }),

    // ── history (shared file with the Electron shell: same brain) ──
    loadHistory: () => invoke("chat_load_history"),
    saveHistory: (history) => invoke("chat_save_history", { json: JSON.stringify(history) }),

    // ── i18n ──
    getI18n: async () => {
      // The Electron original resolves lang from its settings (default zh
      // UI). Resolve from the Rust shell (DESKPT_LANG env, default zh) —
      // WebView2's navigator.language follows the browser locale instead.
      let lang = "zh";
      try { lang = (await invoke("shell_lang")) || "zh"; } catch {}
      lang = String(lang).toLowerCase();
      return { lang: lang.startsWith("zh") ? (lang.includes("tw") || lang.includes("hk") ? "zh-TW" : "zh") : lang.startsWith("ko") ? "ko" : lang.startsWith("ja") ? "ja" : "en" };
    },

    // ── 身体感受: pet touched → immediate wake ──
    onPetTouched: onEvent("pet-touched"),

    // ── 身体移动: the model walks itself ──
    petWalk: (dx, dy) => invoke("pet_walk_to", { dx: Number(dx) || 0, dy: Number(dy) || 0 }).catch(() => {}),
    petHopDesktop: () => invoke("pet_hop_desktop").catch(() => {}),

    // ── safe stubs (features not ported in this shell) ──
    getChatParams: async () => ({}),
    getProviderPrefs: async () => ({ defaultProvider: "local", autoRoute: false, modelProviders: [] }),
    listProviders: async () => ({ providers: [] }),
    getMemory: async () => "",
    getLearnPrompt: async () => null,
    listSkills: async () => ({ skills: [] }),
    getSkill: async () => null,
    mcpListServers: async () => ({ servers: [] }),
    updateStatus: async () => ({ available: false }),
    onUpdateStatus: onEvent("update-status"),
    onUpdateApplying: onEvent("update-applying"),
    updateApply: async () => {},
    engineUpdateApply: async () => {},
    loadAdapter: async () => ({ ok: false, error: "not ported" }),
    setScreenObserveConsent: async () => {},
    openContextMenu: () => invoke("menu_show"),

    // ── event subscriptions (never fire in this shell) ──
    onOpen: onEvent("chat-open"),
    onDismiss: onEvent("chat-dismiss"),
    onReset: onEvent("chat-reset"),
    onToggleThinking: onEvent("chat-toggle-thinking"),
    onNarrate: onEvent("chat-narrate"),
    onCmdReply: onEvent("chat-cmd-reply"),
    onEditMode: onEvent("chat-edit-mode"),
    onClearHistory: onEvent("chat-clear-history"),
    onThemeChanged: onEvent("chat-theme-changed"),
    onProactivePolicy: onEvent("chat-proactive-policy"),
    onLangChange: onEvent("chat-lang-change"),
  };
})();
