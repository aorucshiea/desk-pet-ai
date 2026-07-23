"use strict";

// ── Model Providers settings tab ──
// Generic: supports any OpenAI-compatible provider.

(function initSettingsTabProviders(root) {
  let core = null;
  let helpers = null;
  let ops = null;
  let mounted = false;
  let _section = null;
  let _parent = null;
  let _editingProvider = null;

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
    try {
      if (window.settingsAPI && typeof window.settingsAPI.update === "function") {
        const live = (core.state.snapshot && core.state.snapshot.skills) || {};
        await window.settingsAPI.update("skills", { ...live, [field]: value });
      }
    } catch {}
  }

  async function saveProviders(providers) {
    try {
      if (window.settingsAPI && typeof window.settingsAPI.update === "function") {
        const live = (core.state.snapshot && core.state.snapshot.skills) || {};
        await window.settingsAPI.update("skills", { ...live, modelProviders: providers });
      }
      // Sync to providers.json and reload gateway
      if (window.minicpmSettings && typeof window.minicpmSettings.saveProvidersConfig === "function") {
        await window.minicpmSettings.saveProvidersConfig(providers);
      }
      if (window.minicpmSettings && typeof window.minicpmSettings.restartSidecar === "function") {
        await window.minicpmSettings.restartSidecar();
      }
    } catch {}
  }

  function renderHeader(parent) {
    parent.appendChild(el("div", { style: { padding: "0 0 16px 0", borderBottom: "1px solid var(--border)" } },
      el("h2", { style: { margin: "0 0 4px 0", fontSize: "18px", fontWeight: "600" } }, t("sidebarProviders")),
      el("p", { style: { margin: "0", fontSize: "13px", color: "var(--text-secondary)" } }, "Choose AI models for chat. Supports any OpenAI-compatible API."),
    ));
  }

  function renderContent(parent) {
    const section = el("div", {});
    const skills = (core.state.snapshot && core.state.snapshot.skills) || {};
    const defaultProvider = skills.defaultProvider || "local";
    const autoRoute = !!skills.autoRoute;
    const providers = Array.isArray(skills.modelProviders) ? [...skills.modelProviders] : [];

    // ── Active provider card ──
    const activeCard = el("div", {
      style: { background: "var(--panel-bg)", borderRadius: "8px", padding: "16px", marginTop: "16px", border: "1px solid var(--border)" },
    });
    activeCard.appendChild(el("h3", { style: { margin: "0 0 12px 0", fontSize: "15px", fontWeight: "600" } }, "Active Model"));

    const selRow = el("div", { style: { display: "flex", alignItems: "center", gap: "12px", marginBottom: "8px" } });
    selRow.appendChild(el("span", { style: { fontSize: "13px", whiteSpace: "nowrap" } }, "Current provider:"));
    const sel = el("select", { className: "setting-select", style: { flex: "1", maxWidth: "240px", fontSize: "13px" } });
    [["local", "Local (MiniCPM)"], ...providers.map((p) => [p.provider, p.model ? `${p.provider} / ${p.model}` : p.provider])]
      .forEach(([val, label]) => {
        const opt = el("option", { value: val }, label);
        if (val === defaultProvider) opt.selected = true;
        sel.appendChild(opt);
      });
    sel.addEventListener("change", () => saveField("defaultProvider", sel.value));
    selRow.appendChild(sel);
    activeCard.appendChild(selRow);

    const arRow = el("div", { style: { display: "flex", alignItems: "center", justifyContent: "space-between" } });
    arRow.appendChild(el("span", { style: { fontSize: "13px" } }, "Auto-route (smart model selection)"));
    const arWrap = el("label", { className: "toggle-switch" });
    const arCb = el("input", {
      type: "checkbox", className: "toggle-input",
      checked: autoRoute ? "checked" : undefined,
      onchange: () => { saveField("autoRoute", arCb.checked); },
    });
    arWrap.appendChild(arCb); arWrap.appendChild(el("span", { className: "toggle-slider" }));
    arRow.appendChild(arWrap);
    activeCard.appendChild(arRow);
    parent.appendChild(activeCard);

    // ── API providers card ──
    const apiCard = el("div", {
      style: { background: "var(--panel-bg)", borderRadius: "8px", padding: "16px", marginTop: "12px", border: "1px solid var(--border)" },
    });
    apiCard.appendChild(el("h3", { style: { margin: "0 0 4px 0", fontSize: "15px", fontWeight: "600" } }, "API Providers"));
    apiCard.appendChild(el("p", { style: { margin: "0 0 12px 0", fontSize: "12px", color: "var(--text-secondary)" } }, "DeepSeek, Ollama, OpenRouter, Groq, etc."));

    if (providers.length === 0) {
      apiCard.appendChild(el("p", { style: { color: "var(--text-secondary)", fontSize: "13px", fontStyle: "italic" } }, "No API providers configured."));
    } else {
      for (const p of providers) {
        if (_editingProvider === p.provider) {
          const card = el("div", {
            style: { padding: "10px 12px", marginBottom: "8px", borderRadius: "6px", border: "1px solid var(--accent)", background: "var(--bg)", color: "var(--text-primary)" },
          });
          card.appendChild(el("div", { style: { fontSize: "14px", fontWeight: "600", color: "var(--accent)", marginBottom: "8px" } }, "Edit: " + p.provider));

          const editFields = [
            { key: "apiKey", label: "Key", type: "password", placeholder: "sk-..." },
            { key: "baseUrl", label: "URL", type: "text", placeholder: "https://api.openai.com/v1" },
            { key: "model", label: "Model", type: "text", placeholder: "model name" },
            { key: "contextWindow", label: "Context", type: "text", placeholder: "max tokens" },
          ];
          const editInputs = {};
          for (const ef of editFields) {
            const row = el("div", { style: { display: "flex", alignItems: "center", margin: "4px 0", gap: "8px" } });
            row.appendChild(el("label", { style: { minWidth: "44px", fontSize: "12px", color: "var(--text-secondary)" } }, ef.label));
            const inp = el("input", {
              type: ef.type,
              placeholder: ef.placeholder,
              value: p[ef.key] != null ? String(p[ef.key]) : "",
              style: { flex: "1", padding: "4px 6px", fontSize: "12px", border: "1px solid var(--border)", borderRadius: "4px", background: "var(--bg)", color: "var(--text-primary)" },
            });
            editInputs[ef.key] = inp; row.appendChild(inp); card.appendChild(row);
          }
          const thinkRow = el("div", { style: { display: "flex", alignItems: "center", margin: "4px 0", gap: "8px" } });
          thinkRow.appendChild(el("label", { style: { minWidth: "44px", fontSize: "12px", color: "var(--text-secondary)" } }, "Think"));
          const thinkCb = el("input", { type: "checkbox", style: { margin: "0 8px" } });
          thinkCb.checked = !!p.thinking;
          thinkRow.appendChild(thinkCb);
          thinkRow.appendChild(el("span", { style: { fontSize: "11px", color: "var(--text-secondary)" } }, "Enable thinking mode"));
          card.appendChild(thinkRow);
          const effortRow = el("div", { style: { display: "flex", alignItems: "center", margin: "4px 0", gap: "8px" } });
          effortRow.appendChild(el("label", { style: { minWidth: "44px", fontSize: "12px", color: "var(--text-secondary)" } }, "Effort"));
          const effortSel = el("select", { style: { padding: "4px 6px", fontSize: "12px", border: "1px solid var(--border)", borderRadius: "4px", background: "var(--bg)", color: "var(--text-primary)" } });
          ["", "low", "medium", "high", "xhigh", "max"].forEach((v) => {
            const opt = el("option", { value: v }, v || "default");
            if (v === (p.reasoningEffort || "")) opt.selected = true;
            effortSel.appendChild(opt);
          });
          effortRow.appendChild(effortSel);
          card.appendChild(effortRow);

          const btnRow = el("div", { style: { display: "flex", gap: "8px", marginTop: "8px" } });
          const saveBtn = el("button", {
            style: { padding: "4px 14px", fontSize: "12px", background: "#1677ff", color: "#fff", border: "none", borderRadius: "4px", cursor: "pointer" },
            onclick: async () => {
              const updated = {
                ...p,
                apiKey: editInputs.apiKey.value.trim() || p.apiKey,
                baseUrl: editInputs.baseUrl.value.trim() || "https://api.openai.com/v1",
                model: editInputs.model.value.trim() || null,
                thinking: thinkCb.checked,
                reasoningEffort: effortSel.value || null,
                contextWindow: Number(editInputs.contextWindow.value) || null,
              };
              const newProviders = providers.map((x) => x.provider === p.provider ? updated : x);
              await saveProviders(newProviders);
              _editingProvider = null;
              renderAll();
            },
          }, "Save");
          btnRow.appendChild(saveBtn);
          const cancelBtn = el("button", {
            style: { padding: "4px 14px", fontSize: "12px", background: "var(--bg)", color: "var(--text-secondary)", border: "1px solid var(--border)", borderRadius: "4px", cursor: "pointer" },
            onclick: () => { _editingProvider = null; renderAll(); },
          }, "Cancel");
          btnRow.appendChild(cancelBtn);
          card.appendChild(btnRow);
          apiCard.appendChild(card);
        } else {
          const card = el("div", {
            style: { padding: "10px 12px", marginBottom: "8px", borderRadius: "6px", border: "1px solid var(--border)", background: "var(--bg)", color: "var(--text-primary)" },
          });
          const top = el("div", { style: { display: "flex", justifyContent: "space-between", alignItems: "center" } });
          const left = el("div", {});
          left.appendChild(el("div", { style: { fontSize: "14px", fontWeight: "600", color: "var(--accent)" } }, p.provider));
          left.appendChild(el("div", { style: { fontSize: "11px", color: "var(--text-secondary)", fontFamily: "monospace", marginTop: "2px" } },
            p.baseUrl ? p.baseUrl.slice(0, 50) + (p.baseUrl.length > 50 ? "..." : "") : ""
          ));
          if (p.thinking != null) left.appendChild(el("span", {
            style: { fontSize: "10px", color: p.thinking ? "#52c41a" : "var(--text-secondary)", marginTop: "2px" },
          }, p.thinking ? "thinking: on" : "thinking: off"));
          if (p.reasoningEffort) left.appendChild(el("span", {
            style: { fontSize: "10px", color: "var(--accent)", marginLeft: "8px" },
          }, "effort: " + p.reasoningEffort));
          top.appendChild(left);

          const right = el("div", { style: { display: "flex", alignItems: "center", gap: "8px" } });
          if (p.model) right.appendChild(el("span", {
            style: { fontSize: "11px", background: "color-mix(in srgb, var(--accent) 12%, transparent)", color: "var(--accent)", padding: "2px 8px", borderRadius: "10px" },
          }, p.model));
          if (p.contextWindow) right.appendChild(el("span", {
            style: { fontSize: "11px", background: "color-mix(in srgb, var(--accent) 12%, transparent)", color: "var(--accent)", padding: "2px 8px", borderRadius: "10px", marginLeft: "4px" },
          }, "ctx:" + p.contextWindow));
          const editBtn = el("button", {
            style: { fontSize: "11px", padding: "3px 10px", border: "1px solid var(--border)", borderRadius: "4px", background: "var(--bg)", color: "var(--text-primary)", cursor: "pointer" },
            onclick: () => { _editingProvider = p.provider; renderAll(); },
          }, "Edit");
          right.appendChild(editBtn);
          const delBtn = el("button", {
            style: { fontSize: "11px", padding: "3px 10px", border: "1px solid #ff4d4f", borderRadius: "4px", background: "var(--bg)", color: "#ff4d4f", cursor: "pointer" },
            onclick: async () => {
              const newProviders = providers.filter((x) => x.provider !== p.provider);
              await saveProviders(newProviders);
              renderAll();
            },
          }, "Remove");
          right.appendChild(delBtn);
          top.appendChild(right);
          card.appendChild(top);
          apiCard.appendChild(card);
        }
      }
    }

    // Add form
    const form = el("div", {
      style: { marginTop: "12px", padding: "12px", border: "1px solid var(--border)", borderRadius: "6px", background: "var(--bg)", color: "var(--text-primary)" },
    });
    form.appendChild(el("h4", { style: { margin: "0 0 8px 0", fontSize: "13px", fontWeight: "600" } }, "Add Provider"));
    const fields = [
      { key: "provider", label: "ID", placeholder: "e.g. deepseek", hint: "short identifier" },
      { key: "apiKey", label: "Key", placeholder: "sk-...", hint: "API key" },
      { key: "baseUrl", label: "URL", placeholder: "https://api.deepseek.com/v1", hint: "endpoint" },
      { key: "model", label: "Model", placeholder: "deepseek-chat", hint: "model name" },
      { key: "contextWindow", label: "Context", placeholder: "131072", hint: "max tokens" },
    ];
    const inputs = {};
    for (const fi of fields) {
      const row = el("div", { style: { display: "flex", alignItems: "center", margin: "6px 0", gap: "8px" } });
      row.appendChild(el("label", { style: { minWidth: "44px", fontSize: "12px", color: "var(--text-secondary)" } }, fi.label));
      const inp = el("input", {
        type: (fi.key === "apiKey") ? "password" : "text",
        placeholder: fi.placeholder,
        style: { flex: "1", padding: "5px 8px", fontSize: "13px", border: "1px solid var(--border)", borderRadius: "4px", background: "var(--bg)", color: "var(--text-primary)" },
      });
      inputs[fi.key] = inp; row.appendChild(inp); form.appendChild(row);
    }
    // Thinking toggle row
    const thinkRow = el("div", { style: { display: "flex", alignItems: "center", margin: "6px 0", gap: "8px" } });
    thinkRow.appendChild(el("label", { style: { minWidth: "44px", fontSize: "12px", color: "var(--text-secondary)" } }, "Think"));
    const thinkCb = el("input", { type: "checkbox", style: { margin: "0 8px" } });
    thinkCb.checked = true; // default enabled
    thinkRow.appendChild(thinkCb);
    thinkRow.appendChild(el("span", { style: { fontSize: "11px", color: "var(--text-secondary)" } }, "Enable thinking mode"));
    form.appendChild(thinkRow);
    // Reasoning effort row
    const effortRow = el("div", { style: { display: "flex", alignItems: "center", margin: "6px 0", gap: "8px" } });
    effortRow.appendChild(el("label", { style: { minWidth: "44px", fontSize: "12px", color: "var(--text-secondary)" } }, "Effort"));
    const effortSel = el("select", { style: { padding: "5px 8px", fontSize: "13px", border: "1px solid var(--border)", borderRadius: "4px", background: "var(--bg)", color: "var(--text-primary)" } });
    ["", "low", "medium", "high", "xhigh", "max"].forEach((v) => {
      effortSel.appendChild(el("option", { value: v }, v || "default"));
    });
    effortRow.appendChild(effortSel);
    form.appendChild(effortRow);
    const addBtn = el("button", {
      style: { marginTop: "8px", padding: "6px 20px", fontSize: "13px", background: "var(--accent)", color: "#fff", border: "none", borderRadius: "4px", cursor: "pointer" },
      onclick: async () => {
        const id = (inputs.provider.value || "").replace(/\s+/g, "").toLowerCase();
        if (!id || !(inputs.apiKey.value || "").trim()) return;
        const newProviders = providers.filter((x) => x.provider !== id);
        newProviders.push({
          provider: id,
          apiKey: inputs.apiKey.value.trim(),
          baseUrl: inputs.baseUrl.value.trim() || "https://api.openai.com/v1",
          model: inputs.model.value.trim() || "gpt-4o",
          thinking: thinkCb.checked,
          reasoningEffort: effortSel.value || null,
          contextWindow: Number(inputs.contextWindow.value) || null,
        });
        await saveProviders(newProviders);
        inputs.provider.value = inputs.apiKey.value = inputs.baseUrl.value = inputs.model.value = inputs.contextWindow.value = "";
        renderAll();
      },
    }, "Add Provider");
    form.appendChild(addBtn);
    apiCard.appendChild(form);
    apiCard.setAttribute("data-section", "providers");

    // Restart
    apiCard.appendChild(el("p", { style: { color: "var(--text-secondary)", fontSize: "11px", marginTop: "10px" } }, "New providers need sidecar restart to take effect."));
    const restartBtn = el("button", {
      style: { marginTop: "4px", padding: "6px 16px", fontSize: "13px", background: "var(--accent)", color: "#fff", border: "none", borderRadius: "4px", cursor: "pointer" },
      onclick: async () => {
        restartBtn.textContent = "Restarting..."; restartBtn.disabled = true;
        try { if (window.minicpmSettings && typeof window.minicpmSettings.restartSidecar === "function") await window.minicpmSettings.restartSidecar(); } catch {}
        setTimeout(() => { restartBtn.textContent = "Restart Sidecar"; restartBtn.disabled = false; }, 5000);
      },
    }, "Restart Sidecar");
    apiCard.appendChild(restartBtn);
    parent.appendChild(apiCard);
  }

  async function render(parent) {
    cleanupTimers();
    mounted = true;
    _parent = parent;
    renderAll();
  }

  function renderAll() {
    if (!_parent) return;
    _parent.innerHTML = "";
    renderHeader(_parent);
    renderContent(_parent);
  }

  function init(coreArg) {
    core = coreArg; helpers = core.helpers; ops = core.ops;
    core.tabs.providers = { render: (parent) => { void render(parent); } };
  }

  root.ClawdSettingsTabProviders = { init };
})(typeof globalThis !== "undefined" ? globalThis : window);
