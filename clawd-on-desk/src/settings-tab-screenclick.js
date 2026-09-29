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

  // "总是允许查看屏幕" — binds screenObserveConsent (always/deny) and
  // pushes the change to the live sidecar so it takes effect immediately.
  function makeConsentToggle(label, desc) {
    try {
      var val = read("screenObserveConsent", "deny") === "always";
      var cb = el("input", {
        type: "checkbox", className: "toggle-input",
        checked: val ? "checked" : undefined,
        onchange: function() {
          var next = cb.checked ? "always" : "deny";
          try {
            if (window.settingsAPI && typeof window.settingsAPI.update === "function") {
              window.settingsAPI.update("screenObserveConsent", next);
            }
            if (window.settingsAPI && typeof window.settingsAPI.syncScreenConsent === "function") {
              window.settingsAPI.syncScreenConsent(next);
            }
          } catch (err) { console.warn("screenclick: consent save failed", err); }
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
    } catch(e) { console.warn("screenclick: makeConsentToggle error", e); return el("div"); }
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
      parent.appendChild(makeConsentToggle(t("screenclickAlwaysAllow"), t("screenclickAlwaysAllowDesc")));

      parent.appendChild(el("h3", { style: { margin: "20px 0 4px 0", fontSize: "15px", fontWeight: "600" } }, t("screenclickSectionOcr")));
      parent.appendChild(makeSelect("screenclick_ocrMode", t("screenclickOcrMode"), t("screenclickOcrModeDesc"), {
        local: t("screenclickOcrModeLocal"),
        api: t("screenclickOcrModeApi"),
      }, "local"));
      parent.appendChild(makeToggle("screenclick_chineseOcr", t("screenclickChineseOcr"), t("screenclickChineseOcrDesc")));
      parent.appendChild(makeTextInput("screenclick_ocrApiUrl", t("screenclickOcrApiUrl"), t("screenclickOcrApiUrlDesc"), "http://127.0.0.1:8000/ocr"));
      parent.appendChild(makeTextInput("screenclick_ocrApiKey", t("screenclickOcrApiKey"), t("screenclickOcrApiKeyDesc"), ""));
      parent.appendChild(makeTextInput("screenclick_ocrApiModel", t("screenclickOcrApiModel"), t("screenclickOcrApiModelDesc"), "gpt-4o-mini"));

      // ── Holo GUI agent (H Company) ───────────────────────────────────
      // The pet can DRIVE the desktop: it looks at the screen, asks the
      // Holo vision model what to do next, and executes real clicks /
      // typing / scrolling. Backend (holo_agent.py) is complete; this
      // panel is its only control surface.
      parent.appendChild(el("h3", { style: { margin: "20px 0 4px 0", fontSize: "15px", fontWeight: "600" } }, t("holoSectionTitle")));
      parent.appendChild(el("p", { style: { margin: "0 0 4px 0", fontSize: "12px", color: "var(--text-secondary)" } }, t("holoSectionDesc")));
      parent.appendChild(makeToggle("holo_enabled", t("holoEnabled"), t("holoEnabledDesc")));
      parent.appendChild(makeHoloSourceCard());
      parent.appendChild(makeHoloRunner());
      parent.appendChild(el("p", { style: { margin: "8px 0 0 0", fontSize: "11px", color: "var(--text-secondary)" } }, t("holoRestartHint")));
    } catch(e) { console.warn("screenclick: render error", e); }
  }

  // Which AI actually drives the clicks. Presets fill the address so the
  // user never has to remember ports; "builtin" is the pet's own
  // llama-server (127.0.0.1:18766), the rest are local servers that run
  // the model for us. Local endpoints need no API key.
  var HOLO_SOURCES = [
    { id: "lmstudio", url: "http://127.0.0.1:1234/v1", local: true },
    { id: "ollama", url: "http://127.0.0.1:11434/v1", local: true },
    { id: "builtin", url: "http://127.0.0.1:18766/v1", local: true },
    { id: "hcompany", url: "https://api.hcompany.ai/v1/", local: false },
    { id: "custom", url: "", local: true },
  ];

  function makeHoloSourceCard() {
    var card = el("div", { style: { background: "var(--panel-bg)", borderRadius: "8px", padding: "16px", marginTop: "12px", border: "1px solid var(--border)", display: "flex", flexDirection: "column", gap: "10px" } });
    card.appendChild(el("div", null,
      el("div", { style: { fontSize: "14px", fontWeight: "500", marginBottom: "4px" } }, t("holoSource")),
      el("div", { style: { fontSize: "12px", color: "var(--text-secondary)" } }, t("holoSourceDesc")),
    ));

    var curUrl = read("holo_base_url", "");
    var curKey = read("holo_api_key", "");
    var curModel = read("holo_model", "");

    // Preset picker
    var srcSel = el("select", { className: "setting-select", style: { padding: "6px 10px", fontSize: "13px", border: "1px solid var(--border)", borderRadius: "4px", background: "var(--bg)", color: "var(--text-primary)", maxWidth: "360px" } });
    var matched = HOLO_SOURCES.find(function(s) { return s.url && s.url === curUrl; }) || HOLO_SOURCES[4];
    for (var si = 0; si < HOLO_SOURCES.length; si++) {
      var s = HOLO_SOURCES[si];
      var o = el("option", { value: s.id }, t("holoSource_" + s.id));
      if (s.id === matched.id) o.selected = true;
      srcSel.appendChild(o);
    }
    srcSel.onchange = function() {
      var pick = HOLO_SOURCES.find(function(s) { return s.id === srcSel.value; }) || HOLO_SOURCES[4];
      if (pick.url) { urlInput.value = pick.url; save("holo_base_url", pick.url); }
      keyInput.disabled = !!pick.local;
      hint.textContent = pick.local ? t("holoLocalNoKey") : t("holoCloudNeedsKey");
    };

    var urlLabel = el("div", { style: { fontSize: "12px", color: "var(--text-secondary)" } }, t("holoBaseUrl"));
    var urlInput = el("input", { type: "text", className: "setting-input", value: curUrl, placeholder: "http://127.0.0.1:1234/v1",
      style: { padding: "6px 10px", fontSize: "13px", border: "1px solid var(--border)", borderRadius: "4px", background: "var(--bg)", color: "var(--text-primary)", width: "100%", maxWidth: "420px", boxSizing: "border-box" },
      onchange: function() { save("holo_base_url", urlInput.value.trim()); } });
    var keyLabel = el("div", { style: { fontSize: "12px", color: "var(--text-secondary)" } }, t("holoApiKey"));
    var keyInput = el("input", { type: "password", className: "setting-input", value: curKey, placeholder: "hk-...",
      style: { padding: "6px 10px", fontSize: "13px", border: "1px solid var(--border)", borderRadius: "4px", background: "var(--bg)", color: "var(--text-primary)", width: "100%", maxWidth: "420px", boxSizing: "border-box" },
      onchange: function() { save("holo_api_key", keyInput.value.trim()); } });
    var modelLabel = el("div", { style: { fontSize: "12px", color: "var(--text-secondary)" } }, t("holoModel"));
    var modelInput = el("input", { type: "text", className: "setting-input", value: curModel, placeholder: "holo1.5-3b / holo3-1-35b-a3b",
      style: { padding: "6px 10px", fontSize: "13px", border: "1px solid var(--border)", borderRadius: "4px", background: "var(--bg)", color: "var(--text-primary)", width: "100%", maxWidth: "420px", boxSizing: "border-box" },
      onchange: function() { save("holo_model", modelInput.value.trim()); } });
    var hint = el("div", { style: { fontSize: "11px", color: "var(--text-secondary)" } },
      matched.local ? t("holoLocalNoKey") : t("holoCloudNeedsKey"));
    keyInput.disabled = !!matched.local;

    var result = el("div", { style: { fontSize: "12px", color: "var(--text-secondary)" } }, "");
    var testBtn = el("button", {
      className: "btn-secondary",
      style: { padding: "6px 14px", fontSize: "13px", borderRadius: "4px", cursor: "pointer", alignSelf: "flex-start" },
      onclick: async function() {
        testBtn.disabled = true;
        result.textContent = t("holoTesting");
        try {
          var ret = await window.settingsAPI.holoTest(urlInput.value.trim(), keyInput.value.trim());
          if (ret && ret.status === "ok" && ret.ok) {
            var names = (ret.models || []).slice(0, 6).join(", ");
            result.textContent = t("holoTestOk") + (names ? "  →  " + names : "");
          } else {
            result.textContent = t("holoTestFail") + ((ret && (ret.message || ret.error)) || "");
          }
        } catch (err) {
          result.textContent = t("holoTestFail") + (err && err.message || err);
        } finally { testBtn.disabled = false; }
      },
    }, t("holoTest"));

    function save(key, value) {
      try { if (window.settingsAPI && window.settingsAPI.update) window.settingsAPI.update(key, value); } catch (e) {}
    }

    card.appendChild(srcSel);
    card.appendChild(urlLabel); card.appendChild(urlInput);
    card.appendChild(keyLabel); card.appendChild(keyInput);
    card.appendChild(modelLabel); card.appendChild(modelInput);
    card.appendChild(hint);
    card.appendChild(testBtn);
    card.appendChild(result);
    return card;
  }

  // Task runner card: type a goal in natural language, the agent works it
  // out step by step on your real desktop. Status + cancel included.
  function makeHoloRunner() {
    var card = el("div", { style: { background: "var(--panel-bg)", borderRadius: "8px", padding: "16px", marginTop: "12px", border: "1px solid var(--border)" } });
    var status = el("div", { style: { fontSize: "12px", color: "var(--text-secondary)", marginBottom: "8px" } }, t("holoStatusIdle"));
    var input = el("input", {
      type: "text",
      className: "setting-input",
      placeholder: t("holoTaskPlaceholder"),
      style: { padding: "6px 10px", fontSize: "13px", border: "1px solid var(--border)", borderRadius: "4px", background: "var(--bg)", color: "var(--text-primary)", width: "100%", boxSizing: "border-box" },
    });
    var runBtn = el("button", {
      className: "btn-primary",
      style: { marginTop: "8px", marginRight: "8px", padding: "6px 16px", fontSize: "13px", borderRadius: "4px", cursor: "pointer" },
      onclick: async function() {
        var task = (input.value || "").trim();
        if (!task) return;
        if (!window.settingsAPI || typeof window.settingsAPI.holoRun !== "function") {
          status.textContent = t("holoUnavailable");
          return;
        }
        runBtn.disabled = true;
        cancelBtn.disabled = false;
        status.textContent = t("holoRunning");
        try {
          var ret = await window.settingsAPI.holoRun(task);
          status.textContent = (ret && (ret.status === "ok" ? (ret.summary || ret.message || t("holoDone")) : (ret.message || t("holoFailed"))));
        } catch (err) {
          status.textContent = t("holoFailed") + (err && err.message || err);
        } finally {
          runBtn.disabled = false;
          cancelBtn.disabled = true;
        }
      },
    }, t("holoRun"));
    var cancelBtn = el("button", {
      className: "btn-secondary",
      disabled: "disabled",
      style: { marginTop: "8px", padding: "6px 16px", fontSize: "13px", borderRadius: "4px", cursor: "pointer" },
      onclick: async function() {
        try { if (window.settingsAPI && window.settingsAPI.holoCancel) await window.settingsAPI.holoCancel(); } catch (e) {}
        status.textContent = t("holoCancelled");
      },
    }, t("holoCancel"));

    card.appendChild(el("div", { style: { fontSize: "14px", fontWeight: "500", marginBottom: "4px" } }, t("holoRunnerTitle")));
    card.appendChild(el("div", { style: { fontSize: "12px", color: "var(--text-secondary)", marginBottom: "10px" } }, t("holoRunnerDesc")));
    card.appendChild(input);
    card.appendChild(el("div", null, runBtn, cancelBtn));
    card.appendChild(el("div", { style: { marginTop: "10px" } }, status));
    return card;
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
