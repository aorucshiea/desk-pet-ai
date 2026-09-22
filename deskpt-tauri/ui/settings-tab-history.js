"use strict";

// Settings → 历史 (conversation history, last 24 hours).
//
// Data source: chat-history.json (persisted by the bubble renderer via
// minicpm:save-history) read through minicpm-settings:get-history-file —
// deliberately the FILE, not the bubble's in-memory array, so the
// viewer works even when the bubble is closed. Messages stamped with
// `ts` by _cleanHistoryForSave; the viewer filters to the last 24h
// (用户: 只支持最近一天的内容).
//
// Design notes:
// - Newest first: the primary use case is "刚才它说了什么我没看到" — the
//   missed reply must be visible without scrolling.
// - Thinking is collapsed by default (用户: 输出太长思考内容), capped
//   with an inner scroll.
// - Proactive wake-up prompts ([系统提示：…]) are user-role messages the
//   renderer injects itself — shown as dim chips, not as user bubbles.
// - Tool-call markers ([MCP:server/tool:{json}]) render as dim 🔧 chips.

(function initSettingsTabHistory(root) {
  var core = null;
  var helpers = null;
  var _lastTheme = null;

  function t(key) { try { return helpers.t(key); } catch (e) { return key; } }

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
    } catch (ex) { console.warn("history: el error", ex); return document.createElement("div"); }
  }

  var DAY_MS = 24 * 60 * 60 * 1000;

  function pad2(n) { return (n < 10 ? "0" : "") + n; }

  function fmtTime(ts) {
    try {
      var d = new Date(ts);
      return pad2(d.getMonth() + 1) + "-" + pad2(d.getDate()) + " " + pad2(d.getHours()) + ":" + pad2(d.getMinutes());
    } catch (e) { return ""; }
  }

  // Defensive display cleanup: control tags are usually already stripped
  // by the bubble's sanitize, but the file may hold older raw turns.
  function cleanContent(text) {
    return String(text || "")
      .replace(/<<<MEM>[\s\S]*?<<<MEMEND>>>/g, "")
      .replace(/\[EMOTION:[^\]]*\]/g, "")
      .replace(/\[NEXT_CHAT:\s*\d+[^\]]*\]/gi, "")
      .replace(/\[MCP:([a-zA-Z0-9_\-\/]+)/g, "🔧 $1 ")
      .replace(/\[skill:([a-zA-Z0-9_\-\/]+)\]/gi, "🔧 $1 ")
      .trim();
  }

  function isProactive(m) {
    return m.role === "user" && /^\s*\[系统提示[：:]/.test(String(m.content || ""));
  }

  function chip(text) {
    return el("div", {
      style: {
        textAlign: "center", fontSize: "11px", margin: "10px 0 4px 0",
        color: "var(--text-secondary)", opacity: "0.85",
      },
    }, text);
  }

  function thinkingNode(text) {
    var pre = el("pre", {
      style: {
        margin: "6px 0 0 0", fontSize: "12px", lineHeight: "1.55",
        color: "var(--text-secondary)", whiteSpace: "pre-wrap",
        maxHeight: "260px", overflowY: "auto",
      },
    }, String(text || "").trim());
    var details = el("details", { style: { margin: "8px 0 2px 0" } });
    var summary = el("summary", {
      style: { cursor: "pointer", fontSize: "12px", color: "var(--text-secondary)", userSelect: "none" },
    }, t("historyThinking"));
    details.appendChild(summary);
    details.appendChild(pre);
    return details;
  }

  function messageNode(m) {
    var isUser = m.role === "user";
    var bubbleStyle = {
      maxWidth: "82%", padding: "8px 12px", borderRadius: "10px",
      fontSize: "13px", lineHeight: "1.6", whiteSpace: "pre-wrap",
      wordBreak: "break-word",
    };
    if (isUser) {
      bubbleStyle.background = "rgba(91,124,250,0.16)";
      bubbleStyle.borderRadius = "10px 10px 3px 10px";
      bubbleStyle.marginLeft = "auto";
    } else {
      bubbleStyle.background = "var(--panel-bg)";
      bubbleStyle.border = "1px solid var(--border)";
      bubbleStyle.borderLeft = "2px solid rgba(124,108,247,0.65)";
      bubbleStyle.borderRadius = "10px 10px 10px 3px";
    }

    var bubble = el("div", { style: bubbleStyle }, cleanContent(m.content));
    var row = el("div", {
      style: { display: "flex", flexDirection: "column", marginBottom: "4px" },
    });
    row.appendChild(el("div", {
      style: {
        fontSize: "11px", color: "var(--text-secondary)", opacity: "0.7",
        marginBottom: "2px", textAlign: isUser ? "right" : "left",
      },
    }, (isUser ? "" : "🐾 ") + fmtTime(m.ts)));
    row.appendChild(bubble);
    if (!isUser && m.thinking) row.appendChild(thinkingNode(m.thinking));
    return row;
  }

  function proactiveNode(m) {
    return el("div", {
      style: {
        textAlign: "center", fontSize: "12px", margin: "8px 0",
        color: "var(--text-secondary)", fontStyle: "italic",
      },
    }, t("historyProactive") + " · " + fmtTime(m.ts));
  }

  function themePicker(themes, picked, onChange) {
    var names = Object.keys(themes).sort();
    if (names.length <= 1) return null;
    var select = el("select", {
      style: {
        fontSize: "12px", padding: "3px 8px", borderRadius: "6px",
        border: "1px solid var(--border)", background: "var(--panel-bg)",
        color: "inherit",
      },
    });
    for (var i = 0; i < names.length; i++) {
      var opt = el("option", { value: names[i] }, names[i] === "default" ? "default" : names[i]);
      if (names[i] === picked) opt.setAttribute("selected", "selected");
      select.appendChild(opt);
    }
    select.addEventListener("change", function () { onChange(select.value); });
    return select;
  }

  function renderList(container, themes, themeId) {
    container.innerHTML = "";
    var msgs = Array.isArray(themes[themeId]) ? themes[themeId] : [];
    var cutoff = Date.now() - DAY_MS;
    var recent = msgs.filter(function (m) {
      return m && typeof m.ts === "number" && m.ts >= cutoff;
    });
    // Newest first — the missed reply should be the first thing seen.
    recent.sort(function (a, b) { return b.ts - a.ts; });

    if (!msgs.length) {
      container.appendChild(el("div", {
        style: { textAlign: "center", color: "var(--text-secondary)", padding: "24px 0", fontSize: "13px" },
      }, t("historyEmpty")));
      return;
    }
    if (!recent.length) {
      container.appendChild(el("div", {
        style: { textAlign: "center", color: "var(--text-secondary)", padding: "24px 0", fontSize: "13px" },
      }, t("historyNoTs")));
      return;
    }

    for (var i = 0; i < recent.length; i++) {
      var m = recent[i];
      if (isProactive(m)) container.appendChild(proactiveNode(m));
      else container.appendChild(messageNode(m));
    }
  }

  async function render(parent) {
    try {
      var data = { themes: {} };
      try {
        if (window.minicpmSettings && typeof window.minicpmSettings.getHistoryFile === "function") {
          data = await window.minicpmSettings.getHistoryFile();
        }
      } catch (e) { console.warn("history: load failed", e); }
      var themes = (data && data.themes) || {};

      // Default theme = the one with the most recent message (fallback:
      // last viewed, then "default").
      var newestTheme = "default", newestTs = -1;
      for (var key in themes) {
        if (!Object.prototype.hasOwnProperty.call(themes, key)) continue;
        var arr = Array.isArray(themes[key]) ? themes[key] : [];
        for (var i = 0; i < arr.length; i++) {
          if (arr[i] && typeof arr[i].ts === "number" && arr[i].ts > newestTs) {
            newestTs = arr[i].ts; newestTheme = key;
          }
        }
      }
      var picked = (_lastTheme && themes[_lastTheme]) ? _lastTheme : newestTheme;

      var recentCount = 0;
      (Array.isArray(themes[picked]) ? themes[picked] : []).forEach(function (m) {
        if (m && typeof m.ts === "number" && m.ts >= Date.now() - DAY_MS && !isProactive(m)) recentCount++;
      });

      parent.appendChild(el("div", { style: { padding: "0 0 16px 0", borderBottom: "1px solid var(--border)" } },
        el("div", { style: { display: "flex", alignItems: "center", gap: "10px" } },
          el("h2", { style: { margin: "0", fontSize: "18px", fontWeight: "600", flex: "1" } }, t("historyTitle")),
          el("button", {
            style: {
              fontSize: "12px", padding: "4px 12px", borderRadius: "6px",
              border: "1px solid var(--border)", background: "transparent",
              color: "inherit", cursor: "pointer",
            },
            onClick: function () { void render(parent); },
          }, t("historyRefresh")),
        ),
        el("p", { style: { margin: "4px 0 0 0", fontSize: "13px", color: "var(--text-secondary)" } },
          t("historySubtitle") + " · " + recentCount),
      ));

      var pickerHost = el("div", { style: { minHeight: "1px" } });
      parent.appendChild(pickerHost);

      var list = el("div", { style: { padding: "4px 0 24px 0" } });
      parent.appendChild(list);

      function show(themeId) {
        _lastTheme = themeId;
        var picker = themePicker(themes, _lastTheme, show);
        pickerHost.innerHTML = "";
        if (picker) pickerHost.appendChild(picker);
        renderList(list, themes, _lastTheme);
      }
      show(picked);
    } catch (e) { console.warn("history: render error", e); }
  }

  function init(coreArg) {
    try {
      core = coreArg;
      helpers = core.helpers;
      core.tabs.history = { render: render };
    } catch (e) { console.warn("history: init error", e); }
  }

  root.ClawdSettingsTabHistory = { init };
})(typeof globalThis !== "undefined" ? globalThis : window);
