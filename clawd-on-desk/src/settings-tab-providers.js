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
      // Sync to providers.json and hot-reload the gateway's provider
      // registry (the main process does the reload after writing). NO
      // sidecar restart here: a restart taskkills the process tree and
      // guillotines any chat in flight — the "AI 直接挂了" bug.
      if (window.petSettings && typeof window.petSettings.saveProvidersConfig === "function") {
        await window.petSettings.saveProvidersConfig(providers);
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

  function isLocalId(id) {
    return id === "local" || id === "lmstudio" || id === "ollama";
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
    textCol.appendChild(el("h1", {}, t("sidebarProviders")));
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
        onClick: () => {
          _sourceMode = opt;
          // A selection carries over across modes and shows a local
          // server's detail under the cloud tab (and vice versa). Reset
          // it; renderDetail picks a mode-appropriate default.
          selectedSource = null;
          renderAll();
        },
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

  function sourceItem({ id, p, selected, inUse, onSelect, onRemove }) {
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
    if (onRemove) {
      const del = el("span", {
        className: "prov-del",
        title: t("provRemove"),
        onClick: (ev) => {
          ev.stopPropagation();
          if (inUse) { toast(t("provDelBlocked")); return; }
          onRemove();
        },
      }, "\u2715");
      item.appendChild(del);
    }
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

    // Which half is the user looking at? ONE source of truth (matching
    // the segmented control above): user choice, else cloud. Deriving it
    // from the active provider made the button say "cloud" while the
    // body rendered local — visibly contradictory.
    const mode = _sourceMode || "cloud";

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
          onRemove: async () => {
            await saveProviders(providers.filter((x) => x.provider !== p.provider));
            if (selectedSource === p.provider) selectedSource = defaultProvider === p.provider ? "local" : selectedSource;
            renderAll();
          },
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
        onRemove: async () => {
          await saveProviders(providers.filter((x) => x.provider !== p.provider));
          renderAll();
        },
      }));
    }
    // The add button belongs to THIS group (the built-in engine cannot be
    // "added"), so it sits here — not stranded below the engine entry.
    listEl.appendChild(softBtn("+ " + t("provAddProvider"), () => {
      selectedSource = "__add__";
      renderAll();
    }, { accent: selectedSource === "__add__" })).classList.add("prov-add-btn");
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

  // ── CherryStudio-style model discovery ────────────────────────────────
  // One factory for every form that has a model list. Replaces the native
  // <datalist>, which silently filters candidates by the input's current
  // text — a placeholder like "local-model" matches none of the discovered
  // ids and the dropdown reads as dead. The custom menu lists everything
  // the endpoint serves; onPick(id) selects one, the footer adds them all.
  function mountModelDiscovery(d, { urlInp, getKey, onPick, onAddAll }) {
    const menu = el("div", { className: "prov-model-menu" });
    menu.hidden = true;
    d.appendChild(menu);

    const onDocClick = (ev) => {
      if (!menu.isConnected) { document.removeEventListener("click", onDocClick); return; }
      if (!menu.hidden && !menu.contains(ev.target)) menu.hidden = true;
    };
    document.addEventListener("click", onDocClick);

    return async () => {
      try {
        const res = await window.settingsAPI.discoverModels({
          baseUrl: urlInp.value.trim(),
          apiKey: (getKey ? getKey() : "").trim(),
        });
        if (res && res.ok && Array.isArray(res.models) && res.models.length) {
          menu.innerHTML = "";
          for (const m of res.models) {
            const item = el("button", { type: "button", className: "prov-model-item" }, m);
            item.addEventListener("click", () => {
              menu.hidden = true;
              if (onPick) onPick(m);
            });
            menu.appendChild(item);
          }
          if (onAddAll) {
            const all = el("button", { type: "button", className: "prov-model-item prov-model-addall" }, t("provModelsAddAll"));
            all.addEventListener("click", () => {
              menu.hidden = true;
              onAddAll(res.models);
            });
            menu.appendChild(all);
          }
          menu.hidden = false;
          toast(String(t("provFetchOk")).replace("{n}", String(res.models.length)));
        } else {
          const raw = (res && res.error) || "";
          const target = urlInp.value.trim();
          if (/connect/i.test(raw) && target) {
            // Refused/failed connection to the endpoint itself — the local
            // server is probably not running (LM Studio closed, Ollama
            // stopped, wrong URL). Name the target: "All connection
            // attempts failed" alone reads as a mystery.
            toast(`${t("provFetchOffline")} · ${target}`, true);
          } else {
            toast(raw || t("provFetchFail"), true);
          }
        }
      } catch (err) {
        toast(String(err), true);
      }
    };
  }

  // ── Built-in engine: which .gguf, loaded from where ────────────────────
  //
  // The switcher and the scanned-folders manager used to sit on the Brain
  // page under its 模型 heading. They are local-source settings — the Brain
  // page only reports which brain is live — so they live here, above the
  // engine that reads them.
  function formatModelSize(bytes) {
    if (!Number.isFinite(bytes) || bytes <= 0) return "";
    if (bytes >= 1024 * 1024 * 1024) return (bytes / 1073741824).toFixed(2) + " GB";
    return (bytes / 1048576).toFixed(0) + " MB";
  }

  function localModelBlock() {
    const box = el("div", { className: "prov-local-model" });

    const title = el("div", { className: "prov-local-model-title" }, t("petRowCurrentModel"));
    box.appendChild(title);

    const select = el("select", { className: "prov-input prov-model-select" });
    select.appendChild(el("option", { value: "" }, t("petModelPathUnset")));
    select.disabled = true;
    box.appendChild(select);

    const fill = async () => {
      if (!window.petSettings
        || typeof window.petSettings.listLocalModels !== "function") return;
      let ret = null;
      try { ret = await window.petSettings.listLocalModels(); } catch { return; }
      const models = (ret && ret.models) || [];
      // A repaint can land after the pane was replaced by a re-render.
      if (!select.isConnected) return;
      select.innerHTML = "";
      if (models.length === 0) {
        select.appendChild(el("option", { value: "" }, t("petModelPathUnset")));
        select.disabled = true;
        return;
      }
      for (const m of models) {
        if (!m || !m.path) continue;
        const size = formatModelSize(m.sizeBytes);
        const opt = el("option", { value: m.path },
          `${m.label || m.path}${size ? " · " + size : ""}`);
        if (m.current) opt.selected = true;
        select.appendChild(opt);
      }
      select.disabled = false;
    };

    select.addEventListener("change", async () => {
      const path = select.value;
      if (!path) return;
      select.disabled = true;
      try {
        const ret = await window.petSettings.useModelDir(path);
        if (ret && ret.reloadError) toast(t("petReloadError") + ret.reloadError, true);
        else if (ret && !ret.ok) toast(t("petReloadError") + (ret.error || ""), true);
      } catch (err) {
        toast(t("petReloadError") + ((err && err.message) || err), true);
      } finally {
        select.disabled = false;
        void fill();
      }
    });
    void fill();

    const foldersEl = el("div", { className: "pet-model-folders-list" });
    const paintFolders = (folders) => {
      foldersEl.innerHTML = "";
      if (!folders || folders.length === 0) {
        foldersEl.appendChild(el("div", { className: "pet-model-folder-empty" },
          t("petModelsFolderEmpty")));
        return;
      }
      for (const f of folders) {
        const row = el("div", { className: "pet-model-folder-item" });
        row.appendChild(el("span", { className: "pet-model-folder-path", title: f }, f));
        const rm = el("button", {
          type: "button",
          className: "soft-btn pet-model-folder-remove",
          onClick: async () => {
            try {
              await window.petSettings.removeModelFolder(f);
              const cur = await window.petSettings.listModelFolders();
              paintFolders((cur && cur.folders) || []);
            } catch {}
          },
        }, t("petModelsFolderRemove"));
        row.appendChild(rm);
        foldersEl.appendChild(row);
      }
    };

    const refreshFolders = async () => {
      try {
        const ret = await window.petSettings.listModelFolders();
        paintFolders((ret && ret.folders) || []);
      } catch { paintFolders([]); }
    };

    const addFolder = async () => {
      try {
        const ret = await window.petSettings.addModelFolder();
        if (!ret || ret.canceled) return;
        if (!ret.ok) { toast(t("petModelsFolderAddFailed") + (ret.error || ""), true); return; }
        paintFolders(ret.folders || []);
      } catch {}
    };
    const addFile = async () => {
      try {
        const ret = await window.petSettings.addModelFile();
        if (!ret || ret.canceled) return;
        if (!ret.ok) { toast(t("petModelsFolderAddFailed") + (ret.error || ""), true); return; }
        paintFolders(ret.folders || []);
        if (ret.reloadError) toast(t("petReloadError") + ret.reloadError, true);
        void fill();
      } catch {}
    };

    const actions = el("div", { className: "prov-actions" });
    // Browse registers the picked file's folder in the scan list AND
    // hot-loads that .gguf (mmproj sibling auto-detected).
    actions.appendChild(softBtn(t("petChangeModel"), async () => {
      let ret = null;
      try {
        ret = await window.petSettings.pickModelDir();
      } catch (err) {
        toast(t("petReloadError") + ((err && err.message) || err), true);
      }
      if (ret && ret.ok) {
        if (ret.reloadError) toast(t("petReloadError") + ret.reloadError, true);
        renderAll();
        return;
      }
      if (ret && !ret.canceled && ret.error) toast(ret.error, true);
    }, { accent: true }));
    actions.appendChild(softBtn("+ " + t("petModelsFolderAdd"), addFolder));
    actions.appendChild(softBtn("+ " + t("petModelsFileAdd"), addFile));
    box.appendChild(actions);

    box.appendChild(el("div", { className: "prov-local-model-title prov-local-folders-title" },
      t("petModelsFolderLabel")));
    box.appendChild(foldersEl);
    if (typeof window.petSettings?.listModelFolders === "function") void refreshFolders();
    return box;
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

    // Choosing WHICH file the built-in engine loads, and which folders it
    // may load from, happens right here — the Brain page only reports the
    // result, it is not the place to change it.
    if (window.petSettings) d.appendChild(localModelBlock());

    // The llama.cpp engine lives here rather than on the Brain page: it is
    // part of "run the model on this machine". The cards themselves are
    // owned by the Brain tab module (its health poll feeds them) and
    // adopted here — 推理引擎 then 高级设置, in that order.
    if (window.petSettings && core && core.enginePanel
      && typeof core.enginePanel.mount === "function") {
      const engineHost = el("div", { className: "prov-engine-host" });
      d.appendChild(engineHost);
      core.enginePanel.mount(engineHost);
    }
  }

  // ── Provider model list (CherryStudio-style) ──────────────────────────
  // A provider serves ONE active model to the gateway (`entry.model`,
  // unchanged contract) but the user manages a LIST of them: enable /
  // disable, per-model context window / max output / capabilities, add
  // from discovery, delete. Legacy entries with a single `model` string
  // lazily grow the list on first render.
  function providerModels(p) {
    const list = Array.isArray(p.models)
      ? p.models.filter((m) => m && m.id)
      : [];
    if (!list.length && p.model) {
      list.push({
        id: p.model,
        enabled: true,
        contextWindow: p.contextWindow || null,
        maxOutput: null,
        capabilities: { tools: true, images: false, thinking: !!p.thinking },
      });
    }
    return list;
  }

  function renderModelManager(d, { p, providers, skills, urlInp, getKey }) {
    const id = p.provider;
    const persist = async (models, activeId) => {
      const updated = { ...p, models, model: activeId != null ? activeId : p.model };
      await saveProviders(providers.map((x) => x.provider === id ? updated : x));
      toast(t("provSaved"));
      renderAll();
    };

    const header = el("div", { className: "prov-models-head" });
    header.appendChild(el("span", { className: "section-title prov-models-title" }, t("provModelsTitle")));
    header.appendChild(softBtn(t("provFetchModels"), () => fetchModels()));
    d.appendChild(header);

    const listWrap = el("div", { className: "prov-model-list" });
    d.appendChild(listWrap);

    const drawList = () => {
      const models = providerModels(p);
      listWrap.innerHTML = "";
      if (!models.length) {
        listWrap.appendChild(el("div", { className: "prov-subgroup-empty" }, t("provModelsEmpty")));
      }
      for (const m of models) {
        const active = p.model === m.id;
        const row = el("div", { className: "prov-model-row" + (active ? " is-active" : "") });

        const pick = el("button", {
          type: "button",
          className: "prov-model-pick",
          title: t("provModelsPickHint"),
          onClick: async () => {
            if (active) return;
            await persist(models.map((x) => x.id === m.id ? { ...x, enabled: true } : x), m.id);
          },
        });
        const pickText = el("span", { className: "prov-model-id" }, m.id);
        pick.appendChild(pickText);
        if (active) pick.appendChild(el("span", { className: "prov-tag" }, t("provInUse")));
        else if (m.enabled === false) pick.appendChild(el("span", { className: "prov-model-disabled" }, t("provModelsOff")));
        row.appendChild(pick);

        if (m.contextWindow) {
          row.appendChild(el("span", { className: "prov-model-ctx" }, fmtTokens(m.contextWindow)));
        }

        const sw = switchEl(m.enabled !== false, () => {});
        sw.addEventListener("click", async () => {
          // switchEl's own click handler toggled the class first, so the
          // class now holds the DESIRED state, not the current one.
          const desired = sw.classList.contains("on");
          const modelsNow = providerModels(p);
          if (!desired && active) {
            const nextEnabled = modelsNow.find((x) => x.id !== m.id && x.enabled !== false);
            if (!nextEnabled) { toast(t("provModelsLast"), true); renderAll(); return; }
            await persist(modelsNow.map((x) => x.id === m.id ? { ...x, enabled: false } : x), nextEnabled.id);
            return;
          }
          await persist(modelsNow.map((x) => x.id === m.id ? { ...x, enabled: desired } : x));
        });
        const swWrap = el("div", { className: "row-control" });
        swWrap.appendChild(sw);
        row.appendChild(swWrap);

        row.appendChild(el("button", {
          type: "button", className: "prov-model-btn", title: t("provModelsEdit"),
          onClick: () => drawEditRow(m, row),
        }, "✎"));

        row.appendChild(el("button", {
          type: "button", className: "prov-model-btn prov-model-del", title: t("provRemove"),
          onClick: async () => {
            const modelsNow = providerModels(p);
            if (modelsNow.length <= 1) { toast(t("provModelsLast"), true); return; }
            const wasActive = p.model === m.id;
            const rest = modelsNow.filter((x) => x.id !== m.id);
            const nextActive = wasActive
              ? (rest.find((x) => x.enabled !== false) || rest[0]).id
              : p.model;
            await persist(rest, nextActive);
          },
        }, "✕"));

        listWrap.appendChild(row);
      }
    };

    const fmtTokens = (n) => {
      const v = Number(n);
      if (!v || v <= 0) return "";
      return v >= 1000 ? `${Math.round(v / 1024) || Math.round(v / 1000)}K` : String(v);
    };

    // Inline editor: one row swaps into inputs for id / context window /
    // max output / capabilities. Save keeps the gateway contract coherent:
    // renaming the ACTIVE id also rewrites p.model.
    const drawEditRow = (m, row) => {
      row.innerHTML = "";
      row.classList.add("is-editing");
      const models = providerModels(p);
      const idInp = textInput(m.id, { placeholder: t("provModelsIdPh") });
      const ctxInp = textInput(m.contextWindow, { placeholder: t("provModelsCtxPh") });
      const outInp = textInput(m.maxOutput, { placeholder: t("provMaxOutput") });
      row.appendChild(idInp);
      row.appendChild(ctxInp);
      row.appendChild(outInp);

      const caps = m.capabilities || {};
      const cap = (label, checked) => {
        const wrap = el("label", { className: "prov-cap" });
        const box = el("input", { type: "checkbox" });
        box.checked = !!checked;
        wrap.appendChild(box);
        wrap.appendChild(el("span", {}, label));
        return { wrap, box };
      };
      const cTools = cap(t("provCapTools"), caps.tools !== false);
      const cImages = cap(t("provCapImages"), caps.images);
      const cThink = cap(t("provCapThinking"), caps.thinking);
      const capWrap = el("div", { className: "prov-caps" });
      capWrap.appendChild(cTools.wrap); capWrap.appendChild(cImages.wrap); capWrap.appendChild(cThink.wrap);
      row.appendChild(capWrap);

      const ops = el("div", { className: "prov-model-editops" });
      ops.appendChild(softBtn(t("provSave"), async () => {
        const newId = (idInp.value || "").trim();
        if (!newId) { toast(t("provModelsIdPh"), true); return; }
        if (newId !== m.id && models.some((x) => x.id === newId)) { toast(t("provModelsDup"), true); return; }
        const next = models.map((x) => x.id === m.id ? {
          ...x,
          id: newId,
          contextWindow: Number(ctxInp.value) || null,
          maxOutput: Number(outInp.value) || null,
          capabilities: { tools: cTools.box.checked, images: cImages.box.checked, thinking: cThink.box.checked },
        } : x);
        await persist(next, p.model === m.id ? newId : null);
      }, { accent: true }));
      ops.appendChild(softBtn(t("provCancel"), () => renderAll()));
      row.appendChild(ops);
    };

    // Compact add row — always visible under the list.
    let addRow = null;
    const drawAddRow = () => {
      if (addRow) addRow.remove();
      addRow = el("div", { className: "prov-model-add" });
      const idInp = textInput("", { placeholder: t("provModelsIdPh") });
      const ctxInp = textInput("", { placeholder: t("provModelsCtxPh") });
      addRow.appendChild(idInp);
      addRow.appendChild(ctxInp);
      addRow.appendChild(softBtn("+ " + t("provModelsAdd"), async () => {
        const nid = (idInp.value || "").trim();
        if (!nid) { toast(t("provModelsIdPh"), true); return; }
        const models = providerModels(p);
        if (models.some((x) => x.id === nid)) { toast(t("provModelsDup"), true); return; }
        models.push({
          id: nid,
          enabled: true,
          contextWindow: Number(ctxInp.value) || null,
          maxOutput: null,
          capabilities: { tools: true, images: false, thinking: false },
        });
        idInp.value = ""; ctxInp.value = "";
        await persist(models, p.model || nid);
      }));
      d.appendChild(addRow);
    };

    const fetchModels = mountModelDiscovery(d, {
      urlInp,
      getKey,
      onPick: async (picked) => {
        const models = providerModels(p);
        const known = models.some((x) => x.id === picked);
        const next = known
          ? models.map((x) => x.id === picked ? { ...x, enabled: true } : x)
          : [...models, { id: picked, enabled: true, contextWindow: null, maxOutput: null, capabilities: { tools: true, images: false, thinking: false } }];
        await persist(next, picked);
      },
      onAddAll: async (ids) => {
        const models = providerModels(p);
        const known = new Set(models.map((x) => x.id));
        for (const mid of ids) {
          if (!known.has(mid)) {
            models.push({ id: mid, enabled: false, contextWindow: null, maxOutput: null, capabilities: { tools: true, images: false, thinking: false } });
          }
        }
        await persist(models, p.model);
      },
    });

    drawList();
    drawAddRow();
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
    d.appendChild(fieldRow(t("provFieldUrl"), urlInp));

    // Model list (CherryStudio-style): pick / enable / edit / add, with
    // discovery feeding both single pick and "add all".
    renderModelManager(d, {
      p, providers, skills,
      urlInp,
      getKey: () => (p.apiKey || "").trim(),
    });

    const actions = el("div", { className: "prov-actions" });
    const use = useButton(skills.defaultProvider || "local", id);
    if (use) actions.appendChild(use);
    actions.appendChild(softBtn(t("provSave"), async () => {
      const updated = {
        ...p,
        baseUrl: urlInp.value.trim() || p.baseUrl,
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

    // API key with an eye toggle — keys are pasted blind otherwise.
    const keyInp = textInput(p.apiKey, { type: "password", placeholder: "sk-..." });
    const keyWrap = el("div", { className: "prov-secret" });
    keyWrap.appendChild(keyInp);
    const eye = el("button", { type: "button", className: "prov-model-btn", title: t("provKeyReveal") }, "👁");
    eye.addEventListener("click", () => {
      const hidden = keyInp.getAttribute("type") === "password";
      keyInp.setAttribute("type", hidden ? "text" : "password");
      eye.textContent = hidden ? "🙈" : "👁";
    });
    keyWrap.appendChild(eye);
    const keyField = el("div", { className: "prov-field" });
    keyField.appendChild(el("span", { className: "prov-field-label" }, t("provFieldKey")));
    keyField.appendChild(keyWrap);
    d.appendChild(keyField);

    const urlInp = textInput(p.baseUrl, { placeholder: "https://api.deepseek.com/v1" });
    d.appendChild(fieldRow(t("provFieldUrl"), urlInp));

    renderModelManager(d, {
      p, providers, skills,
      urlInp,
      getKey: () => keyInp.value.trim(),
    });

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
        thinking: thinkCb.classList.contains("on"),
        reasoningEffort: effortSel.value || null,
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
    d.appendChild(el("p", { className: "prov-detail-desc" },
      t(_sourceMode === "local" ? "provAddHint" : "provAddHintCloud")));

    // One preset-card factory for both halves of the page. It must live
    // ABOVE the mode branches: declared inside the local-only block, the
    // cloud grid's loop threw "mkPreset is not defined" and the whole add
    // pane died right after the 云厂商 heading — "click does nothing".
    const mkPreset = (name, desc, cfg) => {
      const card = el("button", { type: "button", className: "prov-preset-card" });
      card.appendChild(el("span", { className: "prov-preset-name" }, tile(tileFor(cfg.provider)), name));
      if (desc) card.appendChild(el("span", { className: "prov-preset-desc" }, desc));
      card.addEventListener("click", async () => {
        const next = providers.filter((x) => x.provider !== cfg.provider);
        next.push(cfg);
        await saveProviders(next);
        selectedSource = cfg.provider;
        renderAll();
      });
      return card;
    };

    // Local presets — LM Studio / Ollama. LOCAL MODE ONLY: showing them
    // inside the cloud half was exactly the "why is lmstudio here" bug.
    if (_sourceMode === "local") {
      const grid = el("div", { className: "prov-preset-grid" });
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
    }

    // Cloud provider presets — shown in cloud mode only. 国产优先,
    // then global aggregators. One click creates the provider with the
    // baseUrl and a sensible default model; the user only adds a key.
    if (_sourceMode !== "local") {
      d.appendChild(el("div", { className: "section-title prov-manual-title" }, t("provCloudPresets")));
      const cgrid = el("div", { className: "prov-preset-grid" });
      // NO hardcoded default models and NO model-list marketing copy on
      // the cards: I invented them from stale memory and the captain
      // rightly called it out (2026). The baseUrl is the only stable
      // fact; the real model ids come from 获取模型列表 after the key
      // is in.
      const CLOUD = [
        ["deepseek", "DeepSeek", "https://api.deepseek.com/v1"],
        ["kimi", "Kimi", "https://api.moonshot.cn/v1"],
        ["zhipu", "智谱GLM", "https://open.bigmodel.cn/api/paas/v4"],
        ["qwen", "通义千问", "https://dashscope.aliyuncs.com/compatible-mode/v1"],
        ["minimax", "MiniMax", "https://api.minimax.chat/v1"],
        ["siliconflow", "硅基流动", "https://api.siliconflow.cn/v1"],
        ["volces", "火山方舟", "https://ark.cn-beijing.volces.com/api/v3"],
        ["hunyuan", "腾讯混元", "https://api.hunyuan.cloud.tencent.com/v1"],
        ["wenxin", "百度文心", "https://qianfan.baidubce.com/v2"],
        ["openai", "OpenAI", "https://api.openai.com/v1"],
        ["anthropic", "Anthropic", "https://api.anthropic.com/v1"],
        ["gemini", "Gemini", "https://generativelanguage.googleapis.com/v1beta"],
        ["groq", "Groq", "https://api.groq.com/openai/v1"],
        ["openrouter", "OpenRouter", "https://openrouter.ai/api/v1"],
      ];
      for (const [pid, name, url] of CLOUD) {
        cgrid.appendChild(mkPreset(name, "", {
          provider: pid, apiKey: "", baseUrl: url, model: "",
          thinking: false, reasoningEffort: null, contextWindow: null,
        }));
      }
      d.appendChild(cgrid);
    }

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
    if (!selectedSource) {
      // Mode-appropriate default: never open a local server's detail under
      // the cloud tab (or the reverse) just because it happens to be the
      // active provider.
      const mode = _sourceMode || "cloud";
      selectedSource = mode === "cloud"
        ? (isLocalId(defaultProvider) ? "local" : defaultProvider)
        : (isLocalId(defaultProvider) ? defaultProvider : "local");
    }

    if (selectedSource === "__add__") {
      renderAddDetail(d, skills, providers);
      return;
    }
    if (selectedSource === "local") {
      if ((_sourceMode || "cloud") === "cloud") {
        // Cloud mode talks ONLY about cloud. The built-in engine has its
        // own home under Local models - and per the captain, even
        // MENTIONING it here is wrong. So: a neutral cloud hint, nothing
        // about local, no pointers, no engine talk.
        const card = el("div", { className: "section" });
        card.appendChild(el("p", { className: "prov-detail-desc" },
          t("provCloudPickHint")));
        d.appendChild(card);
        return;
      }
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
    if (!selectedSource) {
      const mode = _sourceMode || "cloud";
      selectedSource = mode === "cloud"
        ? (isLocalId(skills.defaultProvider || "local") ? "local" : (skills.defaultProvider || "local"))
        : (isLocalId(skills.defaultProvider || "local") ? (skills.defaultProvider || "local") : "local");
    }
    // A stale selection (provider removed on disk) falls back in renderDetail.

    // renderStateRow (the "现在用它说话" card) was removed on purpose:
    // "which model is in use" is the Brain page's job, and saying it here
    // too meant two pages asserting the same fact - they WILL drift apart.

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
