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
      return window.settingsAPI.pluginsState();
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

    const actions = el("div", { className: "evl-hero-actions" });
    const rescan = el("button", {
      className: "soft-btn",
      disabled: ok ? undefined : "disabled",
      onclick: () => rescanPlugins(rescan),
    }, t("evolveRescan"));
    actions.appendChild(rescan);
    hero.appendChild(actions);
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
      nameRow.appendChild(badge(p.suspended ? t("evolveBadgeSuspended") : t("evolveBadgeRunning"), p.suspended ? "warn" : "ok"));
      info.appendChild(nameRow);

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
          onclick: () => unloadPlugin(p.name, btn),
        }, t("evolveUnload"));
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
    }
    renderExplainer(parent);
  }

  async function refresh(parent, { force = false } = {}) {
    const state = await fetchState();
    const json = JSON.stringify(state && state.status === "ok" ? state : { offline: true });
    if (!force && json === lastStateJson) return;
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

  async function rescanPlugins(btn) {
    if (!window.settingsAPI || typeof window.settingsAPI.pluginsLoad !== "function") return;
    if (btn) btn.disabled = "disabled";
    try {
      const r = await window.settingsAPI.pluginsLoad("");
      if (r && r.status === "ok") toast(t("evolveRescanDone"));
      else toast(t("evolveRescanFail"), true);
    } catch {
      toast(t("evolveRescanFail"), true);
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
