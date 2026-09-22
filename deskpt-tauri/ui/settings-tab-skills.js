"use strict";

// ── Skills settings tab ──

(function initSettingsTabSkills(root) {
  let core = null;
  let helpers = null;
  let ops = null;
  let mounted = false;
  let skillsList = [];
  let skillsError = null;

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

  function renderHeader(parent) {
    parent.appendChild(el("div", { style: { padding: "0 0 16px 0", borderBottom: "1px solid var(--border)" } },
      el("h2", { style: { margin: "0 0 4px 0", fontSize: "18px", fontWeight: "600" } }, t("sidebarSkills")),
      el("p", { style: { margin: "0", fontSize: "13px", color: "var(--text-secondary)" } }, "Discover and manage reusable skills for the pet."),
    ));
  }

  function renderSkillsToggle(parent) {
    const card = el("div", {
      style: { background: "var(--panel-bg)", borderRadius: "8px", padding: "16px", marginTop: "16px", border: "1px solid var(--border)", },
    });
    const enabled = !!(core.state.snapshot && core.state.snapshot.skills && core.state.snapshot.skills.enabled);
    const row = el("div", { style: { display: "flex", justifyContent: "space-between", alignItems: "center" } });
    row.appendChild(el("span", { style: { fontSize: "14px", fontWeight: "500" } }, "Enable Skills System"));
    const toggleWrap = el("label", { className: "toggle-switch" });
    const cb = el("input", {
      type: "checkbox", className: "toggle-input",
      checked: enabled ? "checked" : undefined,
      onchange: async () => {
        const val = cb.checked;
        try {
          if (window.settingsAPI && typeof window.settingsAPI.update === "function") {
            const live = (core.state.snapshot && core.state.snapshot.skills) || {};
            await window.settingsAPI.update("skills", { ...live, enabled: val });
          }
        } catch { cb.checked = !val; }
      },
    });
    toggleWrap.appendChild(cb);
    toggleWrap.appendChild(el("span", { className: "toggle-slider" }));
    row.appendChild(toggleWrap);
    card.appendChild(row);
    parent.appendChild(card);
  }

  async function renderSkillsList(parent) {
    const card = el("div", {
      style: { background: "var(--panel-bg)", borderRadius: "8px", padding: "16px", marginTop: "12px", border: "1px solid var(--border)", },
    });
    const title = el("h3", { style: { margin: "0 0 12px 0", fontSize: "15px", fontWeight: "600" } }, "Available Skills");
    card.appendChild(title);

    const enabled = !!(core.state.snapshot && core.state.snapshot.skills && core.state.snapshot.skills.enabled);
    if (!enabled) {
      card.appendChild(el("p", { style: { color: "var(--text-secondary)", fontSize: "13px" } }, "Skills system is disabled."));
    } else if (skillsError) {
      card.appendChild(el("p", { style: { color: "#ff4d4f", fontSize: "13px" } }, "Error: " + skillsError));
    } else if (skillsList.length === 0) {
      card.appendChild(el("p", { style: { color: "var(--text-secondary)", fontSize: "13px" } }, "No skills found. Ensure sidecar is running and skill directories configured."));
    } else {
      const grid = el("div", { style: { display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(280px, 1fr))", gap: "8px" } });
      for (const sk of skillsList) {
        const item = el("div", {
          style: { padding: "10px 12px", borderRadius: "6px", border: "1px solid var(--border)", background: "var(--bg)" },
        });
        item.appendChild(el("div", { style: { fontSize: "13px", fontWeight: "600", fontFamily: "monospace" } }, sk.name || "?"));
        item.appendChild(el("div", { style: { fontSize: "12px", color: "var(--text-secondary)", marginTop: "4px", lineHeight: "1.4" } }, (sk.description || "").slice(0, 150)));
        grid.appendChild(item);
      }
      card.appendChild(grid);
    }
    parent.appendChild(card);
  }

  async function fetchSkills() {
    if (!mounted) return;
    try {
      if (window.minicpmSettings && typeof window.minicpmSettings.listSkills === "function") {
        const data = await window.minicpmSettings.listSkills();
        skillsList = Array.isArray(data && data.skills) ? data.skills : [];
        skillsError = null;
      }
    } catch (err) {
      skillsError = String(err && err.message || err);
      skillsList = [];
    }
  }

  async function render(parent) {
    cleanupTimers();
    mounted = true;
    parent.innerHTML = "";
    renderHeader(parent);
    renderSkillsToggle(parent);
    await fetchSkills();
    await renderSkillsList(parent);
  }

  function init(coreArg) {
    core = coreArg;
    helpers = core.helpers;
    ops = core.ops;
    core.tabs.skills = {
      render: (parent) => { void render(parent); },
    };
  }

  root.ClawdSettingsTabSkills = { init };
})(typeof globalThis !== "undefined" ? globalThis : window);
