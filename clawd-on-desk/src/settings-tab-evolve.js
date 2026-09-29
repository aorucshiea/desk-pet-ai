"use strict";

// ── Self-Evolution settings tab: the plugin kernel, made visible ──
//
// The subconscious (gateway/memory organs) is a cordis-style plugin kernel:
// organs are plugins, services flow through the kernel, every registration
// lands in a reversible effect ledger, and a plugin whose dependency is
// missing parks instead of failing. This page surfaces all of it live via
// GET /api/plugins — running organs, the coeffect waiting room, the service
// table and the effect ledger — plus unload / rescan controls.

(function initSettingsTabEvolve(root) {
  let core = null; let helpers = null; let ops = null; let mounted = false;
  let pollTimer = null; let lastStateJson = "";

  function t(key) { return helpers.t(key); }

  function el(tag, attrs, ...children) {
    const e = document.createElement(tag);
    if (attrs && typeof attrs === "object" && !Array.isArray(attrs)) {
      for (const [k, v] of Object.entries(attrs)) {
        if (k === "className") e.className = v;
        else if (k === "style" && typeof v === "object") Object.assign(e.style, v);
        else if (k.startsWith("on")) e.addEventListener(k.slice(2).toLowerCase(), v);
        else if (v !== undefined && v !== null) e.setAttribute(k, v);
      }
    }
    if (children) {
      for (const c of children) {
        if (c == null) continue;
        if (typeof c === "string" || typeof c === "number") e.appendChild(document.createTextNode(String(c)));
        else if (c instanceof Node) e.appendChild(c);
        else if (Array.isArray(c)) c.forEach((child) => { if (child instanceof Node) e.appendChild(child); });
      }
    }
    return e;
  }

  function toast(message, error) {
    if (ops && typeof ops.showToast === "function") ops.showToast(message, { error: !!error });
  }

  function badge(label, tone) {
    return el("span", { className: `evl-badge tone-${tone}` }, label);
  }

  function tile(letter) {
    return el("span", { className: "evl-tile" }, letter);
  }

  function chip(text) {
    return el("span", { className: "evl-chip" }, text);
  }

  function sectionCard(title, count) {
    const head = el("div", { className: "evl-section-head" },
      el("span", { className: "evl-section-title" }, title));
    if (typeof count === "number") head.appendChild(el("span", { className: "evl-section-count" }, String(count)));
    return el("div", { className: "section-rows evl-card" }, head);
  }

  // The kernel's own file-less context — it IS the self-evolution tool.
  function isKernelCore(name) { return name === "plugin_forge"; }

  function fetchState() {
    if (window.settingsAPI && typeof window.settingsAPI.pluginsState === "function") {
      return window.settingsAPI.pluginsState().then(async (state) => {
        // Config editing arrived with the sidecar's config endpoints; until
        // then this degrades silently and the page just shows no editors.
        if (state && state.status === "ok" && typeof window.settingsAPI.pluginsConfig === "function") {
          try {
            const cfg = await window.settingsAPI.pluginsConfig();
            if (cfg && cfg.status === "ok") state.configData = cfg;
          } catch {}
        }
        return state;
      });
    }
    return Promise.resolve({ status: "error", message: "unavailable" });
  }

  function renderHeader(parent) {
    parent.appendChild(el("div", { className: "evl-header" },
      el("h2", {}, t("sidebarEvolve")),
      el("p", {}, t("evolveSubtitle")),
    ));
  }

  function renderKernelHero(parent, state) {
    const ok = !!state;
    const plugins = ok ? (state.plugins || []) : [];
    const pending = ok ? (state.pending || []) : [];
    const effects = ok ? ((state.effects && state.effects.total) || 0) : 0;
    const suspended = plugins.filter((p) => p.suspended).length;

    const hero = el("div", { className: "section-rows evl-hero" });
    const main = el("div", { className: "evl-hero-main" });
    const nameRow = el("div", { className: "evl-hero-name-row" });
    nameRow.appendChild(el("span", { className: "evl-hero-name" }, t("evolveKernelTitle")));
    nameRow.appendChild(ok
      ? badge(t("evolveKernelOk"), suspended ? "warn" : "ok")
      : badge(t("evolveKernelOffline"), "bad"));
    main.appendChild(nameRow);

    if (ok) {
      const chips = el("div", { className: "evl-hero-chips" });
      chips.appendChild(chip(`${plugins.length} ${t("evolveChipPlugins")}`));
      chips.appendChild(chip(`${pending.length} ${t("evolveChipParked")}`));
      chips.appendChild(chip(`${effects} ${t("evolveChipEffects")}`));
      main.appendChild(chips);
    } else {
      main.appendChild(el("div", { className: "evl-hero-offline" }, t("evolveOfflineHint")));
    }
    hero.appendChild(main);
    parent.appendChild(hero);
  }

  function renderPlugins(parent, state) {
    const card = sectionCard(t("evolvePluginsTitle"), (state.plugins || []).length);
    const plugins = state.plugins || [];
    if (!plugins.length) {
      card.appendChild(el("div", { className: "evl-empty" }, t("evolvePluginsEmpty")));
    }
    for (const p of plugins) {
      const row = el("div", { className: "evl-row" });
      row.appendChild(tile((p.name || "?").charAt(0).toUpperCase()));

      const info = el("div", { className: "evl-row-info" });
      const nameRow = el("div", { className: "evl-row-name" });
      nameRow.appendChild(el("span", { className: "evl-row-title" }, p.name || "?"));
      if (isKernelCore(p.name)) nameRow.appendChild(el("span", { className: "evl-tag" }, t("evolveTagKernel")));
      // muted = switched off via unload; the kernel remembers the intent
      // and keeps the row visible so the UI can offer re-enable.
      nameRow.appendChild(p.muted
        ? badge(t("evolveBadgeMuted"), "bad")
        : badge(p.suspended ? t("evolveBadgeSuspended") : t("evolveBadgeRunning"), p.suspended ? "warn" : "ok"));
      info.appendChild(nameRow);

      // Muted rows keep their capability chips: the kernel freezes the
      // tool list at unload time, so "已停用" still shows what it had.
      const chips = el("div", { className: "evl-row-chips" });
      const tools = (p.tools || []).length;
      if (tools) chips.appendChild(chip(`${t("evolveChipTools")} ${tools}`));
      for (const s of p.provides || []) chips.appendChild(chip(`→ ${s}`));
      for (const s of p.inject || []) chips.appendChild(chip(`← ${s}`));
      if (p.effects) chips.appendChild(chip(`${t("evolveChipEffects")} ${p.effects}`));
      if (chips.childNodes.length) info.appendChild(chips);
      row.appendChild(info);

      if (!isKernelCore(p.name)) {
        const btn = el("button", {
          className: "soft-btn evl-row-action",
          onclick: () => (p.muted ? enablePlugin(p.name, btn) : unloadPlugin(p.name, btn)),
        }, p.muted ? t("evolveEnable") : t("evolveUnload"));
        row.appendChild(btn);
      }
      card.appendChild(row);
    }
    parent.appendChild(card);
  }

  function renderPending(parent, state) {
    const card = sectionCard(t("evolvePendingTitle"), (state.pending || []).length);
    const pending = state.pending || [];
    if (!pending.length) {
      card.appendChild(el("div", { className: "evl-empty" }, t("evolvePendingEmpty")));
    }
    for (const p of pending) {
      const row = el("div", { className: "evl-row" });
      row.appendChild(tile((p.name || "?").charAt(0).toUpperCase()));
      const info = el("div", { className: "evl-row-info" });
      const nameRow = el("div", { className: "evl-row-name" });
      nameRow.appendChild(el("span", { className: "evl-row-title" }, p.name || "?"));
      nameRow.appendChild(badge(t("evolveBadgeParked"), "warn"));
      info.appendChild(nameRow);
      const missing = (p.missing || []).join("、");
      info.appendChild(el("div", { className: "evl-row-sub" },
        `${t("evolvePendingWaits")}${missing}`));
      info.appendChild(el("div", { className: "evl-row-hint" }, t("evolvePendingHint")));
      row.appendChild(info);
      card.appendChild(row);
    }
    parent.appendChild(card);
  }

  function renderServices(parent, state) {
    const card = sectionCard(t("evolveServicesTitle"), (state.services || []).length);
    const wrap = el("div", { className: "evl-chip-wrap" });
    for (const s of state.services || []) wrap.appendChild(chip(s));
    if (!wrap.childNodes.length) card.appendChild(el("div", { className: "evl-empty" }, t("evolveEffectsEmpty")));
    else card.appendChild(wrap);
    parent.appendChild(card);
  }

  const EFFECT_KIND_KEYS = {
    tool: "evolveKindTool",
    listener: "evolveKindListener",
    service: "evolveKindService",
    custom: "evolveKindCustom",
  };

  // ── Config editing (schema declared by the plugin itself) ───────────
  function configInput(entry, value) {
    const type = (entry && entry.type) || "str";
    const choices = entry && entry.choices;
    if (Array.isArray(choices) && choices.length) {
      const sel = el("select", { className: "evl-config-input" });
      for (const c of choices) sel.appendChild(el("option", { value: String(c) }, String(c)));
      sel.value = String(value);
      return sel;
    }
    if (type === "bool") {
      const cb = el("input", { type: "checkbox", className: "evl-config-check" });
      cb.checked = !!value;
      return cb;
    }
    const numeric = type === "int" || type === "float";
    const input = el("input", {
      className: "evl-config-input",
      type: numeric ? "number" : "text",
      step: type === "int" ? "1" : "any",
    });
    input.value = value === undefined || value === null ? "" : String(value);
    return input;
  }

  function collectValues(inputs) {
    const out = {};
    for (const [key, input] of Object.entries(inputs)) {
      if (input.type === "checkbox") out[key] = input.checked;
      else if (input.type === "number") out[key] = input.value === "" ? null : Number(input.value);
      else out[key] = input.value;
    }
    return out;
  }

  async function saveConfig(name, inputs, btn) {
    if (!window.settingsAPI || typeof window.settingsAPI.pluginsSetConfig !== "function") return;
    btn.disabled = "disabled";
    try {
      const r = await window.settingsAPI.pluginsSetConfig(name, collectValues(inputs));
      if (r && r.status === "ok") toast(t("evolveConfigSaved"));
      else toast(t("evolveConfigFail"), true);
    } catch {
      toast(t("evolveConfigFail"), true);
    }
    const panel = document.getElementById("evolve-content");
    if (panel) await refresh(panel, { force: true });
  }

  function renderConfig(parent, state) {
    const plugins = state.configData && state.configData.plugins;
    if (!plugins || typeof plugins !== "object") return; // endpoints not live yet
    const names = Object.keys(plugins)
      .filter((n) => plugins[n] && plugins[n].schema && Object.keys(plugins[n].schema).length)
      .sort();
    if (!names.length) return;
    const card = sectionCard(t("evolveConfigTitle"), names.length);
    for (const name of names) {
      const schema = plugins[name].schema || {};
      const values = plugins[name].values || {};
      const block = el("div", { className: "evl-config-block" });
      block.appendChild(el("div", { className: "evl-config-name evl-mono" }, name));
      const inputs = {};
      for (const [key, entry] of Object.entries(schema)) {
        const row = el("div", { className: "evl-config-row" });
        const label = el("label", { className: "evl-config-label" }, key);
        if (entry && entry.description) label.title = String(entry.description);
        row.appendChild(label);
        inputs[key] = configInput(entry, values[key]);
        row.appendChild(inputs[key]);
        block.appendChild(row);
      }
      const save = el("button", {
        className: "soft-btn evl-config-save",
        onclick: () => void saveConfig(name, inputs, save),
      }, t("evolveConfigSave"));
      block.appendChild(save);
      card.appendChild(block);
    }
    parent.appendChild(card);
  }

  function renderEffects(parent, state) {
    const report = state.effects || {};
    const list = report.effects || [];
    const card = sectionCard(t("evolveEffectsTitle"), report.total || 0);
    if (!list.length) {
      card.appendChild(el("div", { className: "evl-empty" }, t("evolveEffectsEmpty")));
    }
    for (const e of list) {
      const row = el("div", { className: "evl-row evl-effect-row" });
      row.appendChild(badge(t(EFFECT_KIND_KEYS[e.kind] || "evolveKindCustom"), e.kind === "service" ? "ok" : "info"));
      const info = el("div", { className: "evl-row-info" });
      info.appendChild(el("div", { className: "evl-row-title evl-mono" }, e.target || e.id || "?"));
      if (e.description) info.appendChild(el("div", { className: "evl-row-sub" }, e.description));
      row.appendChild(info);
      row.appendChild(el("span", { className: "evl-effect-owner" }, e.plugin || ""));
      card.appendChild(row);
    }
    parent.appendChild(card);
  }

  function renderExplainer(parent) {
    parent.appendChild(el("div", { className: "section-rows evl-explainer" },
      el("div", { className: "evl-explainer-title" }, t("evolveExplainerTitle")),
      el("div", { className: "evl-explainer-body" }, t("evolveExplainerTemporal")),
      el("div", { className: "evl-explainer-body" }, t("evolveExplainerSpatial")),
    ));
  }

  function renderContent(parent, state) {
    parent.innerHTML = "";
    renderHeader(parent);
    renderKernelHero(parent, state);
    if (state) {
      renderPlugins(parent, state);
      renderPending(parent, state);
      renderServices(parent, state);
      renderEffects(parent, state);
      renderConfig(parent, state);
    }
    renderExplainer(parent);
  }

  async function refresh(parent, { force = false } = {}) {
    const state = await fetchState();
    const json = JSON.stringify(state && state.status === "ok" ? state : { offline: true });
    if (!force && json === lastStateJson) return;
    // The 5s poll must never wipe an in-progress config edit: if the user's
    // focus is inside the panel, skip this tick (actions force their own).
    if (!force) {
      const active = document.activeElement;
      if (active && parent.contains(active)
        && ["INPUT", "SELECT", "TEXTAREA", "BUTTON"].includes(active.tagName)) return;
    }
    lastStateJson = json;
    renderContent(parent, state && state.status === "ok" ? state : null);
  }

  async function unloadPlugin(name, btn) {
    if (!window.settingsAPI || typeof window.settingsAPI.pluginsUnload !== "function") return;
    btn.disabled = "disabled";
    try {
      const r = await window.settingsAPI.pluginsUnload(name);
      if (r && r.status === "ok") toast(`${t("evolveUnloaded")}${name}`);
      else toast(`${t("evolveUnloadFail")}${name}`, true);
    } catch {
      toast(t("evolveUnloadFail"), true);
    }
    const panel = document.getElementById("evolve-content");
    if (panel) await refresh(panel, { force: true });
  }

  async function enablePlugin(name, btn) {
    if (!window.settingsAPI || typeof window.settingsAPI.pluginsLoad !== "function") return;
    btn.disabled = "disabled";
    try {
      const r = await window.settingsAPI.pluginsLoad(name);
      if (r && r.status === "ok") toast(`${t("evolveEnabled")}${name}`);
      else toast(`${t("evolveEnableFail")}${name}`, true);
    } catch {
      toast(t("evolveEnableFail"), true);
    }
    const panel = document.getElementById("evolve-content");
    if (panel) await refresh(panel, { force: true });
  }

  async function render(parent) {
    mounted = true; lastStateJson = "";
    parent.innerHTML = "";
    const panel = el("div", { className: "evl-panel", id: "evolve-content" });
    parent.appendChild(panel);
    await refresh(panel, { force: true });
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(() => {
      if (!mounted || document.hidden || core.state.activeTab !== "evolve") return;
      const p = document.getElementById("evolve-content");
      if (p) void refresh(p);
    }, 5000);
  }

  function init(coreArg) {
    core = coreArg; helpers = core.helpers; ops = core.ops;
    core.tabs.evolve = { render: (parent) => { void render(parent); } };
  }
  root.ClawdSettingsTabEvolve = { init };
})(typeof globalThis !== "undefined" ? globalThis : window);
