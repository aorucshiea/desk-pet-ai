"use strict";

// ── DeskPet settings tab ──
//
// Page layout (top → bottom):
//   • Page header: title + subtitle on the left, sidecar status pill on the right
//   • 模型 / Model               — ONE picker row: current model dropdown (with
//                                  size + ✓), the model path as the row
//                                  description, and Load / Browse actions
//                                — collapsed "scanned folders" disclosure
//                                  (auto-expands when no model is found)
//   • 推理引擎 / Engine           — runtime switch (start/stop llama-server),
//                                  version row with check / update / install
//                                  from folder + inline progress + slow-network
//                                  link, backend segmented control (Windows)
//   • 高级 / Advanced (collapsed) — install location, restart Sidecar, open logs
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
  let foldersExpanded = false;

  // The product surface treats DeskPet5 0.9B as the canonical bundled
  // model. Showing the actual gguf filename as the page title would create
  // noise once users sideload variants — the picker and path row expose it.
  const PATH_TRUNCATE_MAX = 56;

  const HEALTH_INTERVAL_MS_SLOW = 60_000;
  const HEALTH_INTERVAL_MS_FAST = 5_000;
  const HEALTH_FAST_ATTEMPTS = 6;
  const NAVIGATOR_PLATFORM = typeof navigator !== "undefined" ? (navigator.platform || "") : "";
  const IS_WINDOWS = /Win/i.test(NAVIGATOR_PLATFORM);
  const IS_MAC = NAVIGATOR_PLATFORM.startsWith("Mac");

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
  function deriveStatus(sidecarReady, llamaReady, probing) {
    if (sidecarReady && llamaReady) return { tone: "ready", label: t("petStatusRunning") };
    if (sidecarReady || probing) return { tone: "starting", label: t("petStatusStarting") };
    return { tone: "offline", label: t("petStatusError") };
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

  // ── Path helpers ───────────────────────────────────────────────────────
  //
  // Path-aware middle truncation: always keeps the filename + its parent
  // directory, then greedily extends the tail and starts the head with the
  // leading components until we run out of room. Character-level fallback
  // for inputs that don't look like a path. Tooltip restores the full
  // string so we never hide information, only collapse it.
  function truncatePath(p, maxLen = PATH_TRUNCATE_MAX) {
    if (!p) return "";
    if (p.length <= maxLen) return p;
    const usesBackslash = p.includes("\\") && !p.includes("/");
    const sep = usesBackslash ? "\\" : "/";
    const parts = p.split(sep);
    if (parts.length < 3) {
      const headLen = Math.ceil((maxLen - 1) / 2);
      const tailLen = Math.floor((maxLen - 1) / 2);
      return p.slice(0, headLen) + "…" + p.slice(-tailLen);
    }
    const fileName = parts[parts.length - 1];
    const tailPieces = [fileName];
    let tailLen = fileName.length;
    let i = parts.length - 2;
    while (i >= 0 && tailLen + parts[i].length + 1 < maxLen - 6) {
      tailPieces.unshift(parts[i]);
      tailLen += parts[i].length + 1;
      i--;
    }
    const headPieces = [];
    let headLen = 0;
    for (let j = 0; j <= i; j++) {
      const piece = parts[j];
      const pieceTotal = piece.length + (j === 0 ? 0 : 1);
      if (headLen + pieceTotal + tailLen + 3 > maxLen) break;
      headPieces.push(piece);
      headLen += pieceTotal;
    }
    if (headPieces.length === 0) headPieces.push(parts[0] || "");
    return headPieces.join(sep) + sep + "…" + sep + tailPieces.join(sep);
  }

  // ── Section header (matches the small-caps title used elsewhere) ──────
  function sectionTitle(text) {
    return el("h2", { className: "section-title pet-section-title" }, text);
  }

  function deviceLabel(device) {
    if (device === "vulkan") return t("petBackendVulkan");
    if (device === "metal") return t("petBackendMetal");
    return t("petBackendCpu");
  }

  function modelPathOpenLabel() {
    const key = IS_WINDOWS
      ? "petOpenModelPathWindows"
      : IS_MAC
        ? "petOpenModelPathMac"
        : "petOpenModelPathGeneric";
    const label = t(key);
    if (label && label !== key) return label;
    const generic = t("petOpenModelPathGeneric");
    return generic && generic !== "petOpenModelPathGeneric" ? generic : t("petOpenModelPath");
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
    const { sidecarReady, llamaReady, probing } = ctx.healthSnapshot;
    const { tone, label } = deriveStatus(sidecarReady, llamaReady, probing);
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

    const modelNameNow = h.model_name
      || (h.model_dir ? h.model_dir.split(/[/\\]/).pop() : null);
    if (modelNameNow) {
      ctx.lastModelName = modelNameNow;
      ctx.lastModelDir = h.model_dir || ctx.lastModelDir;
    }

    ctx.healthSnapshot = {
      st, h, sidecarReady, llamaReady, probing,
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

  // ── Model section ─────────────────────────────────────────────────────
  //
  // One picker row tells the whole story: label + size + ✓ for the loaded
  // model in the dropdown, the full path as the row description (truncated
  // with a restoring tooltip), and Load / Browse actions. The scanned-
  // folders manager collapses behind a disclosure — it is setup-time UI,
  // not something to stare at on every visit.
  function renderModelSection(box, ctx) {
    box.innerHTML = "";
    const snap = ctx.healthSnapshot || {};
    const modelDir = snap.modelDir || "";
    const hasPath = !!modelDir;

    box.appendChild(sectionTitle(t("petSectionModel")));
    const section = helpers.buildSection("", []);
    const rows = section.querySelector(".section-rows");

    // ── Row 1: current model ──────────────────────────────────────────
    const pickerRow = el("div", { className: "row pet-model-picker-row" });
    const pickerText = el("div", { className: "row-text" });
    pickerText.appendChild(el("span", { className: "row-label" }, t("petRowCurrentModel")));
    const pathDesc = el("span", {
      className: "row-desc pet-path-value" + (hasPath ? "" : " is-unset"),
    }, hasPath ? truncatePath(modelDir, PATH_TRUNCATE_MAX) : t("petModelPathUnset"));
    if (hasPath) pathDesc.setAttribute("title", modelDir);
    pickerText.appendChild(pathDesc);
    pickerRow.appendChild(pickerText);

    const pickerCtl = el("div", { className: "row-control pet-model-picker-control" });
    const picker = el("select", {
      className: "setting-select pet-model-picker",
      style: { minWidth: "0", fontSize: "13px" },
    });
    picker.appendChild(el("option", { value: "" }, "—"));
    pickerCtl.appendChild(picker);

    const loadBtn = softBtn(t("petModelLoadButton"), async () => {
      const target = picker.value;
      if (!target) return;
      if (!window.petSettings || typeof window.petSettings.useModelDir !== "function") return;
      loadBtn.disabled = true;
      const origLabel = loadBtn.textContent;
      loadBtn.textContent = t("petPickModelBusy");
      try {
        const ret = await window.petSettings.useModelDir(target);
        if (ret && !ret.ok && ret.error) notifyError(t("petReloadError") + ret.error);
        if (ret && ret.ok && ret.reloadError) notifyError(t("petReloadError") + ret.reloadError);
      } finally {
        loadBtn.disabled = false;
        loadBtn.textContent = origLabel;
        void ctx.refreshAll();
      }
    }, { accent: true });
    loadBtn.disabled = true;
    pickerCtl.appendChild(loadBtn);

    // Browse: the IPC handler registers the picked file's folder in the
    // scan list AND hot-loads that .gguf (mmproj-* sibling auto-detected).
    // Primary action while nothing is loaded yet; secondary afterwards —
    // the dropdown covers the "switch to a known model" case.
    const browseLabel = t("petChangeModel");
    const browseBtn = softBtn(browseLabel, async () => {
      if (browseBtn.disabled) return;
      loadBtn.disabled = true;
      browseBtn.disabled = true;
      browseBtn.classList.add("is-busy");
      browseBtn.textContent = t("petChangeModelBusy");
      let ret = null;
      try {
        ret = await window.petSettings.pickModelDir();
      } catch (err) {
        notifyError(t("petReloadError") + (err && err.message || err));
      }
      // refreshAll() rebuilds the model section from scratch (replacing
      // these buttons), so restoring the busy state explicitly is only
      // necessary on the canceled / error paths.
      if (ret && ret.ok) {
        if (ret.reloadError) notifyError(t("petReloadError") + ret.reloadError);
        void ctx.refreshAll();
        return;
      }
      if (ret && !ret.canceled && ret.error) notifyError(ret.error);
      browseBtn.classList.remove("is-busy");
      browseBtn.textContent = browseLabel;
      browseBtn.disabled = false;
      loadBtn.disabled = !picker.value || picker.value === (picker.dataset.current || "");
    }, { accent: !hasPath });
    pickerCtl.appendChild(browseBtn);
    pickerRow.appendChild(pickerCtl);
    rows.appendChild(pickerRow);

    // Populate the dropdown asynchronously. Done at render time so it picks
    // up new gguf files dropped into the models folder while the settings
    // window is open.
    if (typeof window.petSettings.listLocalModels === "function") {
      window.petSettings.listLocalModels().then((ret) => {
        const models = (ret && ret.models) || [];
        const currentPath = ((models.find((m) => m && m.current) || {}).path) || "";
        picker.innerHTML = "";
        if (models.length === 0) {
          picker.appendChild(el("option", { value: "" }, t("petModelPathUnset")));
          // Nothing to switch to — surface the folder manager so the user
          // can point the scan at a directory (or a single file).
          foldersExpanded = true;
          applyFoldersExpanded();
          return;
        }
        for (const m of models) {
          if (!m || !m.path) continue;
          const sizeLabel = formatSize(m.sizeBytes);
          const flag = m.current ? " ✓" : "";
          const opt = el("option", { value: m.path, title: m.path },
            `${m.label}${sizeLabel ? "  ·  " + sizeLabel : ""}${flag}`);
          picker.appendChild(opt);
        }
        picker.dataset.current = currentPath || "";
        // Selecting a DIFFERENT entry enables the Load button.
        picker.addEventListener("change", () => {
          loadBtn.disabled = !picker.value || picker.value === currentPath;
        });
        // Pre-select the currently loaded model (its ✓ flag makes the
        // row double as the status display) — with nothing to switch to,
        // the button stays disabled.
        picker.value = currentPath || "";
        loadBtn.disabled = !picker.value || picker.value === currentPath;
      }).catch(() => {
        picker.innerHTML = "";
        picker.appendChild(el("option", { value: "" }, t("petNoAdditionalModels")));
      });
    }

    // ── Row 2: scanned folders (collapsed disclosure) ─────────────────
    const trigger = el("button", {
      type: "button",
      className: "pet-advanced-trigger pet-folders-trigger" + (foldersExpanded ? " open" : ""),
      "aria-expanded": foldersExpanded ? "true" : "false",
    });
    const chev = el("span", { className: "pet-advanced-chevron", "aria-hidden": "true" });
    chev.innerHTML = SVG_CHEVRON;
    trigger.appendChild(chev);
    trigger.appendChild(el("span", { className: "section-title pet-advanced-title" }, t("petModelsFolderLabel")));
    trigger.appendChild(el("span", { className: "row-desc pet-folders-trigger-desc" }, t("petFoldersDesc")));

    const foldersSection = helpers.buildSection("", []);
    foldersSection.classList.add("pet-folders-body");
    const foldersRows = foldersSection.querySelector(".section-rows");

    const foldersListEl = el("div", { className: "pet-model-folders-list" });
    foldersRows.appendChild(foldersListEl);

    const actionsRow = el("div", { className: "pet-folders-actions" });
    const addFolderBtn = softBtn("+ " + t("petModelsFolderAdd"), async () => {
      addFolderBtn.disabled = true;
      try {
        const ret = await window.petSettings.addModelFolder();
        if (!ret || ret.canceled) return;
        if (!ret.ok) {
          notifyError(t("petModelsFolderAddFailed") + (ret.error || ""));
          return;
        }
        renderFoldersList(ret.folders || []);
      } finally {
        addFolderBtn.disabled = false;
      }
    });
    actionsRow.appendChild(addFolderBtn);

    const addFileBtn = softBtn("+ " + t("petModelsFileAdd"), async () => {
      addFileBtn.disabled = true;
      try {
        const ret = await window.petSettings.addModelFile();
        if (!ret || ret.canceled) return;
        if (!ret.ok) {
          notifyError(t("petModelsFolderAddFailed") + (ret.error || ""));
          return;
        }
        renderFoldersList(ret.folders || []);
        if (ret.reloadError) {
          notifyError(t("petReloadError") + ret.reloadError);
        }
      } finally {
        addFileBtn.disabled = false;
      }
    });
    actionsRow.appendChild(addFileBtn);
    foldersRows.appendChild(actionsRow);

    // Render + populate folder list. Defined inline so it can refresh
    // when the user adds/removes folders without a full tab re-render.
    const renderFolderRow = (folder) => {
      const row = el("div", { className: "pet-model-folder-item" });
      const path = el("span", { className: "pet-model-folder-path", title: folder }, folder);
      row.appendChild(path);
      const rm = softBtn(t("petModelsFolderRemove"), async () => {
        try {
          await window.petSettings.removeModelFolder(folder);
          const cur = await window.petSettings.listModelFolders();
          renderFoldersList(cur.folders || []);
        } catch {}
      });
      rm.classList.add("pet-model-folder-remove");
      row.appendChild(rm);
      return row;
    };
    const renderFoldersList = (folders) => {
      foldersListEl.innerHTML = "";
      if (!folders || folders.length === 0) {
        foldersListEl.appendChild(el("div", {
          className: "pet-model-folder-empty",
        }, t("petModelsFolderEmpty")));
        return;
      }
      for (const f of folders) foldersListEl.appendChild(renderFolderRow(f));
    };
    if (window.petSettings && typeof window.petSettings.listModelFolders === "function") {
      window.petSettings.listModelFolders().then((ret) => {
        renderFoldersList((ret && ret.folders) || []);
      }).catch(() => renderFoldersList([]));
    } else {
      renderFoldersList([]);
    }

    function applyFoldersExpanded() {
      // The async model-list callback may fire after a health-tick rebuild
      // replaced this DOM — toggling detached nodes is a no-op, not a crash.
      if (!trigger.isConnected) return;
      trigger.classList.toggle("open", foldersExpanded);
      trigger.setAttribute("aria-expanded", foldersExpanded ? "true" : "false");
      foldersSection.style.display = foldersExpanded ? "" : "none";
    }

    box.appendChild(section);
    box.appendChild(trigger);
    box.appendChild(foldersSection);
    applyFoldersExpanded();
    trigger.addEventListener("click", () => {
      foldersExpanded = !foldersExpanded;
      applyFoldersExpanded();
    });
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
    box.appendChild(sectionTitle(t("petSectionEngine")));
    const engSection = helpers.buildSection("", []);
    const engRows = engSection.querySelector(".section-rows");

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
          // Surface the actual reason (connection refused / timeout) so
          // the user can tell "sidecar not running" apart from GitHub
          // being unreachable.
          const reason = (ret && ret.message) || (ret && ret.error) || "";
          if (haveLocal) {
            engValue.textContent = `build ${localBuild}（${t("petEngineUnreachable")}${reason ? `: ${reason}` : ""}）`;
          } else {
            engValue.textContent = t("petEngineNotInstalled") + "（" + t("petEngineUnreachable") + (reason ? `: ${reason}` : "") + "）";
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
      if (available.includes("cpu") || available.includes("vulkan")) {
        const current = (devices && devices.current) || "cpu";
        const row = el("div", { className: "row" });
        const text = el("div", { className: "row-text" });
        text.appendChild(el("span", { className: "row-label" }, t("petRowBackend")));
        text.appendChild(el("span", { className: "row-desc" }, t("petRowBackendDesc")));
        row.appendChild(text);
        const segmented = el("div", { className: "segmented pet-backend-segmented" });
        for (const device of ["cpu", "vulkan"]) {
          if (!available.includes(device)) continue;
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
          if (device === "vulkan") btn.setAttribute("title", t("petBackendVulkanExperimental"));
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
        for (const [key, node] of Object.entries(inputs)) {
          const v = (node.value || "").trim();
          if (v === "") continue;
          if (key === "flash_attn") payload[key] = v === "1";
          else if (["n_cpu_moe", "batch_size", "ubatch_size", "n_gpu_layers", "ctx_size"].includes(key)) payload[key] = Number(v);
          else payload[key] = v;
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

  async function refreshAll(ctx) {
    if (!window.petSettings || !ctx) return;
    await probeHealth(ctx);
    syncStatusPill(ctx);
    renderModelSection(ctx.modelBox, ctx);
    await renderEngineSection(ctx.engineBox, ctx);
    renderAdvancedSection(ctx.advancedBox, ctx);
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
      if (!mounted || document.hidden || core.state.activeTab !== "pet") return;
      const wasHealthy = ctx.everHealthy;
      await probeHealth(ctx);
      syncStatusPill(ctx);
      // Path may have switched after a load-model — keep the cards
      // honest, but never rebuild one the user is mid-interaction with
      // (an open <select> would snap shut) and never touch Advanced
      // (would lose focus / expanded state).
      if (!boxInteractionBusy(ctx.modelBox)) renderModelSection(ctx.modelBox, ctx);
      if (!boxInteractionBusy(ctx.engineBox)) await renderEngineSection(ctx.engineBox, ctx);
      if (!ctx.everHealthy && ctx.fastAttemptsLeft > 0) ctx.fastAttemptsLeft -= 1;
      if (!wasHealthy && ctx.everHealthy) ctx.fastAttemptsLeft = 0;
      healthTimer = setTimeout(tick, nextHealthDelay(ctx));
    };
    healthTimer = setTimeout(tick, nextHealthDelay(ctx));
  }

  function armFastProbes(ctx) {
    ctx.fastAttemptsLeft = HEALTH_FAST_ATTEMPTS;
  }

  async function render(parent) {
    cleanupTimers();
    parent.innerHTML = "";

    const ctx = {
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
    ctx.refreshAll = () => {
      armFastProbes(ctx);
      const p = refreshAll(ctx);
      startHealthPolling(ctx);
      return p;
    };

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
    parent.appendChild(ctx.engineBox);
    parent.appendChild(ctx.advancedBox);

    mounted = true;
    visibilityHandler = () => {
      if (document.hidden || core.state.activeTab !== "pet") {
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
  }

  root.ClawdSettingsTabDeskPet = { init };
})(typeof globalThis !== "undefined" ? globalThis : window);
