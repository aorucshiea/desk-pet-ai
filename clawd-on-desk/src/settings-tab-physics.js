"use strict";

// ── Desktop Physics settings tab（前瞻 02 P0 的 UI 面） ──
// 对应当前后端：world-physics.js + world-geometry.js（重力地板 + 任务栏刚体）。
// 写入键：physicsWorld(bool) / physicsGravity(number 1000..12000, prefs.js schema)。

(function initSettingsTabPhysics(root) {
  let core = null;
  let helpers = null;
  let mounted = false;

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
    for (const c of children) {
      if (c == null) continue;
      if (typeof c === "string" || typeof c === "number") e.appendChild(document.createTextNode(String(c)));
      else if (c instanceof Node) e.appendChild(c);
      else if (Array.isArray(c)) c.forEach((child) => { if (child instanceof Node) e.appendChild(child); });
    }
    return e;
  }

  function card(...children) {
    return el("div", {
      style: { background: "var(--panel-bg)", borderRadius: "8px", padding: "16px", marginTop: "12px", border: "1px solid var(--border)" },
    }, ...children);
  }

  function renderHeader(parent) {
    parent.appendChild(el("div", { style: { padding: "0 0 16px 0", borderBottom: "1px solid var(--border)" } },
      el("h2", { style: { margin: "0 0 4px 0", fontSize: "18px", fontWeight: "600" } }, t("sidebarPhysics")),
      el("p", { style: { margin: "0", fontSize: "13px", color: "var(--text-secondary)" } }, t("physicsSubtitle")),
    ));
  }

  function renderEnabled(parent) {
    const snap = core.state.snapshot || {};
    const enabled = snap.physicsWorld !== false; // default ON
    const row = el("div", { style: { display: "flex", justifyContent: "space-between", alignItems: "center" } });
    const left = el("div", {},
      el("div", { style: { fontSize: "14px", fontWeight: "500" } }, t("physicsEnableTitle")),
      el("div", { style: { fontSize: "12px", color: "var(--text-secondary)", marginTop: "2px" } }, t("physicsEnableDesc")),
    );
    const toggleWrap = el("label", { className: "toggle-switch" });
    const cb = el("input", {
      type: "checkbox", className: "toggle-input",
      checked: enabled ? "checked" : undefined,
      onchange: async () => {
        const val = cb.checked;
        try {
          await window.settingsAPI.update("physicsWorld", val);
        } catch (e) { cb.checked = !val; }
      },
    });
    toggleWrap.appendChild(cb);
    toggleWrap.appendChild(el("span", { className: "toggle-slider" }));
    row.appendChild(left); row.appendChild(toggleWrap);
    parent.appendChild(card(row));
  }

  function renderGravity(parent) {
    const snap = core.state.snapshot || {};
    const cur = Number(snap.physicsGravity);
    const v0 = Number.isFinite(cur) ? cur : 10;
    const valueLabel = el("span", { style: { fontSize: "13px", color: "var(--text-secondary)", fontFamily: "monospace", minWidth: "84px", textAlign: "right" } }, `${v0} m/s²`);
    const slider = el("input", {
      type: "range", min: "0", max: "100", step: "1", value: String(v0),
      style: { flex: "1", accentColor: "var(--accent)" },
      oninput: () => { valueLabel.textContent = `${slider.value} m/s²`; },
      onchange: async () => {
        try {
          await window.settingsAPI.update("physicsGravity", Number(slider.value));
        } catch (e) { /* 快照回滚由 controller 广播 */ }
      },
    });
    const row = el("div", { style: { display: "flex", alignItems: "center", gap: "12px" } },
      el("span", { style: { fontSize: "12px", color: "var(--text-secondary)", fontFamily: "monospace" } }, "0"),
      slider,
      el("span", { style: { fontSize: "12px", color: "var(--text-secondary)", fontFamily: "monospace" } }, "100"),
      valueLabel,
    );
    parent.appendChild(card(
      el("div", { style: { fontSize: "14px", fontWeight: "500", marginBottom: "8px" } }, t("physicsGravityTitle")),
      row,
      el("div", { style: { fontSize: "12px", color: "var(--text-secondary)", marginTop: "8px" } }, t("physicsGravityHint")),
    ));
  }

  async function render(parent) {
    cleanupTimers();
    mounted = true;
    parent.innerHTML = "";
    renderHeader(parent);
    renderEnabled(parent);
    renderGravity(parent);
  }

  function init(coreArg) {
    core = coreArg;
    helpers = core.helpers;
    core.tabs.physics = {
      render: (parent) => { void render(parent); },
    };
  }

  root.ClawdSettingsTabPhysics = { init };
})(typeof globalThis !== "undefined" ? globalThis : window);
