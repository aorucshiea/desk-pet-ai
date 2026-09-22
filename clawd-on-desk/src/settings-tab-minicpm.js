"use strict";

// ── MiniCPM settings tab ──
//
// Page layout (top → bottom):
//   • Page header: title + subtitle on the left, sidecar status pill on the right
//   • 行为 / Behavior              — narration + default thinking switches
//   • 模型 / Model                  — fixed model label + truncated path + buttons
//   • 高级设置 / Advanced (collapsed by default) — restart Sidecar, open logs
//
// Sidecar health is polled at most once a minute (5s during cold-start
// grace), and now only re-renders the header pill. The rest of the page
// stays stable across ticks so the user can interact with switches and
// buttons without re-mount flicker.

(function initSettingsTabMinicpm(root) {
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
  // Survives re-renders within the same Settings session so the user
  // doesn't have to re-expand Advanced every time they revisit the tab.
  let advancedExpanded = false;

  // The product surface treats MiniCPM5 0.9B as the canonical bundled
  // model. Showing the actual gguf filename here would create noise once
  // users sideload variants — we still expose that in the path row.
  const MODEL_INFO_LABEL = "MiniCPM5 0.9B";
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
    if (sidecarReady && llamaReady) return { tone: "ready", label: t("minicpmStatusRunning") };
    if (sidecarReady || probing) return { tone: "starting", label: t("minicpmStatusStarting") };
    return { tone: "offline", label: t("minicpmStatusError") };
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
  function switchRow(label, hint, checked, onChange) {
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
      if (ops && typeof ops.showToast === "function") {
        ops.showToast(t("toastSaveFailed") + (message || "unknown error"), { error: true });
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
    return el("h2", { className: "section-title minicpm-section-title" }, text);
  }

  function deviceLabel(device) {
    if (device === "vulkan") return t("minicpmBackendVulkan");
    if (device === "metal") return t("minicpmBackendMetal");
    return t("minicpmBackendCpu");
  }

  function modelPathOpenLabel() {
    const key = IS_WINDOWS
      ? "minicpmOpenModelPathWindows"
      : IS_MAC
        ? "minicpmOpenModelPathMac"
        : "minicpmOpenModelPathGeneric";
    const label = t(key);
    if (label && label !== key) return label;
    const generic = t("minicpmOpenModelPathGeneric");
    return generic && generic !== "minicpmOpenModelPathGeneric" ? generic : t("minicpmOpenModelPath");
  }

  // ── Header (title + subtitle on left, status pill on right) ───────────
  function renderHeader(ctx) {
    ctx.headerBox.innerHTML = "";
    const wrap = el("div", { className: "minicpm-page-header" });
    const textCol = el("div", { className: "minicpm-page-header-text" });
    textCol.appendChild(el("h1", {}, t("minicpmTitle")));
    textCol.appendChild(el("p", { className: "subtitle" }, t("minicpmSubtitle")));
    wrap.appendChild(textCol);
    ctx.statusPillSlot = el("div", { className: "minicpm-page-header-status" });
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
    try { st = await window.minicpmSettings.getStatus(); } catch {}
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

  async function renderBehaviorSection(box, ctx) {
    box.innerHTML = "";
    const st = ctx.healthSnapshot && ctx.healthSnapshot.st;
    let paramsPayload = null;
    try { paramsPayload = await window.minicpmSettings.getChatParams(); } catch {}
    const thinking = !!(paramsPayload && paramsPayload.params && paramsPayload.params.thinking);

    box.appendChild(sectionTitle(t("minicpmSectionBehavior")));
    const section = helpers.buildSection("", []);
    const rows = section.querySelector(".section-rows");

    // narrationEnabled gates the narration codepath in minicpm-chat.js
    // (`if (!narrationEnabled) return;` in narrateState). Same source of
    // truth as the tray menu — they read/write the same prefs file.
    rows.appendChild(switchRow(
      t("minicpmRowNarration"),
      t("minicpmRowNarrationDesc"),
      !!(st && st.narration),
      (on) => window.minicpmSettings.setNarration(on),
    ));

    // chatParams.thinking is persisted to minicpm-prefs.json and read by
    // the chat bubble on each submit (unless ⌘⇧T overrides for the session).
    rows.appendChild(switchRow(
      t("minicpmRowDefaultThinking"),
      t("minicpmRowDefaultThinkingDesc"),
      thinking,
      (on) => {
        const cur = (paramsPayload && paramsPayload.params) || {};
        return window.minicpmSettings.setChatParams({ ...cur, thinking: on });
      },
    ));

    const backendMode = window.minicpmSettings.getBackendMode
      ? window.minicpmSettings.getBackendMode()
      : null;

    if (IS_WINDOWS && backendMode !== "openvino") {
      let devices = null;
      try { devices = await window.minicpmSettings.listDevices(); } catch {}
      const available = Array.isArray(devices && devices.available) ? devices.available : ["cpu"];
      if (available.includes("cpu") || available.includes("vulkan")) {
        const current = (devices && devices.current) || "cpu";
        const row = el("div", { className: "row" });
        const text = el("div", { className: "row-text" });
        text.appendChild(el("span", { className: "row-label" }, t("minicpmRowBackend")));
        text.appendChild(el("span", { className: "row-desc" }, t("minicpmRowBackendDesc")));
        row.appendChild(text);
        const segmented = el("div", { className: "segmented minicpm-backend-segmented" });
        for (const device of ["cpu", "vulkan"]) {
          if (!available.includes(device)) continue;
          const btn = el("button", {
            type: "button",
            className: current === device ? "active" : "",
            onClick: async () => {
              if (btn.disabled || current === device) return;
              Array.from(segmented.querySelectorAll("button")).forEach((b) => { b.disabled = true; });
              try {
                const fn = window.minicpmSettings.setDeviceAndRestart || window.minicpmSettings.setDevice;
                const ret = await fn(device);
                if (ret && ret.ok === false) {
                  if (ret.fallback === "cpu") {
                    if (ops && typeof ops.showToast === "function") {
                      ops.showToast(t("minicpmBackendVulkanFallback"), { error: true, ttl: 6000 });
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
          if (device === "vulkan") btn.setAttribute("title", t("minicpmBackendVulkanExperimental"));
          segmented.appendChild(btn);
        }
        const ctl = el("div", { className: "row-control" });
        ctl.appendChild(segmented);
        row.appendChild(ctl);
        rows.appendChild(row);
      }
    }
    box.appendChild(section);
  }

  // Engine-update check result cache: { at, text, hasUpdate }. Reused
  // across health-tick rebuilds so the version line doesn't flicker.
  let engineCheckCache = null;
  // While an engine update is running, the health tick must NOT rebuild
  // the section — that would destroy the live progress bar mid-download
  // and reset the buttons (leading to a spurious "另一个更新正在进行").
  let engineUpdating = false;

  function pickLabelFromPath(p) {
    if (!p) return t("minicpmModelPathUnset");
    const m = String(p).match(/([^\\/]+?)(?:\.gguf)?$/i);
    return m ? m[1] : String(p);
  }
  function formatSize(bytes) {
    if (!Number.isFinite(bytes) || bytes <= 0) return "";
    if (bytes >= 1024 * 1024 * 1024) return (bytes / 1073741824).toFixed(2) + " GB";
    return (bytes / 1048576).toFixed(0) + " MB";
  }

  function renderModelSection(box, ctx) {
    box.innerHTML = "";
    const snap = ctx.healthSnapshot || {};
    const modelDir = snap.modelDir || "";
    const hasPath = !!modelDir;
    const truncated = hasPath ? truncatePath(modelDir, PATH_TRUNCATE_MAX) : t("minicpmModelPathUnset");

    box.appendChild(sectionTitle(t("minicpmSectionModel")));
    const section = helpers.buildSection("", []);
    const rows = section.querySelector(".section-rows");

    // ── Model picker row (merged) ────────────────────────────────
    // ONE row for the whole story: the dropdown marks the currently
    // loaded model with ✓ (a separate read-only info row used to sit
    // above this one, duplicating the description text and reading like
    // a second switcher), and any other scanned .gguf is one click away.
    // Drop a .gguf into a scanned folder and it shows up here without a
    // file picker.
    const pickerRow = el("div", { className: "row minicpm-model-picker-row" });
    const pickerText = el("div", { className: "row-text" });
    pickerText.appendChild(el("span", { className: "row-label" }, t("minicpmRowModelInfo")));
    const pickerDesc = el("span", { className: "row-desc" }, t("minicpmModelPickerDesc"));
    pickerText.appendChild(pickerDesc);
    pickerRow.appendChild(pickerText);

    const pickerCtl = el("div", { className: "row-control minicpm-model-picker-control" });
    const picker = el("select", {
      className: "setting-select minicpm-model-picker",
      style: { minWidth: "0", fontSize: "13px", maxWidth: "320px", flex: "1" },
    });
    picker.appendChild(el("option", { value: "" }, "—"));
    pickerCtl.appendChild(picker);

    const pickerBtn = softBtn(t("minicpmPickModelButton"), async () => {
      const target = picker.value;
      if (!target) return;
      if (!window.minicpmSettings || typeof window.minicpmSettings.useModelDir !== "function") return;
      pickerBtn.disabled = true;
      const origLabel = pickerBtn.textContent;
      pickerBtn.textContent = t("minicpmPickModelBusy");
      try {
        const ret = await window.minicpmSettings.useModelDir(target);
        if (ret && !ret.ok && ret.error) notifyError(t("minicpmReloadError") + ret.error);
        if (ret && ret.ok && ret.reloadError) notifyError(t("minicpmReloadError") + ret.reloadError);
      } finally {
        pickerBtn.disabled = false;
        pickerBtn.textContent = origLabel;
        void ctx.refreshAll();
      }
    }, { accent: true });
    pickerBtn.disabled = true;
    pickerCtl.appendChild(pickerBtn);
    pickerRow.appendChild(pickerCtl);
    rows.appendChild(pickerRow);

    // Populate the dropdown asynchronously. Done at render time so it picks
    // up new gguf files dropped into the models folder while the settings
    // window is open.
    if (typeof window.minicpmSettings.listLocalModels === "function") {
      window.minicpmSettings.listLocalModels().then((ret) => {
        const models = (ret && ret.models) || [];
        const currentPath = ((models.find((m) => m && m.current) || {}).path) || "";
        picker.innerHTML = "";
        if (models.length === 0) {
          picker.appendChild(el("option", { value: "" }, t("minicpmModelPathUnset")));
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
        // Selecting a DIFFERENT entry enables the Pick button.
        picker.addEventListener("change", () => {
          pickerBtn.disabled = !picker.value || picker.value === currentPath;
        });
        // Pre-select the currently loaded model (its ✓ flag makes the
        // row double as the old info row) — with nothing to switch to,
        // the button stays disabled.
        picker.value = currentPath || "";
        pickerBtn.disabled = !picker.value || picker.value === currentPath;
      }).catch(() => {
        picker.innerHTML = "";
        picker.appendChild(el("option", { value: "" }, t("minicpmNoAdditionalModels")));
      });
    }

    // ── User-added model folders row ──────────────────────────────────
    // Lets the user point the picker at any directory on disk — e.g.
    // D:\LM\models — without copying files into userData. Each listed
    // folder is scanned for *.gguf every time the picker is rendered.
    const foldersRow = el("div", { className: "row minicpm-model-folders-row" });
    const foldersText = el("div", { className: "row-text" });
    foldersText.appendChild(el("span", { className: "row-label" }, t("minicpmModelsFolderLabel")));
    foldersRow.appendChild(foldersText);

    const foldersCtl = el("div", { className: "row-control minicpm-model-folders-control" });
    const foldersListEl = el("div", { className: "minicpm-model-folders-list" });
    foldersCtl.appendChild(foldersListEl);

    const addFolderBtn = softBtn("+ " + t("minicpmModelsFolderAdd"), async () => {
      addFolderBtn.disabled = true;
      try {
        const ret = await window.minicpmSettings.addModelFolder();
        if (!ret || ret.canceled) return;
        if (!ret.ok) {
          notifyError(t("minicpmModelsFolderAddFailed") + (ret.error || ""));
          return;
        }
        renderFoldersList(ret.folders || []);
      } finally {
        addFolderBtn.disabled = false;
      }
    });
    foldersCtl.appendChild(addFolderBtn);
    foldersRow.appendChild(foldersCtl);
    rows.appendChild(foldersRow);

    // Render + populate folder list. Defined inline so it can refresh
    // when the user adds/removes folders without a full tab re-render.
    const renderFolderRow = (folder, idx) => {
      const row = el("div", { className: "minicpm-model-folder-item" });
      const path = el("span", { className: "minicpm-model-folder-path", title: folder }, folder);
      row.appendChild(path);
      const rm = el("button", { className: "soft-btn", style: { fontSize: "11px", padding: "2px 8px", marginLeft: "auto" } },
        t("minicpmModelsFolderRemove"));
      rm.addEventListener("click", async () => {
        try {
          await window.minicpmSettings.removeModelFolder(folder);
          const cur = await window.minicpmSettings.listModelFolders();
          renderFoldersList(cur.folders || []);
        } catch {}
      });
      row.appendChild(rm);
      return row;
    };
    const renderFoldersList = (folders) => {
      foldersListEl.innerHTML = "";
      if (!folders || folders.length === 0) {
        foldersListEl.appendChild(el("div", {
          style: { fontSize: "11px", color: "var(--text-secondary)", fontStyle: "italic", padding: "4px 0" },
        }, t("minicpmModelsFolderEmpty")));
        return;
      }
      for (const f of folders) foldersListEl.appendChild(renderFolderRow(f));
    };
    if (window.minicpmSettings && typeof window.minicpmSettings.listModelFolders === "function") {
      window.minicpmSettings.listModelFolders().then((ret) => {
        renderFoldersList((ret && ret.folders) || []);
      }).catch(() => renderFoldersList([]));
    }
    // ── Model path row (truncated + tooltip + two buttons) ────────────
    const pathRow = el("div", { className: "row minicpm-path-row" });
    const pathText = el("div", { className: "row-text" });
    pathText.appendChild(el("span", { className: "row-label" }, t("minicpmRowModelPath")));
    const pathDesc = el("span", {
      className: "row-desc minicpm-path-value" + (hasPath ? "" : " is-unset"),
    }, truncated);
    if (hasPath) pathDesc.setAttribute("title", modelDir);
    pathText.appendChild(pathDesc);
    pathRow.appendChild(pathText);

    const ctl = el("div", { className: "row-control minicpm-path-actions" });
    const showBtn = softBtn(modelPathOpenLabel(), async () => {
      const ret = await window.minicpmSettings.openModelDir();
      if (ret && !ret.ok) notifyError(ret.error || t("minicpmOpenModelDirFailed"));
    });
    if (!hasPath) showBtn.disabled = true;
    const changeLabel = t("minicpmChangeModel");
    // The IPC handler also kicks off /api/load-model after persisting, so
    // resolution may take 5–30s depending on model size. Show a busy state
    // on both buttons so the user gets immediate feedback rather than
    // staring at a frozen dialog while llama-server re-spawns.
    const changeBtn = softBtn(changeLabel, async () => {
      if (changeBtn.disabled) return;
      showBtn.disabled = true;
      changeBtn.disabled = true;
      changeBtn.classList.add("is-busy");
      changeBtn.textContent = t("minicpmChangeModelBusy");
      let ret = null;
      try {
        ret = await window.minicpmSettings.pickModelDir();
      } catch (err) {
        notifyError(t("minicpmReloadError") + (err && err.message || err));
      }
      // refreshAll() rebuilds the model section from scratch (replacing
      // these buttons), so restoring the busy state explicitly is only
      // necessary on the canceled / error paths.
      if (ret && ret.ok) {
        if (ret.reloadError) notifyError(t("minicpmReloadError") + ret.reloadError);
        void ctx.refreshAll();
        return;
      }
      if (ret && !ret.canceled && ret.error) notifyError(ret.error);
      changeBtn.classList.remove("is-busy");
      changeBtn.textContent = changeLabel;
      changeBtn.disabled = false;
      showBtn.disabled = !hasPath;
    }, { accent: true });
    ctl.appendChild(showBtn);
    ctl.appendChild(changeBtn);
    pathRow.appendChild(ctl);
    rows.appendChild(pathRow);

    box.appendChild(section);
  }

  // ── Engine (llama.cpp binary) section ─────────────────────────────────
  // One-click self-update of the inference engine: check the official
  // GitHub release, then apply (download → swap → auto-restart). The
  // sidecar endpoints live in gateway/sidecar_updater.py.
  function renderEngineSection(box, ctx) {
    // Never rebuild while an update is in flight — the health tick
    // calls this repeatedly and would tear down the live progress bar.
    if (engineUpdating && box.firstChild) return;
    // Must clear before painting — refreshAll + the health tick both call
    // this, and without the reset each pass appended a duplicate section.
    box.innerHTML = "";
    box.appendChild(sectionTitle(t("minicpmSectionEngine")));
    const engSection = helpers.buildSection("", []);
    const engRows = engSection.querySelector(".section-rows");

    const engInfoRow = el("div", { className: "row minicpm-info-row" });
    const engInfoText = el("div", { className: "row-text" });
    engInfoText.appendChild(el("span", { className: "row-label" }, t("minicpmRowEngineVersion")));
    engInfoText.appendChild(el("span", { className: "row-desc" }, t("minicpmEngineSectionDesc")));
    engInfoRow.appendChild(engInfoText);
    const engInfoVal = el("div", {
      className: "row-control minicpm-info-value",
    }, t("minicpmEngineUnchecked"));
    engInfoRow.appendChild(engInfoVal);
    engRows.appendChild(engInfoRow);

    const engBtnRow = el("div", { className: "row" });
    const engBtnCtl = el("div", { className: "row-control minicpm-path-actions" });

    // Live download progress bar (hidden until an update starts).
    const progRow = el("div", { style: { display: "none", margin: "10px 0 0 0" } });
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
    engRows.appendChild(progRow);

    const updateBtn = softBtn(t("minicpmEngineUpdateButton"), async () => {
      if (updateBtn.disabled) return;
      updateBtn.disabled = true;
      checkBtn.disabled = true;
      updateBtn.textContent = t("minicpmEngineUpdateBusy");
      engInfoVal.textContent = "…";
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
      const unsub = window.minicpmSettings.onEngineUpdateProgress((ev) => {
        if (ev.phase === "start") {
          progFill.style.width = "0%";
          progText.textContent = t("minicpmEngineCheckBusy");
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
          progText.textContent = t("minicpmEngineUpdateBusy");
        } else if (ev.phase === "complete" || ev.phase === "reloaded") {
          progFill.style.width = "100%";
        } else if (ev.phase === "error") {
          progText.textContent = (ev.message || t("minicpmEngineUpdateFailed"));
        }
      });
      try {
        const ret = await window.minicpmSettings.engineUpdateApply();
        if (ret && ret.status === "ok") {
          engInfoVal.textContent = t("minicpmEngineUpdateDone");
          progText.textContent = "✓ " + t("minicpmEngineUpdateDone");
          engineCheckCache = null; // local build changed — re-check next render
        } else {
          const msg = t("minicpmEngineUpdateFailed") + ((ret && ret.message) || "");
          engInfoVal.textContent = msg;
          progText.textContent = msg;
        }
        updateBtn.disabled = true;
      } finally {
        if (typeof unsub === "function") unsub();
        updateBtn.textContent = t("minicpmEngineUpdateButton");
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
    const dirBtn = softBtn(t("minicpmEngineUpdateDirButton"), async () => {
      if (dirBtn.disabled) return;
      dirBtn.disabled = true;
      checkBtn.disabled = true;
      updateBtn.disabled = true;
      dirBtn.textContent = t("minicpmEngineCheckBusy");
      progRow.style.display = "";
      progFill.style.width = "0%";
      progText.textContent = "…";
      engineUpdating = true;
      const unsub = window.minicpmSettings.onEngineUpdateProgress((ev) => {
        if (ev.phase === "start") {
          progText.textContent = t("minicpmEngineUpdateDirVerifying");
        } else if (ev.phase === "verify") {
          progFill.style.width = "50%";
          progText.textContent = `build ${ev.build}`;
        } else if (ev.phase === "swap") {
          progFill.style.width = "90%";
          progText.textContent = t("minicpmEngineUpdateBusy");
        } else if (ev.phase === "complete" || ev.phase === "reloaded") {
          progFill.style.width = "100%";
        } else if (ev.phase === "error") {
          progText.textContent = (ev.message || t("minicpmEngineUpdateFailed"));
        }
      });
      try {
        const ret = await window.minicpmSettings.engineUpdateApplyDir();
        if (ret && ret.status === "canceled") {
          progRow.style.display = "none";
          return;
        }
        if (ret && ret.status === "ok") {
          engInfoVal.textContent = t("minicpmEngineUpdateDone");
          progText.textContent = "✓ " + t("minicpmEngineUpdateDone");
          engineCheckCache = null;
        } else {
          const msg = t("minicpmEngineUpdateFailed") + ((ret && ret.message) || "");
          engInfoVal.textContent = msg;
          progText.textContent = msg;
        }
      } finally {
        if (typeof unsub === "function") unsub();
        dirBtn.textContent = t("minicpmEngineUpdateDirButton");
        dirBtn.disabled = false;
        checkBtn.disabled = false;
        engineUpdating = false;
        setTimeout(() => { progRow.style.display = "none"; }, 2500);
      }
    });

    const checkBtn = softBtn(t("minicpmEngineCheckButton"), async () => {
      if (checkBtn.disabled) return;
      await runEngineCheck();
    });

    // Auto-check on every render so the user always sees which engine
    // build is installed — no need to click anything first.
    //
    // Two steps: the LOCAL build comes from llama-server --version
    // directly (works even when the sidecar is down); the REMOTE check
    // goes through the sidecar + GitHub and only upgrades the line.
    let localBuild = null;
    async function fetchLocalVersion() {
      try {
        const ret = await window.minicpmSettings.engineLocalVersion();
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
        engInfoVal.textContent = `build ${localBuild}`;
        updateBtn.disabled = true;
      }
      // 2. Remote check (sidecar + GitHub) upgrades the line.
      checkBtn.disabled = true;
      checkBtn.textContent = t("minicpmEngineCheckBusy");
      try {
        const ret = await window.minicpmSettings.engineUpdateCheck();
        if (!ret || ret.status !== "ok" || !ret.info) {
          // Surface the actual reason (connection refused / timeout) so
          // the user can tell "sidecar not running" apart from GitHub
          // being unreachable.
          const reason = (ret && ret.message) || (ret && ret.error) || "";
          if (haveLocal) {
            engInfoVal.textContent = `build ${localBuild}（${t("minicpmEngineUnreachable")}${reason ? `: ${reason}` : ""}）`;
          } else {
            engInfoVal.textContent = t("minicpmEngineUnreachable") + (reason ? `（${reason}）` : "");
          }
          return;
        }
        const info = ret.info;
        let text;
        let hasUpdate = false;
        // NOTE: settings t() is a plain dict lookup (no {param} support) —
        // substitute placeholders manually.
        const localLabel = String(info.local_build ?? localBuild ?? "?");
        if (info.error) {
          // Remote check failed (e.g. GitHub unreachable). Show the
          // local build + the reason — never a silent "up to date".
          text = `build ${localLabel}（${info.error}）`;
        } else if (info.available) {
          text = t("minicpmEngineUpdateAvailable")
            .replace("{remote}", info.remote_tag || "?")
            .replace("{local}", localLabel);
          hasUpdate = true;
        } else {
          text = t("minicpmEngineUpToDate").replace("{local}", localLabel);
        }
        engInfoVal.textContent = text;
        updateBtn.disabled = !hasUpdate;
        engineCheckCache = { at: Date.now(), text, hasUpdate };
      } finally {
        checkBtn.disabled = false;
        checkBtn.textContent = t("minicpmEngineCheckButton");
      }
    }

    engBtnCtl.appendChild(checkBtn);
    engBtnCtl.appendChild(updateBtn);
    engBtnCtl.appendChild(dirBtn);
    engBtnRow.appendChild(engBtnCtl);
    engRows.appendChild(engBtnRow);

    box.appendChild(engSection);

    // Auto-check result is cached for a minute: the health tick rebuilds
    // this section repeatedly, and re-hitting the GitHub API every tick
    // would flicker "…" and spam the network.
    if (engineCheckCache && Date.now() - engineCheckCache.at < 60000) {
      engInfoVal.textContent = engineCheckCache.text;
      updateBtn.disabled = !engineCheckCache.hasUpdate;
    } else {
      void runEngineCheck();
    }
  }

  // ── Adapter (LoRA) section ────────────────────────────────────────────
  //
  // Lists every *.gguf the gateway finds in `<userData>/adapters/`, lets
  // the user pick one (radio-style, "Base" included) and refresh after
  // dropping a new file in via the "Open adapters folder" shortcut. The
  // actual activation goes through the existing IPC pipeline
  // (`minicpm-settings:load-adapter`) so the in-bubble notification +
  // chat-history-reset behaviour stays consistent with the in-chat
  // command UX.
  //
  // Each chip also surfaces the manifest's friendly displayName and
  // aliases (for chat keyword routing); a small gear icon opens an
  // inline editor for both. User-uploaded entries get an extra trash
  // button. A dedicated "Upload .gguf" button on the action row pipes
  // through the upload IPC handler which copies the file into the
  // user-writable adapters dir and writes a fresh manifest entry.
  const SVG_GEAR =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" width="100%" height="100%">' +
    '<circle cx="12" cy="12" r="2.6"/>' +
    '<path d="M19.4 15a1.7 1.7 0 0 0 .34 1.87l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.7 1.7 0 0 0-1.87-.34 1.7 1.7 0 0 0-1.03 1.56V21a2 2 0 1 1-4 0v-.09a1.7 1.7 0 0 0-1.11-1.56 1.7 1.7 0 0 0-1.87.34l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.7 1.7 0 0 0 .34-1.87 1.7 1.7 0 0 0-1.56-1.03H3a2 2 0 1 1 0-4h.09a1.7 1.7 0 0 0 1.56-1.11 1.7 1.7 0 0 0-.34-1.87l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.7 1.7 0 0 0 1.87.34h.01a1.7 1.7 0 0 0 1.03-1.56V3a2 2 0 1 1 4 0v.09a1.7 1.7 0 0 0 1.03 1.56 1.7 1.7 0 0 0 1.87-.34l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.7 1.7 0 0 0-.34 1.87v.01a1.7 1.7 0 0 0 1.56 1.03H21a2 2 0 1 1 0 4h-.09a1.7 1.7 0 0 0-1.56 1.03z"/>' +
    "</svg>";
  const SVG_TRASH =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" width="100%" height="100%">' +
    '<path d="M3 6h18"/>' +
    '<path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>' +
    '<path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>' +
    "</svg>";

  // Tiny inline "modal" rendered in-place under the chip. We don't pull
  // a real dialog system in because there isn't one in this codebase —
  // the goal is to stay self-contained and visually consistent with
  // the existing row-control aesthetic.
  function openAdapterEditor({ host, initial, busyLabel, onSave }) {
    // Idempotent: replace any pre-existing editor for the same host.
    host.querySelectorAll(".minicpm-adapter-editor").forEach((n) => n.remove());

    const wrap = el("div", { className: "minicpm-adapter-editor" });
    const nameRow = el("label", { className: "minicpm-adapter-editor-field" });
    nameRow.appendChild(el("span", { className: "minicpm-adapter-editor-label" }, t("minicpmAdapterDisplayNameLabel")));
    const nameInput = el("input", {
      type: "text",
      className: "minicpm-adapter-editor-input",
      placeholder: t("minicpmAdapterDisplayNamePlaceholder"),
    });
    nameInput.value = (initial && initial.displayName) || "";
    nameRow.appendChild(nameInput);
    wrap.appendChild(nameRow);

    const aliasRow = el("label", { className: "minicpm-adapter-editor-field" });
    aliasRow.appendChild(el("span", { className: "minicpm-adapter-editor-label" }, t("minicpmAdapterAliasesLabel")));
    const aliasInput = el("input", {
      type: "text",
      className: "minicpm-adapter-editor-input",
      placeholder: t("minicpmAdapterAliasesPlaceholder"),
    });
    aliasInput.value = (initial && Array.isArray(initial.aliases) ? initial.aliases.join(", ") : "") || "";
    aliasRow.appendChild(aliasInput);
    wrap.appendChild(aliasRow);

    const buttons = el("div", { className: "minicpm-adapter-editor-buttons" });
    const cancelBtn = softBtn(t("minicpmAdapterCancel"), () => { wrap.remove(); });
    const saveBtn = softBtn(t("minicpmAdapterSave"), async () => {
      const displayName = nameInput.value.trim();
      const aliases = aliasInput.value
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean);
      saveBtn.disabled = true;
      cancelBtn.disabled = true;
      const prevSaveLabel = saveBtn.textContent;
      saveBtn.textContent = busyLabel || t("minicpmAdapterApplying");
      try {
        const result = await onSave({ displayName, aliases });
        if (!result || result.ok === false) {
          const msg = (result && (result.error || result.message)) || "";
          if (ops && typeof ops.showToast === "function") {
            ops.showToast(t("minicpmAdapterSaveFailed") + msg, { error: true });
          } else {
            notifyError(t("minicpmAdapterSaveFailed") + msg);
          }
          saveBtn.disabled = false;
          cancelBtn.disabled = false;
          saveBtn.textContent = prevSaveLabel;
          return;
        }
        wrap.remove();
      } catch (err) {
        if (ops && typeof ops.showToast === "function") {
          ops.showToast(t("minicpmAdapterSaveFailed") + (err && err.message || ""), { error: true });
        }
        saveBtn.disabled = false;
        cancelBtn.disabled = false;
        saveBtn.textContent = prevSaveLabel;
      }
    }, { accent: true });
    buttons.appendChild(cancelBtn);
    buttons.appendChild(saveBtn);
    wrap.appendChild(buttons);

    host.appendChild(wrap);
    // Auto-focus the display name input so keyboard users can jump
    // straight into typing.
    try { nameInput.focus(); nameInput.select(); } catch {}
  }

  async function renderAdapterSection(box, ctx) {
    box.innerHTML = "";
    let payload = null;
    try { payload = await window.minicpmSettings.listAdapters(); } catch {}
    const items = (payload && Array.isArray(payload.items)) ? payload.items : [];
    const currentPath = (payload && payload.current) || null;

    box.appendChild(sectionTitle(t("minicpmSectionAdapter")));
    const section = helpers.buildSection("", []);
    const rows = section.querySelector(".section-rows");

    // ── Row 1: radio list (Base + each adapter) ──────────────────────
    const listRow = el("div", { className: "row minicpm-adapter-row" });
    const listText = el("div", { className: "row-text" });
    listText.appendChild(el("span", { className: "row-label" }, t("minicpmRowAdapter")));
    listText.appendChild(el("span", { className: "row-desc" }, t("minicpmRowAdapterDesc")));
    listRow.appendChild(listText);

    const choices = el("div", { className: "row-control minicpm-adapter-choices" });
    const radioName = "minicpm-adapter-radio";

    function buildChoice({ label, value, selected, sub, item }) {
      const wrap = el("label", { className: "minicpm-adapter-choice" + (selected ? " selected" : "") });
      if (item && item.missing) wrap.classList.add("is-missing");
      const input = el("input", {
        type: "radio",
        name: radioName,
      });
      if (selected) input.setAttribute("checked", "checked");
      input.dataset.path = value === null ? "" : value;
      if (item && item.missing) input.disabled = true;
      wrap.appendChild(input);
      const txt = el("span", { className: "minicpm-adapter-choice-label" }, label);
      // Tooltip surfaces filename + aliases so the abbreviated chip
      // label remains readable for users with multiple adapters that
      // share a similar friendly name.
      if (item) {
        const aliasStr = Array.isArray(item.aliases) && item.aliases.length
          ? "\n" + item.aliases.join(", ")
          : "";
        wrap.title = `${item.name || ""}${aliasStr}`;
      }
      wrap.appendChild(txt);
      if (sub) {
        const tag = el("span", { className: "minicpm-adapter-choice-tag" }, sub);
        wrap.appendChild(tag);
      }
      // Mark missing-file entries so the user can spot stale manifest
      // refs without digging into logs.
      if (item && item.missing) {
        const tag = el("span", { className: "minicpm-adapter-choice-tag is-missing" }, t("minicpmAdapterMissingTag"));
        wrap.appendChild(tag);
      }

      input.addEventListener("change", async () => {
        if (!input.checked) return;
        const targetPath = input.dataset.path || null;
        for (const inp of choices.querySelectorAll(`input[name="${radioName}"]`)) {
          inp.disabled = true;
        }
        const prevLabel = txt.textContent;
        txt.textContent = t("minicpmAdapterApplying");
        try {
          const result = await window.minicpmSettings.loadAdapter(targetPath);
          if (!result || (!result.ok && !result.noop)) {
            const msg = (result && (result.error || result.message)) || "";
            if (ops && typeof ops.showToast === "function") {
              ops.showToast(t("minicpmAdapterApplyFailed") + msg, { error: true });
            } else {
              notifyError(t("minicpmAdapterApplyFailed") + msg);
            }
          }
        } catch (err) {
          if (ops && typeof ops.showToast === "function") {
            ops.showToast(t("minicpmAdapterApplyFailed") + (err && err.message || ""), { error: true });
          }
        } finally {
          txt.textContent = prevLabel;
          void ctx.refreshAll();
        }
      });

      // Per-chip controls (edit + optional remove). Only adapters that
      // have an id (i.e. are tracked in the manifest, including
      // bundled presets) can be edited. External .gguf files with no
      // manifest entry would be edited on the next save anyway, so
      // we let them through too via the `external:<path>` synthetic
      // id assigned by the IPC merge layer.
      if (item && item.id) {
        const controls = el("span", { className: "minicpm-adapter-choice-controls" });

        const editBtn = el("button", {
          type: "button",
          className: "minicpm-adapter-icon-btn",
          "aria-label": t("minicpmAdapterEditName"),
          title: t("minicpmAdapterEditName"),
        });
        editBtn.innerHTML = SVG_GEAR;
        editBtn.addEventListener("click", (ev) => {
          ev.preventDefault();
          ev.stopPropagation();
          openAdapterEditor({
            host: wrap,
            initial: { displayName: item.displayName, aliases: item.aliases },
            onSave: async ({ displayName, aliases }) => {
              const r = await window.minicpmSettings.renameAdapter({
                id: item.id,
                displayName,
                aliases,
              });
              if (r && r.ok) void ctx.refreshAll();
              return r;
            },
          });
        });
        controls.appendChild(editBtn);

        if (item.source === "user-upload") {
          const trashBtn = el("button", {
            type: "button",
            className: "minicpm-adapter-icon-btn is-danger",
            "aria-label": t("minicpmAdapterRemove"),
            title: t("minicpmAdapterRemove"),
          });
          trashBtn.innerHTML = SVG_TRASH;
          trashBtn.addEventListener("click", async (ev) => {
            ev.preventDefault();
            ev.stopPropagation();
            const ok = window.confirm(t("minicpmAdapterRemoveConfirm"));
            if (!ok) return;
            try {
              const r = await window.minicpmSettings.removeAdapter({
                id: item.id,
                deleteFile: true,
              });
              if (!r || r.ok === false) {
                const msg = (r && (r.error || r.message)) || "";
                if (ops && typeof ops.showToast === "function") {
                  ops.showToast(t("minicpmAdapterSaveFailed") + msg, { error: true });
                }
              }
            } catch (err) {
              if (ops && typeof ops.showToast === "function") {
                ops.showToast(t("minicpmAdapterSaveFailed") + (err && err.message || ""), { error: true });
              }
            } finally {
              void ctx.refreshAll();
            }
          });
          controls.appendChild(trashBtn);
        }

        wrap.appendChild(controls);
      }

      return wrap;
    }

    // "Base" is always first so users always have a way back to a clean
    // model even if every adapter on disk is broken.
    choices.appendChild(buildChoice({
      label: t("minicpmAdapterBase"),
      value: null,
      selected: !currentPath,
    }));

    if (items.length === 0) {
      const empty = el("div", { className: "minicpm-adapter-empty row-desc" }, t("minicpmAdapterEmpty"));
      choices.appendChild(empty);
    } else {
      for (const item of items) {
        const label = item.displayName || item.name;
        // Show a persona pill when meaningful (not "default"/"custom").
        const persona = item.persona && item.persona !== "default" && item.persona !== "custom"
          ? item.persona
          : null;
        choices.appendChild(buildChoice({
          label,
          value: item.path,
          selected: item.path === currentPath,
          sub: persona,
          item,
        }));
      }
    }

    listRow.appendChild(choices);
    rows.appendChild(listRow);

    // ── Row 2: action buttons (upload + open folder + refresh) ───────
    const actionsRow = el("div", { className: "row minicpm-adapter-actions-row" });
    const actionsText = el("div", { className: "row-text" });
    actionsText.appendChild(el("span", { className: "row-label" }, " "));
    actionsRow.appendChild(actionsText);
    const actions = el("div", { className: "row-control minicpm-path-actions" });
    actions.appendChild(softBtn(t("minicpmAdapterUpload"), async () => {
      // Inline pre-prompt for displayName / aliases. Using window.prompt
      // keeps this dependency-free; the modal editor on each chip is
      // available for post-upload tweaking.
      const displayName = window.prompt(t("minicpmAdapterDisplayNameLabel"), "");
      if (displayName === null) return;
      const aliasesRaw = window.prompt(t("minicpmAdapterAliasesLabel"), "");
      if (aliasesRaw === null) return;
      const aliases = aliasesRaw.split(",").map((s) => s.trim()).filter(Boolean);
      try {
        const r = await window.minicpmSettings.uploadAdapter({ displayName: displayName.trim(), aliases });
        if (!r || (r.ok === false && !r.canceled)) {
          const msg = (r && (r.error || r.message)) || "";
          if (ops && typeof ops.showToast === "function") {
            ops.showToast(t("minicpmAdapterUploadFailed") + msg, { error: true });
          } else {
            notifyError(t("minicpmAdapterUploadFailed") + msg);
          }
        }
      } catch (err) {
        if (ops && typeof ops.showToast === "function") {
          ops.showToast(t("minicpmAdapterUploadFailed") + (err && err.message || ""), { error: true });
        }
      } finally {
        void ctx.refreshAll();
      }
    }));
    actions.appendChild(softBtn(t("minicpmAdapterOpenDir"), async () => {
      try {
        const r = await window.minicpmSettings.openAdapterDir();
        if (r && !r.ok) notifyError(r.error || t("minicpmAdapterOpenDirFailed"));
      } catch (err) {
        notifyError(t("minicpmAdapterOpenDirFailed") + " " + (err && err.message || ""));
      }
    }));
    actions.appendChild(softBtn(t("minicpmAdapterRefresh"), () => {
      void ctx.refreshAll();
    }, { accent: true }));
    actionsRow.appendChild(actions);
    rows.appendChild(actionsRow);

    box.appendChild(section);
  }

  // ── Advanced (collapsible) — restart Sidecar + open logs ──────────────
  //
  // Hand-rolled instead of using helpers.buildCollapsibleGroup so the
  // disclosure trigger can sit in the small-caps section-title style. The
  // body is just a standard section-rows block of two rows; we toggle its
  // visibility with display:none rather than a height animation because
  // the row count is tiny (2) and reflow is instant.
  function renderAdvancedSection(box, ctx) {
    box.innerHTML = "";
    const wrap = el("section", { className: "section minicpm-advanced-section" });

    const trigger = el("button", {
      type: "button",
      className: "minicpm-advanced-trigger" + (advancedExpanded ? " open" : ""),
      "aria-expanded": advancedExpanded ? "true" : "false",
    });
    const chev = el("span", { className: "minicpm-advanced-chevron", "aria-hidden": "true" });
    chev.innerHTML = SVG_CHEVRON;
    trigger.appendChild(chev);
    trigger.appendChild(el("span", { className: "section-title minicpm-advanced-title" }, t("minicpmSectionAdvanced")));
    wrap.appendChild(trigger);

    const section = helpers.buildSection("", []);
    section.classList.add("minicpm-advanced-body");
    const rows = section.querySelector(".section-rows");

    rows.appendChild(buildAdvancedRow({
      icon: SVG_RESTART,
      title: t("minicpmActionRestartSidecar"),
      desc: t("minicpmActionRestartSidecarDesc"),
      busyLabel: t("minicpmActionRestartSidecarBusy"),
      onClick: async () => {
        try { await window.minicpmSettings.restartSidecar(); } catch {}
        void ctx.refreshAll();
      },
    }));
    rows.appendChild(buildAdvancedRow({
      icon: SVG_LOG,
      title: t("minicpmActionOpenLogs"),
      desc: t("minicpmActionOpenLogsDesc"),
      onClick: async () => {
        const ret = await window.minicpmSettings.openLogsDir();
        if (ret && !ret.ok) notifyError(ret.error || t("minicpmActionOpenLogsFailed"));
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
    const row = el("div", { className: "row minicpm-advanced-row" });
    const iconBox = el("span", { className: "minicpm-advanced-row-icon", "aria-hidden": "true" });
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
    if (!window.minicpmSettings || !ctx) return;
    await probeHealth(ctx);
    syncStatusPill(ctx);
    await renderBehaviorSection(ctx.behaviorBox, ctx);
    renderModelSection(ctx.modelBox, ctx);
    renderEngineSection(ctx.engineBox, ctx);
    await renderAdapterSection(ctx.adapterBox, ctx);
    renderAdvancedSection(ctx.advancedBox, ctx);
  }

  function nextHealthDelay(ctx) {
    if (!ctx.everHealthy && ctx.fastAttemptsLeft > 0) return HEALTH_INTERVAL_MS_FAST;
    return HEALTH_INTERVAL_MS_SLOW;
  }

  // The polling loop refreshes only the status pill + model path (cheap)
  // so switches and the advanced collapsible state stay put across ticks.
  function startHealthPolling(ctx) {
    if (healthTimer) {
      clearTimeout(healthTimer);
      healthTimer = null;
    }
    const tick = async () => {
      healthTimer = null;
      if (!mounted || document.hidden || core.state.activeTab !== "minicpm") return;
      const wasHealthy = ctx.everHealthy;
      await probeHealth(ctx);
      syncStatusPill(ctx);
      // Path may have switched after a load-model — keep the model card
      // honest, but never re-render Behavior/Advanced (would lose focus).
      renderModelSection(ctx.modelBox, ctx);
      renderEngineSection(ctx.engineBox, ctx);
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
      behaviorBox: el("div", { className: "minicpm-section-box" }),
      modelBox: el("div", { className: "minicpm-section-box" }),
      engineBox: el("div", { className: "minicpm-section-box" }),
      adapterBox: el("div", { className: "minicpm-section-box" }),
      advancedBox: el("div", { className: "minicpm-section-box" }),
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

    if (!window.minicpmSettings) {
      parent.appendChild(ctx.headerBox);
      parent.appendChild(el("div", { className: "row-desc" }, t("minicpmIpcUnavailable")));
      return;
    }

    parent.appendChild(ctx.headerBox);
    parent.appendChild(ctx.behaviorBox);
    parent.appendChild(ctx.modelBox);
    parent.appendChild(ctx.engineBox);
    parent.appendChild(ctx.adapterBox);
    parent.appendChild(ctx.advancedBox);

    mounted = true;
    visibilityHandler = () => {
      if (document.hidden || core.state.activeTab !== "minicpm") {
        if (healthTimer) {
          clearTimeout(healthTimer);
          healthTimer = null;
        }
      } else {
        armFastProbes(ctx);
        void (async () => {
          await probeHealth(ctx);
          syncStatusPill(ctx);
          renderModelSection(ctx.modelBox, ctx);
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
    core.tabs.minicpm = {
      render: (parent) => { void render(parent); },
    };
  }

  root.ClawdSettingsTabMinicpm = { init };
})(typeof globalThis !== "undefined" ? globalThis : window);
