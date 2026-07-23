"use strict";

(function initSettingsTabTheme(root) {
  const PREVIEW_TARGET_CONTENT_RATIO = 0.55;
  const BUILTIN_THEME_PRIORITY = ["cybercat", "hamster"];

  let state = null;
  let runtime = null;
  let helpers = null;
  let ops = null;
  let readers = null;

  function t(key) {
    return helpers.t(key);
  }

  function render(parent) {
    const h1 = document.createElement("h1");
    h1.textContent = t("themeTitle");
    parent.appendChild(h1);

    const subtitle = document.createElement("p");
    subtitle.className = "subtitle";
    subtitle.textContent = t("themeSubtitle");
    parent.appendChild(subtitle);
    parent.appendChild(buildThemeActions());

    if (runtime.themeList === null) {
      const loading = document.createElement("div");
      loading.className = "placeholder-desc";
      parent.appendChild(loading);
      ops.fetchThemes().then(() => {
        if (state.activeTab === "theme") ops.requestRender({ content: true });
      });
      return;
    }

    if (runtime.themeList.length === 0) {
      const empty = document.createElement("div");
      empty.className = "placeholder";
      empty.innerHTML = `<div class="placeholder-desc">${helpers.escapeHtml(t("themeEmpty"))}</div>`;
      parent.appendChild(empty);
      return;
    }

    for (const section of getThemeSections(runtime.themeList)) {
      const sectionEl = document.createElement("section");
      sectionEl.className = "theme-section";
      sectionEl.setAttribute("aria-labelledby", `theme-section-${section.id}`);

      const title = document.createElement("h2");
      title.id = `theme-section-${section.id}`;
      title.className = "theme-section-title";
      title.textContent = section.title;
      sectionEl.appendChild(title);

      const grid = document.createElement("div");
      grid.className = "theme-grid";
      for (const theme of section.themes) {
        grid.appendChild(buildThemeCard(theme));
      }
      sectionEl.appendChild(grid);
      parent.appendChild(sectionEl);
    }
  }

  function getThemeSections(themes) {
    const groups = {
      builtin: [],
      importedCodexPets: [],
      user: [],
    };
    for (const theme of themes || []) {
      if (theme && theme.builtin) groups.builtin.push(theme);
      else if (theme && theme.managedCodexPet) groups.importedCodexPets.push(theme);
      else groups.user.push(theme);
    }
    groups.builtin.sort((a, b) => getBuiltinThemePriority(a) - getBuiltinThemePriority(b));
    return [
      { id: "builtin", title: t("themeGroupBuiltIn"), themes: groups.builtin },
      { id: "imported-codex-pets", title: t("themeGroupImportedCodexPets"), themes: groups.importedCodexPets },
      { id: "user", title: t("themeGroupUserThemes"), themes: groups.user },
    ].filter((section) => section.themes.length > 0);
  }

  function getBuiltinThemePriority(theme) {
    const idx = BUILTIN_THEME_PRIORITY.indexOf(theme && theme.id);
    return idx >= 0 ? idx : BUILTIN_THEME_PRIORITY.length;
  }

  function localizeField(value) {
    if (value == null) return "";
    if (typeof value === "string") return value;
    if (typeof value === "object") {
      const lang = readers.getLang();
      if (value[lang]) return value[lang];
      if (value.en) return value.en;
      if (value.zh) return value.zh;
      const firstKey = Object.keys(value)[0];
      if (firstKey) return value[firstKey];
    }
    return "";
  }

  function applyThemePreviewScale(el, contentRatio) {
    if (!Number.isFinite(contentRatio) || contentRatio <= 0) return;
    if (contentRatio <= PREVIEW_TARGET_CONTENT_RATIO) return;
    const scale = PREVIEW_TARGET_CONTENT_RATIO / contentRatio;
    const pct = `${(scale * 100).toFixed(2)}%`;
    el.style.maxWidth = pct;
    el.style.maxHeight = pct;
  }

  function applyThemePreviewOffset(el, offsetPct) {
    if (!offsetPct) return;
    const { x, y } = offsetPct;
    if (!Number.isFinite(x) || !Number.isFinite(y)) return;
    if (Math.abs(x) < 0.5 && Math.abs(y) < 0.5) return;
    el.style.transform = `translate(${x.toFixed(2)}%, ${y.toFixed(2)}%)`;
  }

  function getCodexPetPreviewAtlasUrl(theme) {
    return theme
      && theme.codexPet
      && typeof theme.codexPet.previewAtlasUrl === "string"
      && theme.codexPet.previewAtlasUrl;
  }

  function buildCodexPetAtlasPreview(theme) {
    const frame = document.createElement("span");
    frame.className = "theme-thumb-atlas-frame";
    applyThemePreviewScale(frame, theme.previewContentRatio);
    applyThemePreviewOffset(frame, theme.previewContentOffsetPct);

    const img = document.createElement("img");
    img.src = getCodexPetPreviewAtlasUrl(theme);
    img.alt = "";
    img.draggable = false;
    frame.appendChild(img);
    return frame;
  }

  function buildThemePreviewMedia(theme) {
    if (theme.managedCodexPet && getCodexPetPreviewAtlasUrl(theme)) {
      return buildCodexPetAtlasPreview(theme);
    }
    const img = document.createElement("img");
    img.src = theme.previewFileUrl;
    img.alt = "";
    img.draggable = false;
    applyThemePreviewScale(img, theme.previewContentRatio);
    applyThemePreviewOffset(img, theme.previewContentOffsetPct);
    return img;
  }

  function getThemeCapabilityBadgeLabels(theme) {
    const caps = theme && theme.capabilities;
    if (!caps || typeof caps !== "object") return [];
    const badges = [];
    if (caps.idleMode === "tracked") badges.push(t("themeCapabilityTracked"));
    else if (caps.idleMode === "animated") badges.push(t("themeCapabilityAnimated"));
    else if (caps.idleMode === "static") badges.push(t("themeCapabilityStatic"));
    if (caps.miniMode) badges.push(t("themeCapabilityMini"));
    return badges;
  }

  function buildThemeActions() {
    const row = document.createElement("div");
    row.className = "theme-actions";

    const codexGroup = buildThemeActionGroup(t("themeActionGroupCodexPets"));
    const importBtn = document.createElement("button");
    importBtn.type = "button";
    importBtn.className = "soft-btn";
    importBtn.textContent = t("themeImportPetZip");
    importBtn.disabled = !!runtime.codexPetZipImportPending
      || !window.settingsAPI
      || typeof window.settingsAPI.importCodexPetZip !== "function";
    if (runtime.codexPetZipImportPending) importBtn.classList.add("pending");
    importBtn.addEventListener("click", handleImportCodexPetZip);
    codexGroup.buttons.appendChild(importBtn);

    const refreshBtn = document.createElement("button");
    refreshBtn.type = "button";
    refreshBtn.className = "soft-btn";
    refreshBtn.textContent = t("themeRefreshImportedPets");
    refreshBtn.disabled = !!runtime.codexPetsRefreshPending
      || !window.settingsAPI
      || typeof window.settingsAPI.refreshCodexPets !== "function";
    if (runtime.codexPetsRefreshPending) refreshBtn.classList.add("pending");
    refreshBtn.addEventListener("click", handleRefreshCodexPets);
    codexGroup.buttons.appendChild(refreshBtn);
    row.appendChild(codexGroup.group);

    const userThemeGroup = buildThemeActionGroup(t("themeActionGroupUserThemes"));
    const importThemeBtn = document.createElement("button");
    importThemeBtn.type = "button";
    importThemeBtn.className = "soft-btn";
    importThemeBtn.textContent = t("themeImportUserThemeZip");
    importThemeBtn.title = t("themeImportUserThemeZipHint");
    importThemeBtn.disabled = !!runtime.userThemeZipImportPending
      || !window.settingsAPI
      || typeof window.settingsAPI.importUserThemeZip !== "function";
    if (runtime.userThemeZipImportPending) importThemeBtn.classList.add("pending");
    importThemeBtn.addEventListener("click", handleImportUserThemeZip);
    userThemeGroup.buttons.appendChild(importThemeBtn);

    const userThemeFolderBtn = document.createElement("button");
    userThemeFolderBtn.type = "button";
    userThemeFolderBtn.className = "soft-btn";
    userThemeFolderBtn.textContent = t("themeOpenUserThemesFolder");
    userThemeFolderBtn.disabled = !window.settingsAPI
      || typeof window.settingsAPI.openUserThemesDir !== "function";
    userThemeFolderBtn.addEventListener("click", handleOpenUserThemesFolder);
    userThemeGroup.buttons.appendChild(userThemeFolderBtn);

    const refreshThemesBtn = document.createElement("button");
    refreshThemesBtn.type = "button";
    refreshThemesBtn.className = "soft-btn";
    refreshThemesBtn.textContent = t("themeRefreshThemes");
    refreshThemesBtn.disabled = !window.settingsAPI
      || typeof window.settingsAPI.listThemes !== "function";
    refreshThemesBtn.addEventListener("click", handleRefreshThemes);
    userThemeGroup.buttons.appendChild(refreshThemesBtn);
    row.appendChild(userThemeGroup.group);

    return row;
  }

  function buildThemeActionGroup(title) {
    const group = document.createElement("div");
    group.className = "theme-action-group";
    const label = document.createElement("div");
    label.className = "theme-action-label";
    label.textContent = title;
    group.appendChild(label);
    const buttons = document.createElement("div");
    buttons.className = "theme-action-buttons";
    group.appendChild(buttons);
    return { group, buttons };
  }

  function stopThemeCardButtonKeydown(ev) {
    ev.stopPropagation();
  }

  function buildThemeCard(theme) {
    const card = document.createElement("div");
    card.className = "theme-card";
    card.setAttribute("role", "radio");
    card.setAttribute("tabindex", "0");
    card.setAttribute("aria-checked", theme.active ? "true" : "false");
    if (theme.active) card.classList.add("active");

    const thumb = document.createElement("div");
    thumb.className = "theme-thumb";
    if (theme.previewFileUrl || getCodexPetPreviewAtlasUrl(theme)) {
      thumb.appendChild(buildThemePreviewMedia(theme));
    } else {
      const glyph = document.createElement("span");
      glyph.className = "theme-thumb-empty";
      glyph.textContent = t("themeThumbMissing");
      thumb.appendChild(glyph);
    }
    card.appendChild(thumb);

    const name = document.createElement("div");
    name.className = "theme-card-name";
    const nameText = document.createElement("span");
    nameText.className = "theme-card-name-text";
    nameText.textContent = localizeField(theme.name) || theme.id;
    name.appendChild(nameText);
    if (theme.builtin) {
      const badge = document.createElement("span");
      badge.className = "theme-card-badge";
      badge.textContent = t("themeBadgeBuiltin");
      name.appendChild(badge);
    }
    if (theme.managedCodexPet) {
      const badge = document.createElement("span");
      badge.className = "theme-card-badge accent";
      badge.textContent = t("themeBadgeCodexPet");
      name.appendChild(badge);
    }
    card.appendChild(name);

    const capLabels = getThemeCapabilityBadgeLabels(theme);
    if (capLabels.length) {
      const caps = document.createElement("div");
      caps.className = "theme-card-capabilities";
      for (const label of capLabels) {
        const badge = document.createElement("span");
        badge.className = "theme-card-badge";
        badge.textContent = label;
        caps.appendChild(badge);
      }
      card.appendChild(caps);
    }

    const canDelete = !theme.builtin && !theme.active && !theme.managedCodexPet;
    const canRemoveCodexPet = !!theme.managedCodexPet;
    const footer = document.createElement("div");
    footer.className = "theme-card-footer";
    const indicator = document.createElement("span");
    indicator.className = "theme-card-check";
    indicator.textContent = theme.active ? t("themeActiveIndicator") : "";
    if (!theme.active) indicator.setAttribute("aria-hidden", "true");
    footer.appendChild(indicator);
    if (canDelete) {
      const btn = document.createElement("button");
      btn.className = "theme-delete-btn";
      btn.type = "button";
      btn.textContent = "\u{1F5D1}";
      btn.title = t("themeDeleteLabel");
      btn.setAttribute("aria-label", t("themeDeleteLabel"));
      btn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        handleDeleteTheme(theme);
      });
      btn.addEventListener("keydown", stopThemeCardButtonKeydown);
      footer.appendChild(btn);
    }
    if (canRemoveCodexPet) {
      const btn = document.createElement("button");
      btn.className = "theme-uninstall-btn";
      btn.type = "button";
      btn.textContent = t("themeUninstallPetLabel");
      btn.title = t("themeUninstallPetLabel");
      btn.setAttribute("aria-label", t("themeUninstallPetLabel"));
      btn.disabled = runtime.codexPetRemovalPendingThemeId === theme.id;
      btn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        handleRemoveCodexPet(theme);
      });
      btn.addEventListener("keydown", stopThemeCardButtonKeydown);
      footer.appendChild(btn);
    }
    // Emotion animations button — opens a modal showing this theme's
    // emotion → animation mappings. Available on every theme card.
    {
      const btn = document.createElement("button");
      btn.className = "soft-btn";
      btn.type = "button";
      btn.textContent = "情绪动画";
      btn.style.fontSize = "11px";
      btn.style.padding = "3px 10px";
      btn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        showEmotionModal(theme);
      });
      btn.addEventListener("keydown", stopThemeCardButtonKeydown);
      footer.appendChild(btn);
    }
    card.appendChild(footer);

    if (!theme.active) {
      helpers.attachActivation(card, () => window.settingsAPI.command("setThemeSelection", { themeId: theme.id }));
    }
    return card;
  }

  function formatCodexPetsRefreshOk(result) {
    const summary = (result && result.summary) || {};
    const formatter = t("toastCodexPetsRefreshOk");
    if (typeof formatter === "function") {
      return formatter(
        summary.imported || 0,
        summary.updated || 0,
        summary.unchanged || 0,
        summary.removed || 0,
        summary.invalid || 0,
        !!(result && result.switchedToFallback)
      );
    }
    return String(formatter);
  }

  function formatCodexPetsRefreshFailed(message) {
    const formatter = t("toastCodexPetsRefreshFailed");
    if (typeof formatter === "function") return formatter(message || "unknown error");
    return String(formatter) + (message || "unknown error");
  }

  function handleRefreshCodexPets() {
    if (!window.settingsAPI || typeof window.settingsAPI.refreshCodexPets !== "function") return;
    runtime.codexPetsRefreshPending = true;
    if (state.activeTab === "theme") ops.requestRender({ content: true });
    window.settingsAPI.refreshCodexPets()
      .then((result) => {
        if (!result || result.status !== "ok") {
          ops.showToast(formatCodexPetsRefreshFailed(result && result.message), { error: true });
          return null;
        }
        ops.showToast(formatCodexPetsRefreshOk(result));
        return ops.fetchThemes().then(() => {
          if (state.activeTab === "theme") ops.requestRender({ content: true });
        });
      })
      .catch((err) => {
        ops.showToast(formatCodexPetsRefreshFailed(err && err.message), { error: true });
      })
      .finally(() => {
        runtime.codexPetsRefreshPending = false;
        if (state.activeTab === "theme") ops.requestRender({ content: true });
      });
  }

  function handleOpenUserThemesFolder() {
    if (!window.settingsAPI || typeof window.settingsAPI.openUserThemesDir !== "function") return;
    window.settingsAPI.openUserThemesDir()
      .then((result) => {
        if (!result || result.status !== "ok") {
          ops.showToast(t("toastUserThemesFolderFailed") + ((result && result.message) || "unknown error"), { error: true });
        }
      })
      .catch((err) => {
        ops.showToast(t("toastUserThemesFolderFailed") + (err && err.message), { error: true });
      });
  }

  function handleRefreshThemes() {
    ops.fetchThemes().then(() => {
      if (state.activeTab === "theme") ops.requestRender({ content: true });
    });
  }

  function formatUserThemeZipImportOk(result) {
    const formatter = t("toastUserThemeZipImportOk");
    const name = localizeField(result && result.name) || (result && result.themeId) || "theme";
    if (typeof formatter === "function") return formatter(name);
    return String(formatter);
  }

  function formatUserThemeZipImportFailed(message) {
    const formatter = t("toastUserThemeZipImportFailed");
    if (typeof formatter === "function") return formatter(message || "unknown error");
    return String(formatter) + (message || "unknown error");
  }

  function handleImportUserThemeZip() {
    if (!window.settingsAPI || typeof window.settingsAPI.importUserThemeZip !== "function") return;
    runtime.userThemeZipImportPending = true;
    if (state.activeTab === "theme") ops.requestRender({ content: true });
    window.settingsAPI.importUserThemeZip()
      .then((result) => {
        if (!result || result.status === "cancel") return null;
        if (result.status !== "ok") {
          ops.showToast(formatUserThemeZipImportFailed(result && result.message), { error: true });
          return null;
        }
        ops.showToast(formatUserThemeZipImportOk(result));
        return ops.fetchThemes().then(() => {
          if (state.activeTab === "theme") ops.requestRender({ content: true });
        });
      })
      .catch((err) => {
        ops.showToast(formatUserThemeZipImportFailed(err && err.message), { error: true });
      })
      .finally(() => {
        runtime.userThemeZipImportPending = false;
        if (state.activeTab === "theme") ops.requestRender({ content: true });
      });
  }

  function formatCodexPetZipImportOk(result) {
    const imported = result && result.imported;
    const name = imported && (imported.displayName || imported.id);
    const formatter = t("toastCodexPetZipImportOk");
    if (typeof formatter === "function") return formatter(name || "Codex Pet");
    return String(formatter);
  }

  function formatCodexPetZipImportFailed(message) {
    const formatter = t("toastCodexPetZipImportFailed");
    if (typeof formatter === "function") return formatter(message || "unknown error");
    return String(formatter) + (message || "unknown error");
  }

  function handleImportCodexPetZip() {
    if (!window.settingsAPI || typeof window.settingsAPI.importCodexPetZip !== "function") return;
    runtime.codexPetZipImportPending = true;
    if (state.activeTab === "theme") ops.requestRender({ content: true });
    window.settingsAPI.importCodexPetZip()
      .then((result) => {
        if (!result || result.status === "cancel") return null;
        if (result.status !== "ok") {
          ops.showToast(formatCodexPetZipImportFailed(result && result.message), { error: true });
          return null;
        }
        ops.showToast(formatCodexPetZipImportOk(result));
        return ops.fetchThemes().then(() => {
          if (state.activeTab === "theme") ops.requestRender({ content: true });
        });
      })
      .catch((err) => {
        ops.showToast(formatCodexPetZipImportFailed(err && err.message), { error: true });
      })
      .finally(() => {
        runtime.codexPetZipImportPending = false;
        if (state.activeTab === "theme") ops.requestRender({ content: true });
      });
  }

  function formatCodexPetRemoveOk(result) {
    const removed = result && result.removed;
    const name = removed && (removed.displayName || removed.id);
    const formatter = t("toastCodexPetRemoveOk");
    if (typeof formatter === "function") return formatter(name || "Codex Pet", !!(result && result.switchedToFallback));
    return String(formatter);
  }

  function formatCodexPetRemoveFailed(message) {
    const formatter = t("toastCodexPetRemoveFailed");
    if (typeof formatter === "function") return formatter(message || "unknown error");
    return String(formatter) + (message || "unknown error");
  }

  function handleRemoveCodexPet(theme) {
    if (!window.settingsAPI || typeof window.settingsAPI.removeCodexPet !== "function") return;
    runtime.codexPetRemovalPendingThemeId = theme.id;
    if (state.activeTab === "theme") ops.requestRender({ content: true });
    window.settingsAPI.removeCodexPet(theme.id)
      .then((result) => {
        if (!result || result.status === "cancel") return null;
        if (result.status !== "ok") {
          ops.showToast(formatCodexPetRemoveFailed(result && result.message), { error: true });
          return null;
        }
        ops.showToast(formatCodexPetRemoveOk(result));
        return ops.fetchThemes().then(() => {
          if (state.activeTab === "theme") ops.requestRender({ content: true });
        });
      })
      .catch((err) => {
        ops.showToast(formatCodexPetRemoveFailed(err && err.message), { error: true });
      })
      .finally(() => {
        runtime.codexPetRemovalPendingThemeId = null;
        if (state.activeTab === "theme") ops.requestRender({ content: true });
      });
  }

  function handleDeleteTheme(theme) {
    if (!window.settingsAPI) return;
    window.settingsAPI
      .confirmRemoveTheme(theme.id)
      .then((res) => {
        if (!res || !res.confirmed) return null;
        return window.settingsAPI.command("removeTheme", theme.id);
      })
      .then((result) => {
        if (result == null) return;
        if (result.status !== "ok") {
          const msg = (result && result.message) || "unknown error";
          ops.showToast(t("toastThemeDeleteFailed") + msg, { error: true });
          return;
        }
        ops.showToast(t("toastThemeDeleted"));
        ops.fetchThemes().then(() => {
          if (state.activeTab === "theme") ops.requestRender({ content: true });
        });
      })
      .catch((err) => {
        ops.showToast(t("toastThemeDeleteFailed") + (err && err.message), { error: true });
      });
  }

  // ── Emotion animation modal ──
  // Shows a theme's emotion → animation mappings in a popup overlay.
  const EMOTION_ICONS = {
    happy: "😸", curious: "🤔", sad: "😿",
    excited: "🙌", mad: "😤", neutral: "😐",
  };
  const EMOTION_LABELS = {
    happy: "开心", curious: "好奇", sad: "难过",
    excited: "兴奋", mad: "生气", neutral: "平静",
  };

  function showEmotionModal(theme) {
    // Remove any existing modal first
    const existing = document.getElementById("emotion-modal-overlay");
    if (existing) existing.remove();

    const overlay = document.createElement("div");
    overlay.id = "emotion-modal-overlay";
    Object.assign(overlay.style, {
      position: "fixed", top: "0", left: "0", right: "0", bottom: "0",
      background: "rgba(0,0,0,0.5)", zIndex: "10000",
      display: "flex", alignItems: "center", justifyContent: "center",
    });

    const modal = document.createElement("div");
    Object.assign(modal.style, {
      background: "var(--bg, #1e1e2e)", color: "var(--text-primary, #cdd6f4)",
      borderRadius: "12px", padding: "24px", maxWidth: "520px", width: "90%",
      maxHeight: "80vh", overflowY: "auto",
      border: "1px solid var(--border, #45475a)",
      boxShadow: "0 8px 32px rgba(0,0,0,0.3)",
    });

    // Header
    const header = document.createElement("div");
    Object.assign(header.style, {
      display: "flex", justifyContent: "space-between", alignItems: "center",
      marginBottom: "16px", paddingBottom: "12px",
      borderBottom: "1px solid var(--border, #45475a)",
    });
    const title = document.createElement("h3");
    title.textContent = (localizeField(theme.name) || theme.id) + " — 情绪动画";
    title.style.margin = "0";
    title.style.fontSize = "16px";
    title.style.fontWeight = "600";
    header.appendChild(title);

    const closeBtn = document.createElement("button");
    closeBtn.textContent = "✕";
    closeBtn.style.background = "none";
    closeBtn.style.border = "none";
    closeBtn.style.color = "var(--text-secondary)";
    closeBtn.style.cursor = "pointer";
    closeBtn.style.fontSize = "18px";
    closeBtn.addEventListener("click", () => overlay.remove());
    header.appendChild(closeBtn);
    modal.appendChild(header);

    // Loading indicator
    const loading = document.createElement("div");
    loading.textContent = "加载中...";
    loading.style.color = "var(--text-secondary)";
    loading.style.padding = "20px 0";
    loading.style.textAlign = "center";
    modal.appendChild(loading);

    overlay.appendChild(modal);
    overlay.addEventListener("click", (ev) => {
      if (ev.target === overlay) overlay.remove();
    });
    document.body.appendChild(overlay);

    // Fetch emotion map
    if (!window.settingsAPI || typeof window.settingsAPI.getThemeEmotionMap !== "function") {
      loading.textContent = "API 不可用";
      return;
    }
    window.settingsAPI.getThemeEmotionMap(theme.id).then((result) => {
      loading.remove();
      if (!result || result.status !== "ok") {
        const err = document.createElement("div");
        err.textContent = "无法加载情绪映射: " + (result && result.message || "未知错误");
        err.style.color = "#ff4d4f";
        modal.appendChild(err);
        return;
      }
      const map = Array.isArray(result.map) ? result.map : [];
      if (map.length === 0) {
        const empty = document.createElement("div");
        empty.textContent = "这个主题还没有配置情绪动画。在 theme.json 的 states.attention 里添加 {\"file\": \"动画.gif\", \"emotion\": \"happy\"} 格式的条目即可。";
        empty.style.color = "var(--text-secondary)";
        empty.style.fontSize = "13px";
        empty.style.lineHeight = "1.6";
        modal.appendChild(empty);
        return;
      }
      // Render emotion list
      for (const item of map) {
        const row = document.createElement("div");
        Object.assign(row.style, {
          display: "flex", alignItems: "center", gap: "10px",
          padding: "10px 12px", marginBottom: "8px", borderRadius: "8px",
          background: "var(--panel-bg, #181825)",
          border: "1px solid var(--border, #45475a)",
        });
        const icon = document.createElement("span");
        icon.textContent = EMOTION_ICONS[item.emotion] || "🎬";
        icon.style.fontSize = "20px";
        row.appendChild(icon);

        const info = document.createElement("div");
        info.style.flex = "1";
        const label = document.createElement("div");
        label.textContent = EMOTION_LABELS[item.emotion] || item.emotion;
        label.style.fontSize = "14px";
        label.style.fontWeight = "600";
        info.appendChild(label);
        const tag = document.createElement("div");
        tag.textContent = "[EMOTION:" + item.emotion + "]";
        tag.style.fontSize = "11px";
        tag.style.fontFamily = "monospace";
        tag.style.color = "var(--text-secondary)";
        info.appendChild(tag);
        row.appendChild(info);

        const file = document.createElement("div");
        file.textContent = item.file;
        file.style.fontSize = "12px";
        file.style.fontFamily = "monospace";
        file.style.color = "var(--text-secondary)";
        file.style.maxWidth = "180px";
        file.style.overflow = "hidden";
        file.style.textOverflow = "ellipsis";
        file.style.whiteSpace = "nowrap";
        row.appendChild(file);

        if (item.isDefault) {
          const def = document.createElement("span");
          def.textContent = "默认";
          def.style.fontSize = "10px";
          def.style.color = "var(--text-secondary)";
          def.style.fontStyle = "italic";
          row.appendChild(def);
        }
        // Preview button
        const previewBtn = document.createElement("button");
        previewBtn.textContent = "预览";
        previewBtn.style.cssText = "font-size:11px;padding:2px 8px;border:1px solid var(--border);border-radius:4px;background:var(--bg);color:var(--text-primary);cursor:pointer;";
        previewBtn.addEventListener("click", (ev) => {
          ev.stopPropagation();
          if (window.settingsAPI && typeof window.settingsAPI.previewAnimationOverride === "function") {
            window.settingsAPI.previewAnimationOverride({ stateKey: "attention", file: item.file, durationMs: 3000 });
          }
        });
        row.appendChild(previewBtn);

        // Upload button — pick a custom animation file for this emotion
        const uploadBtn = document.createElement("button");
        uploadBtn.textContent = "上传";
        uploadBtn.style.cssText = "font-size:11px;padding:2px 8px;border:1px solid var(--accent);border-radius:4px;background:var(--bg);color:var(--accent);cursor:pointer;";
        uploadBtn.addEventListener("click", (ev) => {
          ev.stopPropagation();
          if (!window.settingsAPI || typeof window.settingsAPI.pickEmotionAnimation !== "function") return;
          window.settingsAPI.pickEmotionAnimation({ emotionName: item.emotion }).then((result) => {
            if (!result || result.status !== "ok") return;
            return window.settingsAPI.saveEmotionOverride({
              themeId: theme.id,
              emotionName: item.emotion,
              file: result.file,
            }).then(() => {
              overlay.remove();
              showEmotionModal(theme);
            });
          }).catch(() => {});
        });
        row.appendChild(uploadBtn);

        // Remove override button (only for user-added emotions)
        if (item.isOverride) {
          const delBtn = document.createElement("button");
          delBtn.textContent = "删除";
          delBtn.style.cssText = "font-size:11px;padding:2px 8px;border:1px solid #ff4d4f;border-radius:4px;background:var(--bg);color:#ff4d4f;cursor:pointer;";
          delBtn.addEventListener("click", (ev) => {
            ev.stopPropagation();
            if (!window.settingsAPI || typeof window.settingsAPI.saveEmotionOverride !== "function") return;
            window.settingsAPI.saveEmotionOverride({
              themeId: theme.id,
              emotionName: item.emotion,
              file: null,
            }).then(() => {
              overlay.remove();
              showEmotionModal(theme);
            }).catch(() => {});
          });
          row.appendChild(delBtn);
        }
        modal.appendChild(row);
      }

      // Add custom emotion section
      const addSection = document.createElement("div");
      Object.assign(addSection.style, {
        marginTop: "16px", padding: "12px", borderRadius: "8px",
        border: "1px dashed var(--border)", display: "flex",
        alignItems: "center", gap: "8px", flexWrap: "wrap",
      });
      const addLabel = document.createElement("span");
      addLabel.textContent = "添加自定义情绪：";
      addLabel.style.cssText = "font-size:13px;color:var(--text-secondary);white-space:nowrap;";
      addSection.appendChild(addLabel);
      const addInput = document.createElement("input");
      addInput.type = "text";
      addInput.placeholder = "情绪标签名（如：surprised）";
      addInput.style.cssText = "flex:1;min-width:120px;padding:4px 8px;font-size:12px;border:1px solid var(--border);border-radius:4px;background:var(--bg);color:var(--text-primary);";
      addSection.appendChild(addInput);
      const addBtn = document.createElement("button");
      addBtn.textContent = "上传动画";
      addBtn.style.cssText = "font-size:12px;padding:4px 14px;border:1px solid var(--accent);border-radius:4px;background:var(--accent);color:#fff;cursor:pointer;";
      addBtn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        const name = addInput.value.trim().toLowerCase();
        if (!name || !/^[a-z_]+$/.test(name)) { addInput.style.borderColor = "#ff4d4f"; return; }
        addInput.style.borderColor = "";
        if (!window.settingsAPI || typeof window.settingsAPI.pickEmotionAnimation !== "function") return;
        window.settingsAPI.pickEmotionAnimation({ emotionName: name }).then((result) => {
          if (!result || result.status !== "ok") return;
          return window.settingsAPI.saveEmotionOverride({
            themeId: theme.id,
            emotionName: name,
            file: result.file,
          }).then(() => {
            overlay.remove();
            showEmotionModal(theme);
          });
        }).catch(() => {});
      });
      addSection.appendChild(addBtn);
      modal.appendChild(addSection);

      // Help text
      const help = document.createElement("div");
      Object.assign(help.style, {
        marginTop: "16px", padding: "12px", borderRadius: "6px",
        background: "color-mix(in srgb, var(--accent, #89b4fa) 8%, transparent)",
        fontSize: "12px", color: "var(--text-secondary)", lineHeight: "1.6",
      });
      help.textContent = "聊天时模型会在回复末尾输出 [EMOTION:xxx] 标签，桌宠自动切换到对应动画。要添加新情绪，在主题文件夹的 theme.json 里加 {\"file\": \"你的动画.gif\", \"emotion\": \"标签名\"}。";
      modal.appendChild(help);
    }).catch((err) => {
      loading.textContent = "加载失败: " + (err && err.message || err);
    });
  }

  function init(core) {
    state = core.state;
    runtime = core.runtime;
    helpers = core.helpers;
    ops = core.ops;
    readers = core.readers;
    core.tabs.theme = {
      render,
    };
  }

  root.ClawdSettingsTabTheme = { init };
})(globalThis);
