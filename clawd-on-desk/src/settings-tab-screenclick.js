"use strict";

(function initSettingsTabScreenClick(root) {
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
    } catch (ex) { console.warn("screenclick: el error", ex); return document.createElement("div"); }
  }

  function read(key, fallback) {
    try {
      var s = core.state.snapshot;
      return s && key in s ? s[key] : fallback;
    } catch(e) { return fallback; }
  }

  function makeToggle(key, label, desc) {
    try {
      var val = !!read(key, false);
      var cb = el("input", {
        type: "checkbox", className: "toggle-input",
        checked: val ? "checked" : undefined,
        onchange: function() {
          try {
            if (window.settingsAPI && typeof window.settingsAPI.update === "function") {
              window.settingsAPI.update(key, cb.checked);
            }
          } catch (err) { console.warn("screenclick: save failed", err); }
        },
      });
      var card = el("div", { style: { background: "var(--panel-bg)", borderRadius: "8px", padding: "16px", marginTop: "12px", border: "1px solid var(--border)" } });
      var row = el("div", { style: { display: "flex", justifyContent: "space-between", alignItems: "center" } },
        el("div", null,
          el("div", { style: { fontSize: "14px", fontWeight: "500", marginBottom: "4px" } }, label),
          desc ? el("div", { style: { fontSize: "12px", color: "var(--text-secondary)" } }, desc) : null,
        ),
        el("label", { className: "toggle-switch" }, cb, el("span", { className: "toggle-slider" })),
      );
      card.appendChild(row);
      return card;
    } catch(e) { console.warn("screenclick: makeToggle error", e); return el("div"); }
  }

  function makeSelect(key, label, desc, options, fallback) {
    try {
      var val = read(key, fallback);
      var sel = el("select", {
        className: "setting-select",
        style: { padding: "6px 10px", fontSize: "13px", border: "1px solid var(--border)", borderRadius: "4px", background: "var(--bg)", color: "var(--text-primary)", maxWidth: "280px" },
        onchange: function() {
          try {
            if (window.settingsAPI && typeof window.settingsAPI.update === "function") {
              window.settingsAPI.update(key, sel.value);
            }
          } catch (err) { console.warn("screenclick: save failed", err); }
        },
      });
      for (var optKey in options) {
        if (!Object.prototype.hasOwnProperty.call(options, optKey)) continue;
        var opt = el("option", { value: optKey }, options[optKey]);
        if (optKey === val) opt.selected = true;
        sel.appendChild(opt);
      }
      var card = el("div", { style: { background: "var(--panel-bg)", borderRadius: "8px", padding: "16px", marginTop: "12px", border: "1px solid var(--border)" } });
      card.appendChild(el("div", { style: { display: "flex", justifyContent: "space-between", alignItems: "center" } },
        el("div", null,
          el("div", { style: { fontSize: "14px", fontWeight: "500", marginBottom: "4px" } }, label),
          desc ? el("div", { style: { fontSize: "12px", color: "var(--text-secondary)" } }, desc) : null,
        ),
        sel,
      ));
      return card;
    } catch(e) { console.warn("screenclick: makeSelect error", e); return el("div"); }
  }

  function makeTextInput(key, label, desc, placeholder) {
    try {
      var val = read(key, "");
      var input = el("input", {
        type: "text", className: "setting-input",
        value: val,
        placeholder: placeholder || "",
        style: { padding: "6px 10px", fontSize: "13px", border: "1px solid var(--border)", borderRadius: "4px", background: "var(--bg)", color: "var(--text-primary)", width: "100%", maxWidth: "360px", boxSizing: "border-box" },
        onchange: function() {
          try {
            if (window.settingsAPI && typeof window.settingsAPI.update === "function") {
              window.settingsAPI.update(key, input.value);
            }
          } catch (err) { console.warn("screenclick: save failed", err); }
        },
      });
      var card = el("div", { style: { background: "var(--panel-bg)", borderRadius: "8px", padding: "16px", marginTop: "12px", border: "1px solid var(--border)" } });
      card.appendChild(el("div", { style: { display: "flex", flexDirection: "column", gap: "8px" } },
        el("div", null,
          el("div", { style: { fontSize: "14px", fontWeight: "500", marginBottom: "4px" } }, label),
          desc ? el("div", { style: { fontSize: "12px", color: "var(--text-secondary)" } }, desc) : null,
        ),
        input,
      ));
      return card;
    } catch(e) { console.warn("screenclick: makeTextInput error", e); return el("div"); }
  }

  function render(parent) {
    try {
      parent.appendChild(el("div", { style: { padding: "0 0 16px 0", borderBottom: "1px solid var(--border)" } },
        el("h2", { style: { margin: "0 0 4px 0", fontSize: "18px", fontWeight: "600" } }, t("screenclickTitle")),
        el("p", { style: { margin: "0", fontSize: "13px", color: "var(--text-secondary)" } }, t("screenclickSubtitle")),
      ));

      parent.appendChild(el("h3", { style: { margin: "20px 0 4px 0", fontSize: "15px", fontWeight: "600" } }, t("screenclickSectionGeneral")));
      parent.appendChild(makeToggle("screenclick_enabled", t("screenclickEnabled"), t("screenclickEnabledDesc")));
      parent.appendChild(makeToggle("screenclick_clickEnabled", t("screenclickClickEnabled"), t("screenclickClickEnabledDesc")));

      parent.appendChild(el("h3", { style: { margin: "20px 0 4px 0", fontSize: "15px", fontWeight: "600" } }, t("screenclickSectionOcr")));
      parent.appendChild(makeSelect("screenclick_ocrMode", t("screenclickOcrMode"), t("screenclickOcrModeDesc"), {
        local: t("screenclickOcrModeLocal"),
        api: t("screenclickOcrModeApi"),
      }, "local"));
      parent.appendChild(makeToggle("screenclick_chineseOcr", t("screenclickChineseOcr"), t("screenclickChineseOcrDesc")));
      parent.appendChild(makeTextInput("screenclick_ocrApiUrl", t("screenclickOcrApiUrl"), t("screenclickOcrApiUrlDesc"), "http://127.0.0.1:8000/ocr"));
      parent.appendChild(makeTextInput("screenclick_ocrApiKey", t("screenclickOcrApiKey"), t("screenclickOcrApiKeyDesc"), ""));
      parent.appendChild(makeTextInput("screenclick_ocrApiModel", t("screenclickOcrApiModel"), t("screenclickOcrApiModelDesc"), "gpt-4o-mini"));
    } catch(e) { console.warn("screenclick: render error", e); }
  }

  function init(coreArg) {
    try {
      core = coreArg;
      helpers = core.helpers;
      core.tabs.screenclick = { render: render };
    } catch(e) { console.warn("screenclick: init error", e); }
  }

  root.ClawdSettingsTabScreenClick = { init: init };
})(typeof globalThis !== "undefined" ? globalThis : window);
