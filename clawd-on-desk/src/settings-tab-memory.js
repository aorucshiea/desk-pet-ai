"use strict";

// Settings → Memory viewer (持续自我存在 — same self, only the look changes).
// Shows the CURRENT theme's soul-layer memory: identity notes
// (MEMORY.md / USER.md), the episodic event list (weight/emotion/
// resolved/core), and the mood state. Read-only for now.

(function initSettingsTabMemory(root) {
  var core = null;
  var helpers = null;

  function t(key) { try { return helpers.t(key); } catch(e) { return key; } }

  function el(tag, attrs) {
    var children = [], len = arguments.length - 2;
    while (len-- > 0) children[len] = arguments[len + 2];
    try {
      var e = document.createElement(tag);
      if (attrs && typeof attrs === "object" && !Array.isArray(attrs)) {
        for (var k in attrs) {
          if (!Object.prototype.hasOwnProperty.call(attrs, k)) continue;
          var v = attrs[k];
          if (k === "className") { e.className = v; }
          else if (k === "style" && typeof v === "object") { for (var sk in v) e.style[sk] = v[sk]; }
          else if (k.startsWith("on")) { e.addEventListener(k.slice(2).toLowerCase(), v); }
          else if (v !== undefined && v !== null) { e.setAttribute(k, v); }
        }
      }
      if (children) {
        for (var ci = 0; ci < children.length; ci++) {
          var c = children[ci];
          if (c == null) continue;
          if (typeof c === "string" || typeof c === "number") e.appendChild(document.createTextNode(String(c)));
          else if (c instanceof Node) e.appendChild(c);
          else if (Array.isArray(c)) { for (var cj = 0; cj < c.length; cj++) { if (c[cj] instanceof Node) e.appendChild(c[cj]); } }
        }
      }
      return e;
    } catch (ex) { console.warn("memory: el error", ex); return document.createElement("div"); }
  }

  function section(title, bodyNode) {
    var card = el("div", { style: { background: "var(--panel-bg)", borderRadius: "8px", padding: "16px", marginTop: "12px", border: "1px solid var(--border)" } });
    card.appendChild(el("div", { style: { fontSize: "14px", fontWeight: "500", marginBottom: "8px" } }, title));
    card.appendChild(bodyNode);
    return card;
  }

  function pre(text) {
    return el("pre", {
      style: {
        whiteSpace: "pre-wrap", wordBreak: "break-word",
        fontSize: "12px", lineHeight: "1.5",
        color: "var(--text-secondary)",
        margin: "0", maxHeight: "240px", overflowY: "auto",
      },
    }, text || t("memoryEmpty"));
  }

  function chip(text, color) {
    return el("span", {
      style: {
        display: "inline-block", padding: "1px 7px", marginRight: "6px",
        borderRadius: "10px", fontSize: "11px",
        background: color || "rgba(128,128,128,0.18)",
        color: "var(--text-secondary)",
      },
    }, text);
  }

  async function render(parent) {
    try {
      parent.appendChild(el("div", { style: { padding: "0 0 16px 0", borderBottom: "1px solid var(--border)" } },
        el("h2", { style: { margin: "0 0 4px 0", fontSize: "18px", fontWeight: "600" } }, t("memoryTitle")),
        el("p", { style: { margin: "0", fontSize: "13px", color: "var(--text-secondary)" } }, t("memorySubtitle")),
      ));

      const status = el("div", { style: { fontSize: "13px", color: "var(--text-secondary)", marginTop: "12px" } }, t("memoryLoading"));
      parent.appendChild(status);

      var ret = null;
      try {
        ret = await window.settingsAPI.getMemoryView();
      } catch (err) {
        status.textContent = t("memoryUnreachable") + (err && err.message ? "：" + err.message : "");
        return;
      }
      if (!ret || ret.status !== "ok") {
        status.textContent = t("memoryUnreachable");
        return;
      }
      parent.removeChild(status);

      // ── 当前身体 / 情绪 ──
      const ident = ret.identity || {};
      const mood = ret.mood || {};
      const headRow = el("div", { style: { display: "flex", flexWrap: "wrap", gap: "6px", marginTop: "12px" } });
      headRow.appendChild(chip(t("memoryTheme") + "：" + (ident.theme || "default"), "rgba(74,158,255,0.25)"));
      headRow.appendChild(chip(t("memoryMood") + "：" + (mood.mood || "—") + (mood.intensity != null ? " (" + mood.intensity + "/100)" : ""), "rgba(255,170,60,0.22)"));
      headRow.appendChild(chip(t("memoryEventCount") + "：" + ((ret.events && ret.events.count) || 0), "rgba(90,200,120,0.22)"));
      parent.appendChild(headRow);

      // ── 身份记忆 ──
      parent.appendChild(section(t("memoryIdentity"), el("div", null,
        el("div", { style: { fontSize: "12px", color: "var(--text-secondary)", marginBottom: "4px" } }, "MEMORY.md" + (ident.memory_dir ? "  ·  " + ident.memory_dir : "")),
        pre(ident.memory),
        el("div", { style: { fontSize: "12px", color: "var(--text-secondary)", margin: "10px 0 4px 0" } }, "USER.md"),
        pre(ident.user),
      )));

      // ── 事件记忆 ──
      const events = (ret.events && ret.events.events) || [];
      const evtBody = el("div", null);
      if (events.length === 0) {
        evtBody.appendChild(el("div", { style: { fontSize: "12px", color: "var(--text-secondary)" } }, t("memoryNoEvents")));
      }
      events.forEach((evt) => {
        const chips = el("div", { style: { marginBottom: "4px" } });
        chips.appendChild(chip("w" + evt.weight, evt.weight >= 700 ? "rgba(255,120,80,0.25)" : evt.weight >= 300 ? "rgba(255,200,80,0.22)" : "rgba(128,128,128,0.18)"));
        if (evt.emotion) chips.appendChild(chip(evt.emotion, "rgba(140,120,255,0.25)"));
        if (evt.type === "knowledge") chips.appendChild(chip(t("memoryKnowledge"), "rgba(90,180,255,0.22)"));
        if (!evt.resolved) chips.appendChild(chip(t("memoryUnfinished"), "rgba(255,90,90,0.25)"));
        if (evt.core) chips.appendChild(chip("★ " + t("memoryCore"), "rgba(255,220,90,0.3)"));

        evtBody.appendChild(el("div", {
          style: {
            padding: "10px", borderRadius: "6px", marginBottom: "8px",
            background: "rgba(128,128,128,0.06)", border: "1px solid var(--border)",
          },
        },
          el("div", { style: { fontSize: "13px", fontWeight: "500", marginBottom: "2px" } },
            (evt.core ? "★ " : "") + evt.title),
          chips,
          el("div", { style: { fontSize: "12px", color: "var(--text-secondary)", lineHeight: "1.5" } }, evt.content),
        ));
      });
      parent.appendChild(section(t("memoryEvents"), evtBody));
    } catch(e) { console.warn("memory: render error", e); }
  }

  function init(coreArg) {
    try {
      core = coreArg;
      helpers = core.helpers;
      core.tabs.memory = { render: render };
    } catch(e) { console.warn("memory: init error", e); }
  }

  root.ClawdSettingsTabMemory = { init: init };
})(typeof globalThis !== "undefined" ? globalThis : window);
