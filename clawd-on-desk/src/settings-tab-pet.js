"use strict";

// ── DeskPet settings tab ──
//
// Page layout (top → bottom):
//   • Page header: title + subtitle on the left, sidecar status pill on the right
//   • 模型 / Model               — hero card: the model in use (name, path,
//                                  size, backend) with 在文件夹中显示 / 更换
//                                — collapsed "scanned folders" disclosure
//                                  (auto-expands when no model is found)
//   • 推理引擎 / Engine           — runtime switch (start/stop llama-server),
//                                  version row with check / update / install
//                                  from folder + inline progress + slow-network
//                                  link, backend segmented control (Windows)
//   • 高级 / Advanced (collapsed) — install location, restart Sidecar, open logs
//
// The redundant model dropdown is gone: the hero's 更换 button already opens
// the picker, and a second list of the same files below it only asked
// "why are there two places to switch models?".
//
// 2026-09 redesign: the old page stacked ~11 flat rows and 8 buttons; the
// path row duplicated the picker, the folder manager was always expanded,
// and the engine block mixed runtime/version/backend/download hints into
// one wall of controls. Every capability is kept, but each card now has a
// single primary action and low-frequency tools live behind disclosures.
//
// Sidecar health is polled at most once a minute (5s during cold-start
// grace). Ticks re-render only the header pill and cards the user is NOT
// interacting with, so an open dropdown or an in-flight click survives.

(function initSettingsTabDeskPet(root) {
  let core = null;
  let helpers = null;
  let ops = null;

  // Electron's window.notifyError() blocks the MAIN process until dismissed —
  // while it's open every pet/bubble/settings window ghosts ("未响应",
  // Windows Application Hang) and the sidecar pipe freezes. Never alert;
  // toast instead.
  function notifyError(message) {
    if (ops && typeof ops.showToast === "function") {
      ops.showToast(message, { error: true, ttl: 8000 });
    }
  }

  let healthTimer = null;
  let visibilityHandler = null;
  let mounted = false;
  // Survive re-renders within the same Settings session so the user
  // doesn't have to re-expand a disclosure every time they revisit the tab.
  let advancedExpanded = false;


  const HEALTH_INTERVAL_MS_SLOW = 60_000;
  const HEALTH_INTERVAL_MS_FAST = 5_000;
  const HEALTH_FAST_ATTEMPTS = 6;
  const NAVIGATOR_PLATFORM = typeof navigator !== "undefined" ? (navigator.platform || "") : "";
  const IS_WINDOWS = /Win/i.test(NAVIGATOR_PLATFORM);

  function t(key) {
    return helpers.t(key);
  }

  // ── Inline SVGs ────────────────────────────────────────────────────────
  const SVG_RESTART =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" width="100%" height="100%">' +
    '<path d="M12 4v8"/>' +
    '<path d="M16.24 7.76a6 6 0 1 1-8.49 0"/>' +
    '</svg>';
  const SVG_LOG =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" width="100%" height="100%">' +
    '<rect x="3" y="4" width="18" height="16" rx="2"/>' +
    '<path d="M7 9l3 3-3 3"/>' +
    '<path d="M13 15h5"/>' +
    '</svg>';
  const SVG_CHEVRON =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="100%" height="100%">' +
    '<path d="M9 6l6 6-6 6"/>' +
    '</svg>';
  const SVG_CHIP =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" width="100%" height="100%">' +
    '<rect x="6" y="6" width="12" height="12" rx="2"/>' +
    '<rect x="10" y="10" width="4" height="4" rx="1"/>' +
    '<path d="M9 2.5V6M15 2.5V6M9 18v3.5M15 18v3.5M2.5 9H6M2.5 15H6M18 9h3.5M18 15h3.5"/>' +
    '</svg>';

  function cleanupTimers() {
    if (healthTimer) {
      clearTimeout(healthTimer);
      healthTimer = null;
    }
    if (visibilityHandler) {
      document.removeEventListener("visibilitychange", visibilityHandler);
      visibilityHandler = null;
    }
    mounted = false;
  }

  function el(tag, attrs, ...children) {
    const e = document.createElement(tag);
    for (const k of Object.keys(attrs || {})) {
      if (k === "style") Object.assign(e.style, attrs[k]);
      else if (k === "className") e.className = attrs[k];
      else if (k.startsWith("on") && typeof attrs[k] === "function") {
        e.addEventListener(k.slice(2).toLowerCase(), attrs[k]);
      } else e.setAttribute(k, attrs[k]);
    }
    for (const child of children) {
      if (child == null) continue;
      e.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
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

  // ── Status pill (top-right of page header) ────────────────────────────
  //
  // Three states. We deliberately collapse the original "probing" and
  // "starting" labels into a single yellow "starting" pill: at the page
  // level the user only cares whether things are healthy, warming up, or
  // broken. The full debug breakdown lives in the logs.
  function deriveStatus(sidecarReady, llamaReady, probing, engineRunning, isProvider) {
    // Provider mode (LM Studio / Ollama / cloud): the llama-server states
    // are irrelevant — the brain channel is up iff the sidecar is.
    if (isProvider) {
      if (sidecarReady) return { tone: "ready", label: t("petStatusRunning") };
      return probing
        ? { tone: "starting", label: t("petStatusStarting") }
        : { tone: "offline", label: t("petStatusError") };
    }
    if (sidecarReady && llamaReady) return { tone: "ready", label: t("petStatusRunning") };
    if (!sidecarReady) {
      return probing
        ? { tone: "starting", label: t("petStatusStarting") }
        : { tone: "offline", label: t("petStatusError") };
    }
    // The sidecar answers even when nobody wants a local model. Claiming
    // "启动中" in that case never resolves — an engine the user switched
    // off is idle, not slow. Only say "starting" when a process is really
    // coming up, or we are still probing for the first health answer.
    if (engineRunning || probing) return { tone: "starting", label: t("petStatusStarting") };
    return { tone: "idle", label: t("petStatusIdle") };
  }

  function statusPill(tone, label) {
    const cls = tone === "ready"
      ? "remote-ssh-status-connected"
      : tone === "starting"
        ? "remote-ssh-status-connecting"
        : tone === "offline"
          ? "remote-ssh-status-failed"
          : "remote-ssh-status-idle";
    return el("span", { className: `remote-ssh-status-badge ${cls}` }, label);
  }

  // ── Switch row (committed-vs-pending; rolls back on IPC failure) ──────
  // `opts.failureMessage` replaces the generic "保存失败" prefix when the
  // row drives something that is not a preference save (e.g. engine start).
  function switchRow(label, hint, checked, onChange, opts = {}) {
    const row = el("div", { className: "row" });
    const text = el("div", { className: "row-text" });
    text.appendChild(el("span", { className: "row-label" }, label));
    if (hint) text.appendChild(el("span", { className: "row-desc" }, hint));
    row.appendChild(text);
    const sw = el("div", {
      className: "switch" + (checked ? " on" : ""),
      role: "switch",
      tabindex: "0",
      "aria-checked": checked ? "true" : "false",
    });

    let committedOn = !!checked;
    let pending = false;

    function applyVisual(on, isPending) {
      sw.classList.toggle("on", !!on);
      sw.classList.toggle("pending", !!isPending);
      sw.setAttribute("aria-checked", on ? "true" : "false");
    }

    function isOk(result) {
      if (!result) return false;
      if (result.ok === true) return true;
      if (result.status === "ok") return true;
      return false;
    }

    function notifyFailure(message) {
      const prefix = opts.failureMessage || t("toastSaveFailed");
      if (ops && typeof ops.showToast === "function") {
        ops.showToast(prefix + (message || "unknown error"), { error: true });
      }
    }

    async function runToggle() {
      if (pending) return;
      const next = !committedOn;
      pending = true;
      applyVisual(next, true);
      let ok = false;
      let message = "";
      try {
        const result = await onChange(next);
        ok = isOk(result);
        if (!ok) message = (result && (result.error || result.message)) || "";
      } catch (err) {
        ok = false;
        message = (err && err.message) || "";
      } finally {
        pending = false;
      }
      if (ok) {
        committedOn = next;
        applyVisual(next, false);
      } else {
        applyVisual(committedOn, false);
        notifyFailure(message);
      }
    }

    sw.addEventListener("click", () => { void runToggle(); });
    sw.addEventListener("keydown", (ev) => {
      if (ev.key === " " || ev.key === "Enter") {
        ev.preventDefault();
        void runToggle();
      }
    });
    const ctl = el("div", { className: "row-control" });
    ctl.appendChild(sw);
    row.appendChild(ctl);
    return row;
  }

  // ── Section header (matches the small-caps title used elsewhere) ──────
  function sectionTitle(text) {
    return el("h2", { className: "section-title pet-section-title" }, text);
  }

  function deviceLabel(device) {
    if (device === "vulkan") return t("petBackendVulkan");
    if (device === "cuda") return t("petBackendCuda");
    if (device === "metal") return t("petBackendMetal");
    return t("petBackendCpu");
  }

  // Which brain answers the pet when the built-in engine is not the
  // source: the settings snapshot carries the active provider id. The card
  // is about the brain, not about a local .gguf, so a cloud / external
  // server has to be nameable here too.
  function activeBrainSource() {
    const skills = (core && core.state && core.state.snapshot
      && core.state.snapshot.skills) || {};
    const id = skills.defaultProvider || "local";
    if (id === "local") return { name: "", detail: "", isProvider: false, modelId: "", baseUrl: "" };
    const p = (skills.modelProviders || []).find((x) => x && x.provider === id) || {};
    const name = id === "lmstudio" ? "LM Studio" : id === "ollama" ? "Ollama" : id;
    const modelId = p.model || "";
    const where = modelId || (p.baseUrl ? String(p.baseUrl).replace(/^https?:\/\//, "") : "");
    return {
      name,
      detail: [name, where].filter(Boolean).join(" · "),
      isProvider: true,
      modelId,
      baseUrl: p.baseUrl || "",
    };
  }

  // True when the user's focus (e.g. an open <select>) lives inside the
  // box — the automatic health tick must not rebuild it mid-interaction.
  function boxInteractionBusy(box) {
    const active = typeof document !== "undefined" ? document.activeElement : null;
    return !!(active && box && box.contains(active));
  }

  // ── Header (title + subtitle on left, status pill on right) ───────────
  function renderHeader(ctx) {
    ctx.headerBox.innerHTML = "";
    const wrap = el("div", { className: "pet-page-header" });
    const textCol = el("div", { className: "pet-page-header-text" });
    textCol.appendChild(el("h1", {}, t("petTitle")));
    textCol.appendChild(el("p", { className: "subtitle" }, t("petSubtitle")));
    wrap.appendChild(textCol);
    ctx.statusPillSlot = el("div", { className: "pet-page-header-status" });
    wrap.appendChild(ctx.statusPillSlot);
    ctx.headerBox.appendChild(wrap);
    syncStatusPill(ctx);
  }

  function syncStatusPill(ctx) {
    if (!ctx.statusPillSlot) return;
    const { sidecarReady, llamaReady, probing, engineRunning } = ctx.healthSnapshot;
    const { tone, label } = deriveStatus(
      sidecarReady, llamaReady, probing, engineRunning,
      !!activeBrainSource().isProvider,
    );
    ctx.statusPillSlot.innerHTML = "";
    ctx.statusPillSlot.appendChild(statusPill(tone, label));
  }

  // ── Health probe → updates ctx.healthSnapshot ─────────────────────────
  async function probeHealth(ctx) {
    let st = null;
    try { st = await window.petSettings.getStatus(); } catch {}
    const h = (st && st.health) || {};
    const sidecarReady = !!(st && (st.sidecarReady || (st.health && st.health.ok) || st.healthy));
    const llamaReady = sidecarReady
      && (h.alive === true || !!(h.llama_server && h.llama_server.status === "ok"));
    if (sidecarReady) ctx.everHealthy = true;
    const probing = !sidecarReady && !ctx.everHealthy && ctx.fastAttemptsLeft > 0;

    // Whether a llama-server process actually exists. The health payload
    // answers "is the gateway up", which is a different question — without
    // this the pill could never leave 启动中.
    let engineRunning = false;
    if (sidecarReady && typeof window.petSettings.engineParams === "function") {
      try {
        const ep = await window.petSettings.engineParams();
        engineRunning = !!(ep && ep.running);
      } catch {}
    }

    const modelNameNow = h.model_name
      || (h.model_dir ? h.model_dir.split(/[/\\]/).pop() : null);
    if (modelNameNow) {
      ctx.lastModelName = modelNameNow;
      ctx.lastModelDir = h.model_dir || ctx.lastModelDir;
    }

    ctx.healthSnapshot = {
      st, h, sidecarReady, llamaReady, probing, engineRunning,
      modelName: modelNameNow
        || ((probing || sidecarReady) ? ctx.lastModelName : null),
      modelDir: h.model_dir || ctx.lastModelDir,
    };
    return ctx.healthSnapshot;
  }

  // ── Sections ──────────────────────────────────────────────────────────

// Engine-update check result cache: { at, text, hasUpdate }. Reused
  // across health-tick rebuilds so the version line doesn't flicker.
  let engineCheckCache = null;
  // While an engine update is running, the health tick must NOT rebuild
  // the section — that would destroy the live progress bar mid-download
  // and reset the buttons (leading to a spurious "另一个更新正在进行").
  let engineUpdating = false;

  function formatSize(bytes) {
    if (!Number.isFinite(bytes) || bytes <= 0) return "";
    if (bytes >= 1024 * 1024 * 1024) return (bytes / 1073741824).toFixed(2) + " GB";
    return (bytes / 1048576).toFixed(0) + " MB";
  }

  // ── Path helpers ───────────────────────────────────────────────────────
  //
  // The hero card shows the full path with CSS ellipsis and a restoring
  // tooltip (title attribute) — no character-level truncation needed, the
  // browser lays it out and the tooltip never hides information.
  function pickLabelFromPath(p) {
    if (!p) return t("petModelPathUnset");
    const m = String(p).match(/([^\\/]+?)(?:\.gguf)?$/i);
    return m ? m[1] : String(p);
  }

  function metaChip(label) {
    return el("span", { className: "pet-chip" }, label);
  }

  // ── Model section ─────────────────────────────────────────────────────
  //
  // A read-only hero card: "which brain is my pet running" at a glance —
  // model name, live status, where it comes from, and size / backend
  // chips. Everything that changes the brain (pick a model, scan folders,
  // run the engine) lives in 模型来源 → 本地模型.
  function renderModelSection(box, ctx) {
    box.innerHTML = "";
    const snap = ctx.healthSnapshot || {};
    const src = activeBrainSource();
    // Provider mode (LM Studio / Ollama / cloud): the brain is the
    // provider's ACTIVE model. health.model_dir is the BUILT-IN engine's
    // gguf path — showing it here claimed the pet was running a model it
    // had explicitly stopped using (the captain's stale-hero bug).
    const isProvider = !!src.isProvider;
    const modelDir = isProvider ? "" : (snap.modelDir || "");
    const hasPath = !!modelDir;

    box.appendChild(sectionTitle(t("petSectionModel")));

    // ── Hero card: which brain is running ─────────────────────────────
    // Read-only on purpose. Picking a model, opening its folder and
    // managing scan roots live in 模型来源 → 本地模型, next to the engine
    // that runs them; this card only answers "which brain is my pet on".
    const hero = el("div", { className: "section-rows pet-model-hero" });

    const heroMain = el("div", { className: "pet-model-hero-main" });
    const iconBox = el("span", { className: "pet-model-hero-icon", "aria-hidden": "true" });
    iconBox.innerHTML = SVG_CHIP;
    heroMain.appendChild(iconBox);

    const heroText = el("div", { className: "pet-model-hero-text" });
    const nameRow = el("div", { className: "pet-model-hero-name-row" });
    const heroName = el("span", { className: "pet-model-hero-name" },
      isProvider ? (src.modelId || src.name)
        : (hasPath ? pickLabelFromPath(modelDir) : (src.name || t("petModelPathUnset"))));
    nameRow.appendChild(heroName);
    // Live status beside the name. In provider mode the llama-server
    // states are meaningless — the channel is up iff the sidecar is.
    let heroStatus;
    if (isProvider) {
      heroStatus = snap.sidecarReady
        ? { tone: "ready", label: t("petStatusRunning") }
        : (snap.probing
          ? { tone: "starting", label: t("petStatusStarting") }
          : { tone: "offline", label: t("petStatusError") });
    } else {
      heroStatus = deriveStatus(snap.sidecarReady, snap.llamaReady, snap.probing, snap.engineRunning);
    }
    const heroTone = heroStatus.tone === "ready" ? "ok"
      : heroStatus.tone === "starting" ? "warn"
        : heroStatus.tone === "idle" ? "muted" : "bad";
    nameRow.appendChild(el("span", {
      className: `pet-model-hero-status tone-${heroTone}`,
    }, heroStatus.label));
    heroText.appendChild(nameRow);

    const heroPath = el("div", {
      className: "pet-model-hero-path" + (!isProvider && hasPath ? "" : " is-unset"),
      title: isProvider ? (src.baseUrl || src.detail) : (hasPath ? modelDir : src.detail),
    }, isProvider ? src.detail : (hasPath ? modelDir : (src.detail || t("petModelPathUnset"))));
    heroText.appendChild(heroPath);
    heroMain.appendChild(heroText);
    hero.appendChild(heroMain);

    // Meta chips (source / size / backend) — filled asynchronously; both
    // reads are cheap and cached by their IPC handlers. The isConnected
    // guard keeps a late response from touching a health-tick-replaced card.
    const chips = el("div", { className: "pet-model-hero-chips" });
    hero.appendChild(chips);
    if (src.name) chips.appendChild(metaChip(src.name));
    if (hasPath && typeof window.petSettings.listLocalModels === "function") {
      window.petSettings.listLocalModels().then((ret) => {
        const cur = ((ret && ret.models) || []).find((m) => m && m.current);
        const size = formatSize(cur && cur.sizeBytes);
        if (size && chips.isConnected) chips.appendChild(metaChip(size));
      }).catch(() => {});
    }
    if (typeof window.petSettings.listDevices === "function") {
      window.petSettings.listDevices().then((devices) => {
        const cur = (devices && devices.current) || "";
        if (cur && chips.isConnected) chips.appendChild(metaChip(deviceLabel(cur)));
      }).catch(() => {});
    }

    box.appendChild(hero);

    // The model list still earns its keep: while the engine is stopped the
    // health payload carries no model at all, and the hero then claimed
    // 未选择模型 even though a model IS configured. Fill it from the same
    // source the model picker reads.
    if (typeof window.petSettings.listLocalModels === "function") {
      window.petSettings.listLocalModels().then((ret) => {
        const cur = ((ret && ret.models) || []).find((m) => m && m.current);
        if (!cur || !cur.path) return;
        if (hasPath || src.name || !heroName.isConnected) return;
        heroName.textContent = cur.label || pickLabelFromPath(cur.path);
        heroPath.textContent = cur.path;
        heroPath.title = cur.path;
        heroPath.classList.remove("is-unset");
      }).catch(() => {});
    }
  }

  // ── Engine (llama.cpp binary) section ─────────────────────────────────
  // Three rows: runtime switch, version row (check / update / install from
  // folder + inline progress + slow-network link), backend segmented
  // control. The install location moved to Advanced — it is debug info.
  async function renderEngineSection(box, ctx) {
    // Never rebuild while an update is in flight — the health tick
    // calls this repeatedly and would tear down the live progress bar.
    if (engineUpdating && box.firstChild) return;
    // Must clear before painting — refreshAll + the health tick both call
    // this, and without the reset each pass appended a duplicate section.
    box.innerHTML = "";

    // ── Gate: the built-in engine is OFF by default ────────────────────
    // Local models are expected to run behind LM Studio / Ollama (the
    // OpenAI-compatible endpoint configured in Settings → Providers).
    // Clicking 启用 expands the full engine UI and persists the choice;
    // 停用 collapses it again. Rationale: maintaining a private llama.cpp
    // install (CUDA builds, MoE tuning, per-model flags) duplicates what
    // those apps already do well.
    let engineEnabled = false;
    try {
      const prefs = await window.petSettings.getEnginePrefs();
      engineEnabled = !!(prefs && prefs.params && prefs.params.engine_enabled);
    } catch {}
    box.appendChild(sectionTitle(t("petSectionEngine")));
    const engSection = helpers.buildSection("", []);
    const engRows = engSection.querySelector(".section-rows");

    // One switch showing the real preference. The gate card that used to
    // sit above this section rendered "OFF, click 启用" while this row's
    // hardcoded `true` said "已启用" — two widgets contradicting each other.
    engRows.appendChild(switchRow(
      t("petEngineGateTitle"),
      t("petEngineGateDesc"),
      engineEnabled,
      async (next) => {
        await window.petSettings.setEngineParams({ engine_enabled: !!next });
        void ctx.refreshAll();
        return { ok: true };
      },
    ));
    const liveLlama = !!(ctx && ctx.healthSnapshot && ctx.healthSnapshot.llamaReady);
    if (!engineEnabled && liveLlama) {
      engRows.appendChild(el("div", { className: "row" },
        el("div", { className: "row-text" },
          el("span", { className: "row-desc" }, t("petEngineGateLive")))));
    }

    // ── Row 1: runtime switch ───────────────────────────────────────────
    // Start uses the currently configured model (errors loudly when none
    // is picked), stop kills llama-server. The engine still auto-starts
    // for local inference — this switch is for people who want the switch
    // in their hand. After a toggle we refresh ONLY the header pill: the
    // health tick repaints the card later, and rebuilding the row the
    // user just clicked would make the switch visibly jump.
    const llamaReady = !!(ctx && ctx.healthSnapshot && ctx.healthSnapshot.llamaReady);
    engRows.appendChild(switchRow(
      t("petRowEngineRunning"),
      llamaReady ? t("petEngineRunningYes") : t("petEngineRunningNo"),
      llamaReady,
      async (next) => {
        const fn = next ? window.petSettings.engineStart : window.petSettings.engineStop;
        const ret = await fn();
        if (ret && ret.status === "error") return { ok: false, error: ret.message || "" };
        await probeHealth(ctx);
        syncStatusPill(ctx);
        return { ok: true };
      },
      { failureMessage: t("petEngineStartFail") },
    ));

    // ── Row 2: version + update actions ─────────────────────────────────
    // One-click self-update of the inference engine: check the official
    // GitHub release, then apply (download → swap → auto-restart). The
    // sidecar endpoints live in gateway/sidecar_updater.py.
    const verRow = el("div", { className: "row pet-engine-version-row" });
    const verText = el("div", { className: "row-text" });
    verText.appendChild(el("span", { className: "row-label" }, t("petRowEngineVersion")));
    const engValue = el("span", { className: "row-desc pet-engine-value" }, t("petEngineUnchecked"));
    verText.appendChild(engValue);
    // Slow-network escape hatch: collapsed by default, one quiet link.
    const slowLink = el("button", { type: "button", className: "pet-slow-link" }, t("petEngineSlowLink"));
    verText.appendChild(slowLink);
    verRow.appendChild(verText);

    const verCtl = el("div", { className: "row-control pet-path-actions" });

    // Live download progress bar (hidden until an update starts).
    const progRow = el("div", { className: "pet-engine-progress-row", style: { display: "none" } });
    const progTrack = el("div", {
      style: {
        height: "6px", borderRadius: "3px",
        background: "rgba(128,128,128,0.25)", overflow: "hidden",
      },
    });
    const progFill = el("div", {
      style: {
        height: "100%", width: "0%",
        background: "var(--accent, #4a9eff)",
        transition: "width .15s ease",
      },
    });
    const progText = el("div", {
      style: {
        fontSize: "11px", color: "var(--text-secondary, #8899b0)",
        marginTop: "4px",
      },
    });
    progTrack.appendChild(progFill);
    progRow.appendChild(progTrack);
    progRow.appendChild(progText);

    const slowPanel = el("div", {
      className: "pet-slow-panel",
      style: { display: "none" },
    });
    slowPanel.innerHTML = t("petEngineSlowSolution");
    slowLink.addEventListener("click", () => {
      slowPanel.style.display = slowPanel.style.display === "none" ? "block" : "none";
    });

    const updateBtn = softBtn(t("petEngineUpdateButton"), async () => {
      if (updateBtn.disabled) return;
      updateBtn.disabled = true;
      checkBtn.disabled = true;
      updateBtn.textContent = t("petEngineUpdateBusy");
      engValue.textContent = "…";
      progRow.style.display = "";
      progFill.style.width = "0%";
      progText.textContent = "…";
      engineUpdating = true;
      // Speed calculation: delta bytes / delta time between transfers,
      // smoothed with an EMA so it doesn't jump, and shown in adaptive
      // units (MB/s or KB/s — a raw toFixed(1) on MB/s shows "0.0" for
      // anything under ~50 KB/s even while the download is progressing).
      let lastDone = 0;
      let lastAt = 0;
      let emaSpeed = 0;
      const fmtSpeed = (bps) => {
        if (bps >= 1024 * 1024) return `${(bps / 1048576).toFixed(1)} MB/s`;
        if (bps >= 1024) return `${Math.round(bps / 1024)} KB/s`;
        return `${Math.round(bps)} B/s`;
      };
      // Live progress from the sidecar SSE stream (start / transfer /
      // swap / complete / error).
      const unsub = window.petSettings.onEngineUpdateProgress((ev) => {
        if (ev.phase === "start") {
          progFill.style.width = "0%";
          progText.textContent = t("petEngineCheckBusy");
          lastDone = 0;
          lastAt = 0;
          emaSpeed = 0;
        } else if (ev.phase === "transfer") {
          const done = ev.bytes_done || 0;
          const total = ev.bytes_total || 0;
          const pct = total > 0 ? Math.min(95, Math.round((done / total) * 100)) : 0;
          progFill.style.width = pct + "%";
          const now = Date.now();
          let speed = "";
          if (lastAt > 0 && done > lastDone) {
            const secs = (now - lastAt) / 1000;
            if (secs > 0) {
              const inst = ((done - lastDone) / secs);
              emaSpeed = emaSpeed > 0 ? emaSpeed * 0.7 + inst * 0.3 : inst;
              speed = `  ·  ${fmtSpeed(emaSpeed)}`;
            }
          }
          lastDone = done;
          lastAt = now;
          progText.textContent = `${formatSize(done)} / ${formatSize(total)}${speed}`;
        } else if (ev.phase === "swap") {
          progFill.style.width = "97%";
          progText.textContent = t("petEngineUpdateBusy");
        } else if (ev.phase === "complete" || ev.phase === "reloaded") {
          progFill.style.width = "100%";
        } else if (ev.phase === "error") {
          progText.textContent = (ev.message || t("petEngineUpdateFailed"));
        }
      });
      try {
        const ret = await window.petSettings.engineUpdateApply();
        if (ret && ret.status === "ok") {
          engValue.textContent = t("petEngineUpdateDone");
          progText.textContent = "✓ " + t("petEngineUpdateDone");
          engineCheckCache = null; // local build changed — re-check next render
        } else {
          const msg = t("petEngineUpdateFailed") + ((ret && ret.message) || "");
          engValue.textContent = msg;
          progText.textContent = msg;
        }
        updateBtn.disabled = true;
      } finally {
        if (typeof unsub === "function") unsub();
        updateBtn.textContent = t("petEngineUpdateButton");
        checkBtn.disabled = false;
        engineUpdating = false;
        // Collapse the progress row a couple seconds after finishing.
        setTimeout(() => { progRow.style.display = "none"; }, 2500);
      }
    }, { accent: true });
    updateBtn.disabled = true;

    // Offline update: install the engine from a folder the user picked
    // (a copy of an official release someone else downloaded — useful on
    // slow networks). The folder must contain a llama-server newer than
    // the installed one; it is copied, never modified.
    const dirBtn = softBtn(t("petEngineUpdateDirButton"), async () => {
      if (dirBtn.disabled) return;
      dirBtn.disabled = true;
      checkBtn.disabled = true;
      updateBtn.disabled = true;
      dirBtn.textContent = t("petEngineCheckBusy");
      progRow.style.display = "";
      progFill.style.width = "0%";
      progText.textContent = "…";
      engineUpdating = true;
      const unsub = window.petSettings.onEngineUpdateProgress((ev) => {
        if (ev.phase === "start") {
          progText.textContent = t("petEngineUpdateDirVerifying");
        } else if (ev.phase === "verify") {
          progFill.style.width = "50%";
          progText.textContent = `build ${ev.build}`;
        } else if (ev.phase === "swap") {
          progFill.style.width = "90%";
          progText.textContent = t("petEngineUpdateBusy");
        } else if (ev.phase === "complete" || ev.phase === "reloaded") {
          progFill.style.width = "100%";
        } else if (ev.phase === "error") {
          progText.textContent = (ev.message || t("petEngineUpdateFailed"));
        }
      });
      try {
        const ret = await window.petSettings.engineUpdateApplyDir();
        if (ret && ret.status === "canceled") {
          progRow.style.display = "none";
          return;
        }
        if (ret && ret.status === "ok") {
          engValue.textContent = t("petEngineUpdateDone");
          progText.textContent = "✓ " + t("petEngineUpdateDone");
          engineCheckCache = null;
        } else {
          const msg = t("petEngineUpdateFailed") + ((ret && ret.message) || "");
          engValue.textContent = msg;
          progText.textContent = msg;
        }
      } finally {
        if (typeof unsub === "function") unsub();
        dirBtn.textContent = t("petEngineUpdateDirButton");
        dirBtn.disabled = false;
        checkBtn.disabled = false;
        engineUpdating = false;
        setTimeout(() => { progRow.style.display = "none"; }, 2500);
      }
    });

    const checkBtn = softBtn(t("petEngineCheckButton"), async () => {
      if (checkBtn.disabled) return;
      await runEngineCheck();
    });

    verCtl.appendChild(checkBtn);
    verCtl.appendChild(updateBtn);
    verCtl.appendChild(dirBtn);
    verRow.appendChild(verCtl);
    engRows.appendChild(verRow);
    engRows.appendChild(progRow);
    engRows.appendChild(slowPanel);

    // Auto-check on every render so the user always sees which engine
    // build is installed — no need to click anything first.
    //
    // Two steps: the LOCAL build comes from llama-server --version
    // directly (works even when the sidecar is down); the REMOTE check
    // goes through the sidecar + GitHub and only upgrades the line.
    let localBuild = null;
    async function fetchLocalVersion() {
      try {
        const ret = await window.petSettings.engineLocalVersion();
        if (ret && ret.status === "ok" && ret.build != null) {
          localBuild = ret.build;
          return true;
        }
      } catch {}
      return false;
    }

    async function runEngineCheck() {
      // 1. Local build first — fast, no sidecar needed.
      const haveLocal = await fetchLocalVersion();
      if (haveLocal) {
        engValue.textContent = `build ${localBuild}`;
        updateBtn.disabled = true;
      } else {
        // No llama-server on disk — say so plainly instead of "build ?".
        engValue.textContent = t("petEngineNotInstalled");
        updateBtn.disabled = false; // "update" doubles as "install"
      }
      // 2. Remote check (sidecar + GitHub) upgrades the line.
      checkBtn.disabled = true;
      checkBtn.textContent = t("petEngineCheckBusy");
      try {
        const ret = await window.petSettings.engineUpdateCheck();
        if (!ret || ret.status !== "ok" || !ret.info) {
          // The raw reason (connection refused / timeout) still matters for
          // telling "sidecar not running" apart from GitHub being
          // unreachable — but it belongs in the hover title, not in the
          // row, where `connect ECONNREFUSED 127.0.0.1:18765` just reads
          // as a broken page.
          const reason = (ret && ret.message) || (ret && ret.error) || "";
          if (reason) engValue.title = reason;
          if (haveLocal) {
            engValue.textContent = `build ${localBuild}（${t("petEngineUnreachable")}）`;
          } else {
            engValue.textContent = t("petEngineNotInstalled") + "（" + t("petEngineUnreachable") + "）";
          }
          return;
        }
        const info = ret.info;
        let text;
        let hasUpdate = false;
        // NOTE: settings t() is a plain dict lookup (no {param} support) —
        // substitute placeholders manually.
        if (!haveLocal && info.local_build == null) {
          // Engine not installed: remote tag known → offer to install.
          text = t("petEngineNotInstalled")
            + (info.remote_tag ? `（${t("petEngineRemoteLatest")}: ${info.remote_tag}）` : "");
          hasUpdate = true;
        } else {
          const localLabel = String(info.local_build ?? localBuild ?? "?");
          if (info.error) {
            // Remote check failed (e.g. GitHub unreachable). Show the
            // local build + the reason — never a silent "up to date".
            text = `build ${localLabel}（${info.error}）`;
          } else if (info.available) {
            text = t("petEngineUpdateAvailable")
              .replace("{remote}", info.remote_tag || "?")
              .replace("{local}", localLabel);
            hasUpdate = true;
          } else {
            text = t("petEngineUpToDate").replace("{local}", localLabel);
          }
        }
        engValue.textContent = text;
        updateBtn.disabled = !hasUpdate;
        engineCheckCache = { at: Date.now(), text, hasUpdate, installRoot: info.install_root || "" };
      } finally {
        checkBtn.disabled = false;
        checkBtn.textContent = t("petEngineCheckButton");
      }
    }

    // ── Row 3: backend selector ─────────────────────────────────────────
    // cpu / vulkan IS an engine property — it decides which binary runs.
    const backendMode = window.petSettings.getBackendMode
      ? window.petSettings.getBackendMode()
      : null;

    if (IS_WINDOWS && backendMode !== "openvino") {
      let devices = null;
      try { devices = await window.petSettings.listDevices(); } catch {}
      const available = Array.isArray(devices && devices.available) ? devices.available : ["cpu"];
      const reasons = (devices && devices.reasons) || {};
      // The engine decides what is selectable — the list below is only the
      // display order. Hardcoding ["cpu","vulkan"] here is what made CUDA
      // unpickable even when the sidecar reported it.
      const pickable = ["cpu", "cuda", "vulkan", "metal"].filter((d) => available.includes(d));
      // A backend the install ships but the engine cannot address stays
      // visible and greyed, carrying the engine's own reason. "只能选 CPU"
      // has to explain itself, or it reads as a broken control.
      const blocked = ["cuda", "vulkan"].filter((d) => !available.includes(d) && reasons[d]);
      if (pickable.length > 0) {
        const current = (devices && devices.current) || "cpu";
        const row = el("div", { className: "row" });
        const text = el("div", { className: "row-text" });
        text.appendChild(el("span", { className: "row-label" }, t("petRowBackend")));
        text.appendChild(el("span", { className: "row-desc" }, t("petRowBackendDesc")));
        row.appendChild(text);
        const segmented = el("div", { className: "segmented pet-backend-segmented" });
        for (const device of blocked) {
          const btn = el("button", {
            type: "button",
            className: "is-unavailable",
            disabled: true,
            title: String(reasons[device] || ""),
          }, deviceLabel(device));
          segmented.appendChild(btn);
        }
        for (const device of pickable) {
          const btn = el("button", {
            type: "button",
            className: current === device ? "active" : "",
            onClick: async () => {
              if (btn.disabled || current === device) return;
              Array.from(segmented.querySelectorAll("button")).forEach((b) => { b.disabled = true; });
              try {
                const fn = window.petSettings.setDeviceAndRestart || window.petSettings.setDevice;
                const ret = await fn(device);
                if (ret && ret.ok === false) {
                  if (ret.fallback === "cpu") {
                    if (ops && typeof ops.showToast === "function") {
                      ops.showToast(t("petBackendVulkanFallback"), { error: true, ttl: 6000 });
                    }
                  } else if (ops && typeof ops.showToast === "function") {
                    ops.showToast(t("toastSaveFailed") + (ret.error || "unknown error"), { error: true });
                  }
                }
              } catch (err) {
                if (ops && typeof ops.showToast === "function") {
                  ops.showToast(t("toastSaveFailed") + (err && err.message || ""), { error: true });
                }
              } finally {
                void ctx.refreshAll();
              }
            },
          }, deviceLabel(device));
          const hint = device === "vulkan" ? t("petBackendVulkanExperimental") : reasons[device];
          if (hint && hint !== device) btn.setAttribute("title", String(hint));
          segmented.appendChild(btn);
        }
        const ctl = el("div", { className: "row-control" });
        ctl.appendChild(segmented);
        row.appendChild(ctl);
        engRows.appendChild(row);
      }
    }

    // ── Advanced parameters (collapsible, LM-Studio-style knobs) ───────
    // Each row maps 1:1 to a llama-server CLI flag. Values persist to
    // prefs and reach the engine as PET_* env vars on the next spawn —
    // which is why Apply restarts the sidecar. Collapsed by default: nine
    // rows used to push the rest of the engine section off-screen.
    let paramsInfo = null;
    try { paramsInfo = await window.petSettings.engineParams(); } catch {}
    const curParams = (paramsInfo && paramsInfo.params) || {};

    const advToggle = el("div", { className: "row pet-engine-adv-toggle" });
    advToggle.appendChild(el("div", { className: "row-text" },
      el("span", { className: "row-label" }, t("petEngineAdvanced"))));
    const advArrow = el("span", { className: "pet-engine-adv-arrow" }, "\u25B8");
    advToggle.appendChild(el("div", { className: "row-control" }, advArrow));
    engRows.appendChild(advToggle);

    const advBody = el("div", { className: "pet-engine-adv-body", style: { display: "none" } });
    advBody.appendChild(el("div", { className: "row-desc", style: { margin: "0 0 6px" } },
      t("petEngineAdvancedDesc")));
    advToggle.addEventListener("click", () => {
      const open = advBody.style.display !== "none";
      advBody.style.display = open ? "none" : "block";
      advArrow.textContent = open ? "\u25B8" : "\u25BE";
    });
    engRows.appendChild(advBody);

    const inputs = {};
    const mkSelectRow = (labelKey, key, options, curVal) => {
      const row = el("div", { className: "row" });
      const text = el("div", { className: "row-text" });
      text.appendChild(el("span", { className: "row-label" }, t(labelKey)));
      row.appendChild(text);
      const sel = el("select", { className: "pet-engine-adv-input" });
      for (const o of options) {
        const opt = el("option", { value: o.v }, o.label);
        if (String(curVal || "") === o.v) opt.selected = true;
        sel.appendChild(opt);
      }
      inputs[key] = sel;
      row.appendChild(el("div", { className: "row-control" }, sel));
      advBody.appendChild(row);
    };
    const mkNumberRow = (labelKey, key, curVal) => {
      const row = el("div", { className: "row" });
      const text = el("div", { className: "row-text" });
      text.appendChild(el("span", { className: "row-label" }, t(labelKey)));
      row.appendChild(text);
      const inp = el("input", { type: "number", className: "pet-engine-adv-input", min: "1" });
      if (curVal != null) inp.value = String(curVal);
      inputs[key] = inp;
      row.appendChild(el("div", { className: "row-control" }, inp));
      advBody.appendChild(row);
    };

    mkSelectRow("petEngineLoadMode", "load_mode", [
      { v: "mmap", label: t("petEngineLoadMmap") },
      { v: "mmap+mlock", label: t("petEngineLoadMmapMlock") },
      { v: "none", label: t("petEngineLoadNone") },
    ], curParams.load_mode || "mmap");
    mkNumberRow("petEngineNGpuLayers", "n_gpu_layers", curParams.n_gpu_layers);
    mkNumberRow("petEngineCtxSize", "ctx_size", curParams.ctx_size);
    mkNumberRow("petEngineNCpuMoe", "n_cpu_moe", curParams.n_cpu_moe);
    mkSelectRow("petEngineCacheTypeK", "cache_type_k", [
      { v: "", label: t("petEngineCacheDefault") },
      { v: "q8_0", label: "q8_0" },
      { v: "q4_0", label: "q4_0" },
    ], curParams.cache_type_k || "");
    mkSelectRow("petEngineCacheTypeV", "cache_type_v", [
      { v: "", label: t("petEngineCacheDefault") },
      { v: "q8_0", label: "q8_0" },
      { v: "q4_0", label: "q4_0" },
    ], curParams.cache_type_v || "");
    const mkFlashRow = () => {
      const row = el("div", { className: "row" });
      const text = el("div", { className: "row-text" });
      text.appendChild(el("span", { className: "row-label" }, t("petEngineFlashAttn")));
      row.appendChild(text);
      const sel = el("select", { className: "pet-engine-adv-input" });
      for (const o of [
        { v: "", label: t("petEngineCacheDefault") },
        { v: "1", label: "on" },
        { v: "0", label: "off" },
      ]) {
        const opt = el("option", { value: o.v }, o.label);
        if (String(curParams.flash_attn === true ? "1" : curParams.flash_attn === false ? "0" : "") === o.v) opt.selected = true;
        sel.appendChild(opt);
      }
      inputs.flash_attn = sel;
      row.appendChild(el("div", { className: "row-control" }, sel));
      advBody.appendChild(row);
    };
    mkFlashRow();
    mkNumberRow("petEngineBatchSize", "batch_size", curParams.batch_size);
    mkNumberRow("petEngineUbatchSize", "ubatch_size", curParams.ubatch_size);
    mkNumberRow("petEngineThreads", "threads", curParams.threads);
    mkNumberRow("petEngineParallel", "parallel", curParams.parallel);
    mkNumberRow("petEngineCtxCheckpoints", "ctx_checkpoints", curParams.ctx_checkpoints);
    const mkBoolRow = (labelKey, key, curVal) => {
      const row = el("div", { className: "row" });
      const text = el("div", { className: "row-text" });
      text.appendChild(el("span", { className: "row-label" }, t(labelKey)));
      row.appendChild(text);
      const sel = el("select", { className: "pet-engine-adv-input" });
      for (const o of [
        { v: "", label: t("petEngineCacheDefault") },
        { v: "1", label: "on" },
        { v: "0", label: "off" },
      ]) {
        const opt = el("option", { value: o.v }, o.label);
        if (String(curVal === true ? "1" : curVal === false ? "0" : "") === o.v) opt.selected = true;
        sel.appendChild(opt);
      }
      inputs[key] = sel;
      row.appendChild(el("div", { className: "row-control" }, sel));
      advBody.appendChild(row);
    };
    mkBoolRow("petEngineKvOffload", "kv_offload", curParams.kv_offload);
    mkBoolRow("petEngineKvUnified", "kv_unified", curParams.kv_unified);
    mkSelectRow("petEngineSpecType", "spec_type", [
      { v: "", label: t("petEngineCacheDefault") },
      { v: "draft-mtp", label: "draft-mtp" },
      { v: "none", label: "none" },
    ], curParams.spec_type || "");
    mkNumberRow("petEngineSpecDraftNMax", "spec_draft_n_max", curParams.spec_draft_n_max);
    mkNumberRow("petEngineVerbosity", "verbosity", curParams.verbosity);

    // Free-form extra args — the escape hatch for knobs we do not model
    // (--main-gpu, --tensor-split, --split-mode, --chat-template-file, …).
    const extraRow = el("div", { className: "row" });
    const extraText = el("div", { className: "row-text" });
    extraText.appendChild(el("span", { className: "row-label" }, t("petEngineExtraArgs")));
    extraText.appendChild(el("span", { className: "row-desc" }, t("petEngineExtraArgsDesc")));
    extraRow.appendChild(extraText);
    const extraInput = el("input", {
      type: "text",
      className: "pet-engine-adv-input",
      placeholder: "--main-gpu 0 --tensor-split 1,0",
    });
    if (Array.isArray(curParams.extra_args)) extraInput.value = curParams.extra_args.join(" ");
    inputs.extra_args = extraInput;
    extraRow.appendChild(el("div", { className: "row-control" }, extraInput));
    advBody.appendChild(extraRow);

    // Buttons: apply (persist + restart) and speed test.
    const advBtnRow = el("div", { className: "row" });
    advBtnRow.appendChild(el("div", { className: "row-text" },
      el("span", { className: "row-label" }, t("petEngineApply"))));
    const advCtl = el("div", { className: "row-control pet-path-actions" });
    const benchResult = el("div", { className: "pet-engine-bench-result" }, "");
    const applyBtn = softBtn(t("petEngineApplyBtn"), async () => {
      applyBtn.disabled = true;
      applyBtn.classList.add("is-busy");
      try {
        const payload = {};
        const NUM_KEYS = [
          "n_cpu_moe", "batch_size", "ubatch_size", "n_gpu_layers", "ctx_size",
          "threads", "parallel", "ctx_checkpoints", "spec_draft_n_max", "verbosity",
        ];
        const BOOL_KEYS = ["flash_attn", "kv_offload", "kv_unified"];
        for (const [key, node] of Object.entries(inputs)) {
          const v = (node.value || "").trim();
          if (v === "") continue;
          if (BOOL_KEYS.includes(key)) payload[key] = v === "1";
          else if (NUM_KEYS.includes(key)) payload[key] = Number(v);
          else payload[key] = v; // load_mode / cache_type_k/v / spec_type / extra_args
        }
        // load_mode always sent so the default (mmap) is explicit.
        payload.load_mode = (inputs.load_mode && inputs.load_mode.value) || "mmap";
        const ret = await window.petSettings.setEngineParams(payload);
        if (!ret || !ret.ok) {
          notifyError(t("toastSaveFailed") + ((ret && ret.error) || ""));
          return;
        }
        if (ops && typeof ops.showToast === "function") {
          ops.showToast(t("petEngineApplyRestarting"), { ttl: 4000 });
        }
        // Restart the sidecar so the new flags take effect, then refresh.
        try { await window.petSettings.restartSidecar(); } catch {}
        void ctx.refreshAll();
      } finally {
        applyBtn.disabled = false;
        applyBtn.classList.remove("is-busy");
      }
    });
    advCtl.appendChild(applyBtn);

    const benchBtn = softBtn(t("petEngineBenchBtn"), async () => {
      benchBtn.disabled = true;
      benchBtn.classList.add("is-busy");
      benchResult.textContent = t("petEngineBenchRunning");
      try {
        const ret = await window.petSettings.engineBenchmark(128);
        if (!ret || ret.status === "error" || ret.ok === false) {
          benchResult.textContent = t("petEngineBenchFail") + ((ret && (ret.message || ret.error)) || "");
        } else if (ret.tps) {
          benchResult.textContent = `${ret.tps} tok/s`
            + (ret.prompt_tps ? `  \u00B7  prompt ${ret.prompt_tps} tok/s` : "")
            + (ret.ms ? `  \u00B7  ${(ret.ms / 1000).toFixed(1)}s / ${ret.n_predict || 0} tokens` : "");
        } else {
          benchResult.textContent = t("petEngineBenchFail");
        }
      } catch (err) {
        benchResult.textContent = t("petEngineBenchFail") + (err && err.message || err);
      } finally {
        benchBtn.disabled = false;
        benchBtn.classList.remove("is-busy");
      }
    });
    advCtl.appendChild(benchBtn);
    advBtnRow.appendChild(advCtl);
    advBody.appendChild(advBtnRow);
    advBody.appendChild(benchResult);

    // Live view of the exact command line these knobs produce.
    const argv = (paramsInfo && Array.isArray(paramsInfo.argv) && paramsInfo.argv.length)
      ? paramsInfo.argv.join(" ")
      : "";
    if (argv) {
      advBody.appendChild(el("div", { className: "row-desc" }, t("petEngineArgvLabel")));
      advBody.appendChild(el("pre", { className: "pet-engine-argv" }, argv));
    }

    box.appendChild(engSection);

    // Auto-check result is cached for a minute: the health tick rebuilds
    // this section repeatedly, and re-hitting the GitHub API every tick
    // would flicker "…" and spam the network.
    if (engineCheckCache && Date.now() - engineCheckCache.at < 60000) {
      engValue.textContent = engineCheckCache.text;
      updateBtn.disabled = !engineCheckCache.hasUpdate;
    } else {
      void runEngineCheck();
    }
  }

  // ── Advanced (collapsible) — install location + restart Sidecar + logs ─
  //
  // Hand-rolled instead of using helpers.buildCollapsibleGroup so the
  // disclosure trigger can sit in the small-caps section-title style. The
  // body is just a standard section-rows block; we toggle its visibility
  // with display:none rather than a height animation because the row count
  // is tiny and reflow is instant.
  function renderAdvancedSection(box, ctx) {
    box.innerHTML = "";
    const wrap = el("section", { className: "section pet-advanced-section" });

    const trigger = el("button", {
      type: "button",
      className: "pet-advanced-trigger" + (advancedExpanded ? " open" : ""),
      "aria-expanded": advancedExpanded ? "true" : "false",
    });
    const chev = el("span", { className: "pet-advanced-chevron", "aria-hidden": "true" });
    chev.innerHTML = SVG_CHEVRON;
    trigger.appendChild(chev);
    trigger.appendChild(el("span", { className: "section-title pet-advanced-title" }, t("petSectionAdvanced")));
    wrap.appendChild(trigger);

    const section = helpers.buildSection("", []);
    section.classList.add("pet-advanced-body");
    const rows = section.querySelector(".section-rows");

    // ── Install location row: where the binary actually lives ──────────
    // Sourced from the sidecar's own resolution (same candidate list the
    // launcher uses), so it can never disagree with what really runs.
    // Debug info — belongs here, not in the main engine card.
    const locHint = (engineCheckCache && engineCheckCache.installRoot)
      || "pet-sidecar\\bin\\win-x64\\llama-server.exe";
    const locRow = el("div", { className: "row" });
    const locText = el("div", { className: "row-text" });
    locText.appendChild(el("span", { className: "row-label" }, t("petRowEngineLocation")));
    const locVal = el("span", { className: "row-desc pet-path-value", title: locHint }, locHint);
    locText.appendChild(locVal);
    locRow.appendChild(locText);
    rows.appendChild(locRow);

    rows.appendChild(buildAdvancedRow({
      icon: SVG_RESTART,
      title: t("petActionRestartSidecar"),
      desc: t("petActionRestartSidecarDesc"),
      busyLabel: t("petActionRestartSidecarBusy"),
      onClick: async () => {
        try { await window.petSettings.restartSidecar(); } catch {}
        void ctx.refreshAll();
      },
    }));
    rows.appendChild(buildAdvancedRow({
      icon: SVG_LOG,
      title: t("petActionOpenLogs"),
      desc: t("petActionOpenLogsDesc"),
      onClick: async () => {
        const ret = await window.petSettings.openLogsDir();
        if (ret && !ret.ok) notifyError(ret.error || t("petActionOpenLogsFailed"));
      },
    }));

    wrap.appendChild(section);
    box.appendChild(wrap);

    function applyExpanded() {
      trigger.classList.toggle("open", advancedExpanded);
      trigger.setAttribute("aria-expanded", advancedExpanded ? "true" : "false");
      section.style.display = advancedExpanded ? "" : "none";
    }
    applyExpanded();
    trigger.addEventListener("click", () => {
      advancedExpanded = !advancedExpanded;
      applyExpanded();
    });
  }

  function buildAdvancedRow({ icon, title, desc, busyLabel, onClick }) {
    const row = el("div", { className: "row pet-advanced-row" });
    const iconBox = el("span", { className: "pet-advanced-row-icon", "aria-hidden": "true" });
    iconBox.innerHTML = icon;
    row.appendChild(iconBox);
    const text = el("div", { className: "row-text" });
    text.appendChild(el("span", { className: "row-label" }, title));
    if (desc) text.appendChild(el("span", { className: "row-desc" }, desc));
    row.appendChild(text);
    const ctl = el("div", { className: "row-control" });
    // The trigger button blends visually with the row; we keep the entire
    // row clickable so the touch target matches the visual surface.
    row.classList.add("clickable");
    row.setAttribute("role", "button");
    row.setAttribute("tabindex", "0");
    let pending = false;
    async function run() {
      if (pending) return;
      pending = true;
      row.classList.add("is-busy");
      if (busyLabel) text.querySelector(".row-label").textContent = busyLabel;
      try { await onClick(); } catch {}
      pending = false;
      row.classList.remove("is-busy");
      if (busyLabel) text.querySelector(".row-label").textContent = title;
    }
    row.addEventListener("click", () => { void run(); });
    row.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); void run(); }
    });
    row.appendChild(ctl);
    return row;
  }

  // ── Refresh + polling ─────────────────────────────────────────────────

  // The engine + advanced cards are adopted by the 模型来源 page, so every
  // paint of them has to check the box is still in a document: a detached
  // box means the providers tab re-rendered without us and rebuilding here
  // would leak a fresh card into an orphan node.
  async function refreshAll(ctx) {
    if (!window.petSettings || !ctx) return;
    await probeHealth(ctx);
    syncStatusPill(ctx);
    renderModelSection(ctx.modelBox, ctx);
    if (ctx.engineBox.isConnected) await renderEngineSection(ctx.engineBox, ctx);
    if (ctx.advancedBox.isConnected) renderAdvancedSection(ctx.advancedBox, ctx);
  }

  function nextHealthDelay(ctx) {
    if (!ctx.everHealthy && ctx.fastAttemptsLeft > 0) return HEALTH_INTERVAL_MS_FAST;
    return HEALTH_INTERVAL_MS_SLOW;
  }

  // The polling loop refreshes only the status pill + cards the user is
  // not interacting with, so open dropdowns and in-flight clicks survive.
  function startHealthPolling(ctx) {
    if (healthTimer) {
      clearTimeout(healthTimer);
      healthTimer = null;
    }
    const tick = async () => {
      healthTimer = null;
      if (!mounted || document.hidden || !petOrProvidersActive()) return;
      const wasHealthy = ctx.everHealthy;
      await probeHealth(ctx);
      syncStatusPill(ctx);
      // Path may have switched after a load-model — keep the cards
      // honest, but never rebuild one the user is mid-interaction with
      // (an open <select> would snap shut) and never touch Advanced
      // (would lose focus / expanded state).
      if (!boxInteractionBusy(ctx.modelBox)) renderModelSection(ctx.modelBox, ctx);
      if (ctx.engineBox.isConnected && !boxInteractionBusy(ctx.engineBox)) {
        await renderEngineSection(ctx.engineBox, ctx);
      }
      if (!ctx.everHealthy && ctx.fastAttemptsLeft > 0) ctx.fastAttemptsLeft -= 1;
      if (!wasHealthy && ctx.everHealthy) ctx.fastAttemptsLeft = 0;
      healthTimer = setTimeout(tick, nextHealthDelay(ctx));
    };
    healthTimer = setTimeout(tick, nextHealthDelay(ctx));
  }

  function armFastProbes(ctx) {
    ctx.fastAttemptsLeft = HEALTH_FAST_ATTEMPTS;
  }

  // The page state outlives a single render: the engine and advanced cards
  // are adopted by the 模型来源 page (they belong to "run the model on this
  // machine"), so their health poll and cached reads must survive tab
  // switches — and survive this page re-rendering underneath them.
  let pageCtx = null;

  function ensureCtx() {
    if (pageCtx) return pageCtx;
    pageCtx = {
      headerBox: el("div", {}),
      modelBox: el("div", { className: "pet-section-box" }),
      engineBox: el("div", { className: "pet-section-box" }),
      advancedBox: el("div", { className: "pet-section-box" }),
      statusPillSlot: null,
      everHealthy: false,
      fastAttemptsLeft: HEALTH_FAST_ATTEMPTS,
      lastModelName: null,
      lastModelDir: null,
      healthSnapshot: {
        st: null, h: {}, sidecarReady: false, llamaReady: false, probing: true,
        modelName: null, modelDir: null,
      },
      refreshAll: null,
    };
    pageCtx.refreshAll = () => {
      armFastProbes(pageCtx);
      const p = refreshAll(pageCtx);
      startHealthPolling(pageCtx);
      return p;
    };
    return pageCtx;
  }

  // The engine card is on 模型来源 and the brain card is here, so the poll
  // has to stay alive while either page is on screen.
  function petOrProvidersActive() {
    const tab = core && core.state ? core.state.activeTab : null;
    return tab === "pet" || tab === "providers";
  }

  // Called by the 模型来源 tab: adopts the engine + advanced cards into
  // `host` and keeps them fed by the same health poll the Brain page uses.
  function mountEnginePanel(host) {
    if (!host || !window.petSettings) return;
    const ctx = ensureCtx();
    mounted = true;
    host.innerHTML = "";
    host.appendChild(ctx.engineBox);
    host.appendChild(ctx.advancedBox);
    armFastProbes(ctx);
    void (async () => {
      await probeHealth(ctx);
      syncStatusPill(ctx);
      await renderEngineSection(ctx.engineBox, ctx);
      renderAdvancedSection(ctx.advancedBox, ctx);
    })();
    startHealthPolling(ctx);
  }

  async function render(parent) {
    cleanupTimers();
    parent.innerHTML = "";

    const ctx = ensureCtx();

    // Build the header eagerly so the page never flashes empty before
    // /api/health resolves — the pill starts in the "starting" yellow
    // state via the initial probing=true snapshot.
    renderHeader(ctx);

    if (!window.petSettings) {
      parent.appendChild(ctx.headerBox);
      parent.appendChild(el("div", { className: "row-desc" }, t("petIpcUnavailable")));
      return;
    }

    parent.appendChild(ctx.headerBox);
    parent.appendChild(ctx.modelBox);

    mounted = true;
    visibilityHandler = () => {
      if (document.hidden || !petOrProvidersActive()) {
        if (healthTimer) {
          clearTimeout(healthTimer);
          healthTimer = null;
        }
      } else {
        armFastProbes(ctx);
        void (async () => {
          await probeHealth(ctx);
          syncStatusPill(ctx);
          if (!boxInteractionBusy(ctx.modelBox)) renderModelSection(ctx.modelBox, ctx);
        })();
        startHealthPolling(ctx);
      }
    };
    document.addEventListener("visibilitychange", visibilityHandler);

    await refreshAll(ctx);
    startHealthPolling(ctx);
  }

  function init(coreArg) {
    core = coreArg;
    helpers = core.helpers;
    ops = core.ops;
    core.tabs.pet = {
      render: (parent) => { void render(parent); },
    };
    // 模型来源 adopts the engine + advanced cards through this handle.
    core.enginePanel = { mount: mountEnginePanel };
  }

  root.ClawdSettingsTabDeskPet = { init };
})(typeof globalThis !== "undefined" ? globalThis : window);
