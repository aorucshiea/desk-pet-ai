"use strict";

// ── Model Providers settings tab ──
// Where do model responses come from? A two-pane page in the ZCode /
// CherryStudio style: a grouped source list on the left (built-in engine,
// local servers, custom APIs) with the active source marked, and a detail
// pane on the right for the selected source. Adding a provider happens in
// the right pane too — preset cards for local servers, or a manual
// OpenAI-compatible form — never as a permanently expanded raw form.
//
// Data model (unchanged): snapshot.skills.defaultProvider ("local" or a
// provider id), snapshot.skills.modelProviders
// ([{ provider, apiKey, baseUrl, model, thinking, reasoningEffort,
// contextWindow }]). saveProviders() persists to prefs, mirrors
// providers.json and restarts the sidecar.

(function initSettingsTabProviders(root) {
  let core = null;
  let helpers = null;
  let ops = null;
  // Which source the right pane shows. Session-only; falls back to the
  // active provider on every mount so the page opens on "where am I".
  let selectedSource = null;
  let _sourceMode = null; // "cloud" | "local" - which half of the page

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

  function softBtn(label, onClick, opts = {}) {
    const b = el("button", {
      type: "button",
      className: "soft-btn" + (opts.accent ? " accent" : ""),
      onClick,
    });
    b.textContent = label;
    if (opts.disabled) b.disabled = true;
    return b;
  }

  // The design-system switch (same markup the pet tab's switchRow uses).
  function switchEl(checked, onChange) {
    const sw = el("div", {
      className: "switch" + (checked ? " on" : ""),
      role: "switch",
      tabindex: "0",
      "aria-checked": checked ? "true" : "false",
    });
    const run = () => {
      const next = !sw.classList.contains("on");
      sw.classList.toggle("on", next);
      sw.setAttribute("aria-checked", next ? "true" : "false");
      try { onChange(next); } catch {}
    };
    sw.addEventListener("click", run);
    sw.addEventListener("keydown", (ev) => {
      if (ev.key === " " || ev.key === "Enter") { ev.preventDefault(); run(); }
    });
    return sw;
  }

  function toast(message, error) {
    if (ops && typeof ops.showToast === "function") {
      ops.showToast(message, { error: !!error });
    }
  }

  async function saveField(field, value) {
    try {
      if (window.settingsAPI && typeof window.settingsAPI.update === "function") {
        const live = (core.state.snapshot && core.state.snapshot.skills) || {};
        await window.settingsAPI.update("skills", { ...live, [field]: value });
      }
    } catch {}
  }

  async function saveProviders(providers) {
    try {
      if (window.settingsAPI && typeof window.settingsAPI.update === "function") {
        const live = (core.state.snapshot && core.state.snapshot.skills) || {};
        await window.settingsAPI.update("skills", { ...live, modelProviders: providers });
      }
      // Sync to providers.json and reload the gateway so the new routing
      // takes effect without a manual restart.
      if (window.petSettings && typeof window.petSettings.saveProvidersConfig === "function") {
        await window.petSettings.saveProvidersConfig(providers);
      }
      if (window.petSettings && typeof window.petSettings.restartSidecar === "function") {
        await window.petSettings.restartSidecar();
      }
    } catch {}
  }

  // ── Source list tiles ──────────────────────────────────────────────────
  // Rounded monogram tiles stand in for provider logos (ZCode-style list
  // without shipping brand assets).
  function tile(label) {
    return el("span", { className: "prov-tile", "aria-hidden": "true" }, label);
  }

  function tileFor(id) {
    if (id === "local") return "❖";
    if (id === "lmstudio") return "LM";
    if (id === "ollama") return "O";
    return (id || "?").slice(0, 2).toUpperCase();
  }

  function displayName(p) {
    if (!p) return t("provLocalName");
    if (p.provider === "lmstudio") return "LM Studio";
    if (p.provider === "ollama") return "Ollama";
    return p.provider;
  }

  function subLabel(p) {
    if (p && p.baseUrl) return p.baseUrl.replace(/^https?:\/\//, "");
    if (p && p.provider === "lmstudio") return "127.0.0.1:1234";
    if (p && p.provider === "ollama") return "127.0.0.1:11434";
    return "";
  }

  // ── Page header ────────────────────────────────────────────────────────
  function renderHeader(parent) {
    const wrap = el("div", { className: "pet-page-header" });
    const textCol = el("div", { className: "pet-page-header-text" });
    textCol.appendChild(el("h1", {}, t("provTitle")));
    textCol.appendChild(el("p", { className: "subtitle" }, t("provSubtitle")));
    wrap.appendChild(textCol);
    parent.appendChild(wrap);

    // ── 云模型 / 本地模型: the one question this page answers first ──
    const seg = el("div", { className: "prov-mode-switch" });
    const activeMode = _sourceMode || "cloud";
    for (const opt of ["cloud", "local"]) {
      seg.appendChild(el("button", {
        type: "button",
        className: "prov-mode-btn" + (activeMode === opt ? " is-active" : ""),
        onClick: () => { _sourceMode = opt; renderAll(); },
      }, t(opt === "cloud" ? "provModeCloud" : "provModeLocal")));
    }
    wrap.appendChild(seg);
  }

  // ── What is in use right now — the first thing a user must see ──
  // Two settings pages both touch "the model" (this one picks WHO serves
  // it, the Models page picks WHICH local file), so this card says plainly
  // which one is live and where the other half is configured. That is the
  // whole point of the card: no more "which page do I click?"
  function renderStateRow(parent, skills, providers) {
    const defaultProvider = skills.defaultProvider || "local";
    const isLocal = defaultProvider === "local";
    const active = isLocal ? null : providers.find((p) => p && p.provider === defaultProvider);
    const name = active ? displayName(active) : t("provLocalName");
    const model = active && active.model ? active.model : "";

    const card = el("div", { className: "section-rows prov-active-card" });
    const line1 = el("div", { className: "prov-active-row" });
    line1.appendChild(el("span", { className: "prov-active-label" }, t("provNowUsing")));
    line1.appendChild(el("span", { className: "prov-active-name" }, name));
    if (model) line1.appendChild(el("span", { className: "prov-active-model" }, model));
    line1.appendChild(el("span", { className: "prov-active-tag" }, t("provInUse")));
    card.appendChild(line1);

    card.appendChild(el("div", { className: "prov-active-kind" },
      isLocal
        ? t("provKindBuiltin")
        : (active && (active.provider === "lmstudio" || active.provider === "ollama"))
          ? t("provKindLocalApi")
          : t("provKindRemote")));
    card.appendChild(el("div", { className: "prov-active-explain" }, t("provActiveExplain")));
    parent.appendChild(card);
  }

  // ── Left pane: grouped source list ─────────────────────────────────────
  function groupTitle(label, first) {
    return el("div", {
      className: "section-title prov-group-title" + (first ? " is-first" : ""),
    }, label);
  }

  function sourceItem({ id, p, selected, inUse, onSelect }) {
    const item = el("button", {
      type: "button",
      className: "prov-item" + (selected ? " selected" : ""),
      onClick: onSelect,
    });
    item.appendChild(tile(tileFor(id)));
    const text = el("div", { className: "prov-item-text" });
    text.appendChild(el("span", { className: "prov-item-name" }, displayName(p)));
    const sub = subLabel(p);
    if (sub) text.appendChild(el("span", { className: "prov-item-sub" }, sub));
    item.appendChild(text);
    if (inUse) item.appendChild(el("span", { className: "prov-tag" }, t("provInUse")));
    else item.appendChild(el("span", { className: "prov-dot" + (p ? "" : " idle") }));
    return item;
  }

  // A sub-group heading under a parent group (used for the two kinds of
  // "local": something you run yourself, or the engine built into the pet).
  function subGroupTitle(label) {
    return el("div", { className: "prov-subgroup-title" }, label);
  }

  // ── Left pane: sources grouped the way the user thinks about them ──
  //
  //   服务商          — models served by someone else's machine
  //   本地
  //     本地推理引擎 API — LM Studio / Ollama, running on THIS machine
  //     桌宠内置推理引擎 — the llama.cpp build the pet ships, hot-updatable
  //
  // The old order (Builtin / LocalServers / Custom) read as three unrelated
  // lists; this one answers "where does the model live?" first.
  function renderList(listEl, skills, providers) {
    const defaultProvider = skills.defaultProvider || "local";
    const select = (id) => {
      selectedSource = id;
      renderAll();
    };

    // Which half is the user looking at? Default follows whatever is
    // actually in use, so the page opens on the relevant side.
    const isLocalId = (id) => id === "local" || id === "lmstudio" || id === "ollama";
    const mode = _sourceMode || (isLocalId(defaultProvider) ? "local" : "cloud");

    const localServers = providers.filter((p) => p && (p.provider === "lmstudio" || p.provider === "ollama"));
    const custom = providers.filter((p) => p && p.provider !== "lmstudio" && p.provider !== "ollama");

    if (mode === "cloud") {
      // ── 云模型：anything served from someone else's machine ──────────
      listEl.appendChild(groupTitle(t("provGroupCustom"), true));
      if (!custom.length) {
        listEl.appendChild(el("div", { className: "prov-subgroup-empty" }, t("provNoCloud")));
      }
      for (const p of custom) {
        listEl.appendChild(sourceItem({
          id: p.provider, p,
          selected: selectedSource === p.provider,
          inUse: defaultProvider === p.provider,
          onSelect: () => select(p.provider),
        }));
      }
      listEl.appendChild(softBtn("+ " + t("provAddProvider"), () => {
        selectedSource = "__add__";
        renderAll();
      }, { accent: selectedSource === "__add__" })).classList.add("prov-add-btn");
      return;
    }

    // ── 本地模型：runs on this machine ────────────────────────────────
    // 1. a local inference API you run yourself (LM Studio / Ollama)
    listEl.appendChild(groupTitle(t("provGroupLocalServers"), true));
    if (!localServers.length) {
      listEl.appendChild(el("div", { className: "prov-subgroup-empty" }, t("provNoLocalApi")));
    }
    for (const p of localServers) {
      listEl.appendChild(sourceItem({
        id: p.provider, p,
        selected: selectedSource === p.provider,
        inUse: defaultProvider === p.provider,
        onSelect: () => select(p.provider),
      }));
    }
    // 2. the engine that ships with the pet — LAST, by request
    listEl.appendChild(subGroupTitle(t("provGroupBuiltin")));
    listEl.appendChild(sourceItem({
      id: "local", p: null,
      selected: selectedSource === "local",
      inUse: defaultProvider === "local",
      onSelect: () => select("local"),
    }));
  }

  // ── Right pane: detail for the selected source ─────────────────────────
  function fieldRow(label, input) {
    const row = el("div", { className: "prov-field" });
    row.appendChild(el("span", { className: "prov-field-label" }, label));
    row.appendChild(input);
    return row;
  }

  function textInput(value, opts = {}) {
    return el("input", {
      type: opts.type || "text",
      className: "prov-input",
      placeholder: opts.placeholder || "",
      value: value != null ? String(value) : "",
    });
  }

  function useButton(defaultProvider, id) {
    if (defaultProvider === id) return null;
    return softBtn(t("provUseIt"), async () => {
      await saveField("defaultProvider", id);
      renderAll();
    }, { accent: true });
  }

  function renderLocalDetail(d, skills) {
    d.appendChild(el("div", { className: "prov-detail-title" },
      tile(tileFor("local")),
      el("span", { className: "prov-detail-name" }, t("provLocalName")),
      skills.defaultProvider === "local" ? el("span", { className: "prov-tag" }, t("provInUse")) : null));

    d.appendChild(el("p", { className: "prov-detail-desc" }, t("provLocalDesc")));

    // Live engine status line — filled async, same source as the Models page.
    const statusLine = el("div", { className: "prov-detail-status" }, t("provStatusChecking"));
    d.appendChild(statusLine);
    if (window.petSettings && typeof window.petSettings.getStatus === "function") {
      window.petSettings.getStatus().then((st) => {
        if (!statusLine.isConnected) return;
        const h = (st && st.health) || {};
        const ready = !!(st && (st.sidecarReady || st.healthy));
        const llama = ready && (h.alive === true || !!(h.llama_server && h.llama_server.status === "ok"));
        const name = h.model_name || "";
        statusLine.textContent = llama
          ? `● ${t("provStatusRunning")}${name ? " · " + name : ""}`
          : (ready ? `● ${t("provStatusWarming")}` : `● ${t("provStatusOffline")}`);
        statusLine.className = "prov-detail-status " + (llama || ready ? "is-ok" : "is-off");
      }).catch(() => {
        if (statusLine.isConnected) {
          statusLine.textContent = `● ${t("provStatusOffline")}`;
          statusLine.className = "prov-detail-status is-off";
        }
      });
    }

    const actions = el("div", { className: "prov-actions" });
    const use = useButton(skills.defaultProvider || "local", "local");
    if (use) actions.appendChild(use);
    if (actions.childElementCount > 0) d.appendChild(actions);
  }

  function renderServerDetail(d, skills, providers, p) {
    const id = p.provider;
    d.appendChild(el("div", { className: "prov-detail-title" },
      tile(tileFor(id)),
      el("span", { className: "prov-detail-name" }, displayName(p)),
      skills.defaultProvider === id ? el("span", { className: "prov-tag" }, t("provInUse")) : null));

    d.appendChild(el("p", { className: "prov-detail-desc" },
      id === "lmstudio" ? t("provLmstudioDesc") : t("provOllamaDesc")));

    const urlInp = textInput(p.baseUrl, { placeholder: id === "lmstudio" ? "http://127.0.0.1:1234/v1" : "http://127.0.0.1:11434/v1" });
    const modelInp = textInput(p.model, { placeholder: id === "ollama" ? "llama3.2" : "local-model" });
    d.appendChild(fieldRow(t("provFieldUrl"), urlInp));
    d.appendChild(fieldRow(t("provFieldModel"), modelInp));

    const actions = el("div", { className: "prov-actions" });
    const use = useButton(skills.defaultProvider || "local", id);
    if (use) actions.appendChild(use);
    actions.appendChild(softBtn(t("provSave"), async () => {
      const updated = {
        ...p,
        baseUrl: urlInp.value.trim() || p.baseUrl,
        model: modelInp.value.trim() || p.model,
      };
      await saveProviders(providers.map((x) => x.provider === id ? updated : x));
      toast(t("provSaved"));
      renderAll();
    }));
    actions.appendChild(softBtn(t("provRemove"), async () => {
      await saveProviders(providers.filter((x) => x.provider !== id));
      if (selectedSource === id) selectedSource = skills.defaultProvider || "local";
      renderAll();
    }));
    d.appendChild(actions);
  }

  function renderCustomDetail(d, skills, providers, p) {
    const id = p.provider;
    d.appendChild(el("div", { className: "prov-detail-title" },
      tile(tileFor(id)),
      el("span", { className: "prov-detail-name" }, displayName(p)),
      skills.defaultProvider === id ? el("span", { className: "prov-tag" }, t("provInUse")) : null));

    d.appendChild(el("p", { className: "prov-detail-desc" }, t("provCustomDesc")));

    const keyInp = textInput(p.apiKey, { type: "password", placeholder: "sk-..." });
    const urlInp = textInput(p.baseUrl, { placeholder: "https://api.deepseek.com/v1" });
    const modelInp = textInput(p.model, { placeholder: "deepseek-chat" });
    const ctxInp = textInput(p.contextWindow, { placeholder: "131072" });
    d.appendChild(fieldRow(t("provFieldKey"), keyInp));
    d.appendChild(fieldRow(t("provFieldUrl"), urlInp));
    d.appendChild(fieldRow(t("provFieldModel"), modelInp));
    d.appendChild(fieldRow(t("provFieldContext"), ctxInp));

    const thinkRow = el("div", { className: "prov-field" });
    thinkRow.appendChild(el("span", { className: "prov-field-label" }, t("provThinking")));
    const thinkWrap = el("div", { className: "row-control" });
    thinkWrap.appendChild(switchEl(!!p.thinking, () => {}));
    const thinkCb = thinkWrap.querySelector(".switch");
    thinkRow.appendChild(thinkWrap);
    d.appendChild(thinkRow);

    const effortSel = el("select", { className: "prov-input", style: { flex: "0 0 140px" } });
    ["", "low", "medium", "high", "xhigh", "max"].forEach((v) => {
      const opt = el("option", { value: v }, v || t("provEffortDefault"));
      if (v === (p.reasoningEffort || "")) opt.selected = true;
      effortSel.appendChild(opt);
    });
    d.appendChild(fieldRow(t("provEffort"), effortSel));

    const actions = el("div", { className: "prov-actions" });
    const use = useButton(skills.defaultProvider || "local", id);
    if (use) actions.appendChild(use);
    actions.appendChild(softBtn(t("provSave"), async () => {
      const updated = {
        ...p,
        apiKey: keyInp.value.trim() || p.apiKey,
        baseUrl: urlInp.value.trim() || "https://api.openai.com/v1",
        model: modelInp.value.trim() || null,
        thinking: thinkCb.classList.contains("on"),
        reasoningEffort: effortSel.value || null,
        contextWindow: Number(ctxInp.value) || null,
      };
      await saveProviders(providers.map((x) => x.provider === id ? updated : x));
      toast(t("provSaved"));
      renderAll();
    }));
    actions.appendChild(softBtn(t("provRemove"), async () => {
      await saveProviders(providers.filter((x) => x.provider !== id));
      if (selectedSource === id) selectedSource = skills.defaultProvider || "local";
      renderAll();
    }));
    d.appendChild(actions);
  }

  function renderAddDetail(d, skills, providers) {
    d.appendChild(el("div", { className: "prov-detail-title" },
      el("span", { className: "prov-detail-name" }, t("provAddTitle"))));
    d.appendChild(el("p", { className: "prov-detail-desc" }, t("provAddHint")));

    // Preset cards — the recommended path for local models: LM Studio /
    // Ollama already handle engine binaries; the pet just talks to them.
    const grid = el("div", { className: "prov-preset-grid" });
    const mkPreset = (name, desc, cfg) => {
      const card = el("button", { type: "button", className: "prov-preset-card" });
      card.appendChild(el("span", { className: "prov-preset-name" }, tile(tileFor(cfg.provider)), name));
      card.appendChild(el("span", { className: "prov-preset-desc" }, desc));
      card.addEventListener("click", async () => {
        const next = providers.filter((x) => x.provider !== cfg.provider);
        next.push(cfg);
        await saveProviders(next);
        selectedSource = cfg.provider;
        renderAll();
      });
      return card;
    };
    grid.appendChild(mkPreset("LM Studio", t("provPresetLmstudioDesc"), {
      provider: "lmstudio",
      apiKey: "lm-studio",          // LM Studio ignores the key
      baseUrl: "http://127.0.0.1:1234/v1",
      model: "local-model",         // LM Studio serves whatever is loaded
      thinking: false,
      reasoningEffort: null,
      contextWindow: null,
    }));
    grid.appendChild(mkPreset("Ollama", t("provPresetOllamaDesc"), {
      provider: "ollama",
      apiKey: "ollama",             // Ollama ignores the key locally
      baseUrl: "http://127.0.0.1:11434/v1",
      model: "llama3.2",            // change to a model you actually pulled
      thinking: false,
      reasoningEffort: null,
      contextWindow: null,
    }));
    d.appendChild(grid);

    // Manual OpenAI-compatible form.
    d.appendChild(el("div", { className: "section-title prov-manual-title" }, t("provManualTitle")));
    const idInp = textInput("", { placeholder: "e.g. deepseek" });
    const keyInp = textInput("", { type: "password", placeholder: "sk-..." });
    const urlInp = textInput("", { placeholder: "https://api.deepseek.com/v1" });
    const modelInp = textInput("", { placeholder: "deepseek-chat" });
    const ctxInp = textInput("", { placeholder: "131072" });
    d.appendChild(fieldRow(t("provFieldId"), idInp));
    d.appendChild(fieldRow(t("provFieldKey"), keyInp));
    d.appendChild(fieldRow(t("provFieldUrl"), urlInp));
    d.appendChild(fieldRow(t("provFieldModel"), modelInp));
    d.appendChild(fieldRow(t("provFieldContext"), ctxInp));

    const thinkRow = el("div", { className: "prov-field" });
    thinkRow.appendChild(el("span", { className: "prov-field-label" }, t("provThinking")));
    const thinkWrap = el("div", { className: "row-control" });
    thinkWrap.appendChild(switchEl(true, () => {}));
    const thinkCb = thinkWrap.querySelector(".switch");
    thinkRow.appendChild(thinkWrap);
    d.appendChild(thinkRow);

    const effortSel = el("select", { className: "prov-input", style: { flex: "0 0 140px" } });
    ["", "low", "medium", "high", "xhigh", "max"].forEach((v) => {
      effortSel.appendChild(el("option", { value: v }, v || t("provEffortDefault")));
    });
    d.appendChild(fieldRow(t("provEffort"), effortSel));

    const actions = el("div", { className: "prov-actions" });
    actions.appendChild(softBtn(t("provAddAction"), async () => {
      const id = (idInp.value || "").replace(/\s+/g, "").toLowerCase();
      if (!id || !(keyInp.value || "").trim()) {
        toast(t("provAddMissing"), true);
        return;
      }
      const newProviders = providers.filter((x) => x.provider !== id);
      newProviders.push({
        provider: id,
        apiKey: keyInp.value.trim(),
        baseUrl: urlInp.value.trim() || "https://api.openai.com/v1",
        model: modelInp.value.trim() || "gpt-4o",
        thinking: thinkCb.classList.contains("on"),
        reasoningEffort: effortSel.value || null,
        contextWindow: Number(ctxInp.value) || null,
      });
      await saveProviders(newProviders);
      selectedSource = id;
      renderAll();
    }, { accent: true }));
    d.appendChild(actions);
  }

  function renderDetail(d, skills, providers) {
    d.innerHTML = "";
    const defaultProvider = skills.defaultProvider || "local";
    if (!selectedSource) selectedSource = defaultProvider;

    if (selectedSource === "__add__") {
      renderAddDetail(d, skills, providers);
      return;
    }
    if (selectedSource === "local") {
      renderLocalDetail(d, skills);
      return;
    }
    const p = providers.find((x) => x && x.provider === selectedSource);
    if (!p) {
      // The source disappeared (removed elsewhere) — fall back gracefully.
      selectedSource = defaultProvider;
      if (selectedSource === "local") { renderLocalDetail(d, skills); return; }
      renderAddDetail(d, skills, providers);
      return;
    }
    if (p.provider === "lmstudio" || p.provider === "ollama") {
      renderServerDetail(d, skills, providers, p);
    } else {
      renderCustomDetail(d, skills, providers, p);
    }
  }

  function renderContent(parent) {
    const skills = (core.state.snapshot && core.state.snapshot.skills) || {};
    const providers = Array.isArray(skills.modelProviders) ? [...skills.modelProviders] : [];
    if (!selectedSource) selectedSource = skills.defaultProvider || "local";
    // A stale selection (provider removed on disk) falls back in renderDetail.

    renderStateRow(parent, skills, providers);

    const pane = el("div", { className: "prov-pane" });
    const list = el("div", { className: "prov-list" });
    const detail = el("div", { className: "section-rows prov-detail" });

    renderList(list, skills, providers);
    pane.appendChild(list);
    pane.appendChild(detail);
    parent.appendChild(pane);

    renderDetail(detail, skills, providers);
  }

  let _parent = null;
  function renderAll() {
    if (!_parent) return;
    _parent.innerHTML = "";
    renderHeader(_parent);
    renderContent(_parent);
  }

  async function render(parent) {
    _parent = parent;
    renderAll();
  }

  function init(coreArg) {
    core = coreArg; helpers = core.helpers; ops = core.ops;
    core.tabs.providers = { render: (parent) => { void render(parent); } };
  }

  root.ClawdSettingsTabProviders = { init };
})(typeof globalThis !== "undefined" ? globalThis : window);
