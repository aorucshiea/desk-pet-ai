"use strict";

// ── Self-Evolution settings tab (Phase 3) ──

(function initSettingsTabEvolve(root) {
  let core = null; let helpers = null; let ops = null; let mounted = false;

  function t(key) { return helpers.t(key); }
  function cleanupTimers() { mounted = false; }

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

  async function saveField(field, value) {
    try { if (window.settingsAPI && typeof window.settingsAPI.update === "function") { const live = (core.state.snapshot && core.state.snapshot.skills) || {}; await window.settingsAPI.update("skills", { ...live, [field]: value }); } } catch {}
  }

  function renderHeader(parent) {
    parent.appendChild(el("div", { style: { padding: "0 0 16px 0", borderBottom: "1px solid var(--border)" } },
      el("h2", { style: { margin: "0 0 4px 0", fontSize: "18px", fontWeight: "600" } }, t("sidebarEvolve")),
      el("p", { style: { margin: "0", fontSize: "13px", color: "var(--text-secondary)" } }, "Let the pet learn from conversations and improve over time."),
    ));
  }

  function renderContent(parent) {
    const skills = (core.state.snapshot && core.state.snapshot.skills) || {};
    const autoReview = !!skills.autoReview;
    const intervalMs = (typeof skills.nudgeInterval === "number" && skills.nudgeInterval > 0) ? skills.nudgeInterval : 604800000;
    const days = Math.round(intervalMs / 86400000);

    // Card 1: Auto-Review
    const card1 = el("div", { style: { background: "var(--panel-bg)", borderRadius: "8px", padding: "16px", marginTop: "16px", border: "1px solid var(--border)" } });
    card1.appendChild(el("h3", { style: { margin: "0 0 4px 0", fontSize: "15px", fontWeight: "600" } }, "Auto-Review"));
    card1.appendChild(el("p", { style: { margin: "0 0 12px 0", fontSize: "12px", color: "var(--text-secondary)" } }, "Enable per-turn analysis to help the pet learn from conversation patterns."));

    const row = el("div", { style: { display: "flex", justifyContent: "space-between", alignItems: "center" } });
    row.appendChild(el("span", { style: { fontSize: "14px", fontWeight: "500" } }, "Auto-review (per-turn analysis)"));
    const wrap = el("label", { className: "toggle-switch" });
    const cb = el("input", { type: "checkbox", className: "toggle-input", checked: autoReview ? "checked" : undefined, onchange: () => saveField("autoReview", cb.checked) });
    wrap.appendChild(cb); wrap.appendChild(el("span", { className: "toggle-slider" }));
    row.appendChild(wrap);
    card1.appendChild(row);
    parent.appendChild(card1);

    // Card 2: Nudge
    const card2 = el("div", { style: { background: "var(--panel-bg)", borderRadius: "8px", padding: "16px", marginTop: "12px", border: "1px solid var(--border)" } });
    card2.appendChild(el("h3", { style: { margin: "0 0 4px 0", fontSize: "15px", fontWeight: "600" } }, "Nudge Interval"));
    card2.appendChild(el("p", { style: { margin: "0 0 12px 0", fontSize: "12px", color: "var(--text-secondary)" } }, "How often the pet suggests creating new skills from patterns."));

    const nRow = el("div", { style: { display: "flex", alignItems: "center", gap: "12px" } });
    nRow.appendChild(el("span", { style: { fontSize: "13px" } }, "Interval"));
    const val = el("span", { style: { fontSize: "14px", fontWeight: "600", color: "var(--accent)", minWidth: "32px" } }, days + "d");
    const slider = el("input", { type: "range", min: "1", max: "28", value: String(days), style: { flex: "1", maxWidth: "200px" }, oninput: () => { val.textContent = parseInt(slider.value, 10) + "d"; }, onchange: () => saveField("nudgeInterval", (parseInt(slider.value, 10) || 7) * 86400000) });
    nRow.appendChild(slider); nRow.appendChild(val);
    card2.appendChild(nRow);
    parent.appendChild(card2);
  }

  async function render(parent) {
    cleanupTimers(); mounted = true; parent.innerHTML = "";
    renderHeader(parent); renderContent(parent);
  }

  function init(coreArg) { core = coreArg; helpers = core.helpers; ops = core.ops; core.tabs.evolve = { render: (parent) => { void render(parent); } }; }
  root.ClawdSettingsTabEvolve = { init };
})(typeof globalThis !== "undefined" ? globalThis : window);
