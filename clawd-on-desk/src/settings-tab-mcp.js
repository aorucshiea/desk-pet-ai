"use strict";

// ── MCP Servers settings tab ──

(function initSettingsTabMcp(root) {
  let core = null; let helpers = null; let ops = null; let mounted = false;
  let serversData = {}; let toolsList = []; let _card = null; let _parent = null;

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
      el("h2", { style: { margin: "0 0 4px 0", fontSize: "18px", fontWeight: "600" } }, t("sidebarMcp")),
      el("p", { style: { margin: "0", fontSize: "13px", color: "var(--text-secondary)" } }, "Connect MCP servers for tool calling. Claude Code .mcp.json compatible."),
    ));
  }

  function buildCard() {
    const card = el("div", {
      style: { background: "var(--panel-bg)", borderRadius: "8px", padding: "16px", marginTop: "16px", border: "1px solid var(--border)" },
    });
    const names = Object.keys(serversData);

    card.appendChild(el("h3", { style: { margin: "0 0 12px 0", fontSize: "15px", fontWeight: "600" } }, "Connected Servers"));

    if (names.length === 0) {
      card.appendChild(el("p", { style: { color: "var(--text-secondary)", fontSize: "13px", fontStyle: "italic" } }, "No MCP servers configured."));
    } else {
      for (const name of names) {
        const cfg = serversData[name] || {};
        const transport = cfg.type || "stdio";
        const detail = cfg.url ? `${cfg.url}` : `${cfg.command || "?"} ${(cfg.args || []).join(" ")}`;
        const item = el("div", {
          style: { padding: "10px 12px", marginBottom: "6px", borderRadius: "6px", border: "1px solid var(--border)", background: "var(--bg)", color: "var(--text-primary)" },
        });
        const top = el("div", { style: { display: "flex", justifyContent: "space-between", alignItems: "center" } });
        const left = el("div", {});
        left.appendChild(el("div", { style: { fontSize: "14px", fontWeight: "600" } }, name));
        left.appendChild(el("span", { style: { fontSize: "11px", color: "var(--text-secondary)", fontFamily: "monospace", marginTop: "2px" } }, `${transport}  ${detail}`));
        top.appendChild(left);
        const delBtn = el("button", {
          style: { fontSize: "11px", padding: "3px 10px", color: "#ff4d4f", border: "1px solid #ff4d4f", borderRadius: "4px", background: "var(--bg)", cursor: "pointer" },
          onclick: async () => {
            delete serversData[name]; await save(); refreshCard();
          },
        }, "Remove");
        top.appendChild(delBtn);
        item.appendChild(top);
        card.appendChild(item);
      }
    }

    // ── Available Tools ──
    if (toolsList.length > 0) {
      card.appendChild(el("h3", { style: { margin: "20px 0 12px 0", fontSize: "15px", fontWeight: "600" } }, "Available Tools"));

      // Group by server_name, builtin first
      const groups = {};
      for (const t of toolsList) {
        const sn = t.server_name || "unknown";
        if (!groups[sn]) groups[sn] = [];
        groups[sn].push(t);
      }
      const groupNames = Object.keys(groups).sort((a, b) => {
        if (a === "builtin") return -1;
        if (b === "builtin") return 1;
        return a.localeCompare(b);
      });

      for (const sn of groupNames) {
        const tools = groups[sn];
        const isBuiltin = sn === "builtin";
        const section = el("div", {
          style: { marginBottom: "10px", border: "1px solid var(--border)", borderRadius: "6px", overflow: "hidden" },
        });
        // Server header
        const header = el("div", {
          style: { padding: "8px 12px", background: isBuiltin ? "var(--accent)" : "var(--bg)", color: isBuiltin ? "#fff" : "var(--text-primary)", fontSize: "12px", fontWeight: "600", display: "flex", justifyContent: "space-between", alignItems: "center" },
        });
        header.appendChild(el("span", {}, isBuiltin ? "🔧 Built-in Tools" : `📡 ${sn}`));
        header.appendChild(el("span", { style: { fontSize: "11px", opacity: "0.8" } }, `${tools.length} tool${tools.length > 1 ? "s" : ""}`));
        section.appendChild(header);

        for (const tool of tools) {
          const toolItem = el("div", {
            style: { padding: "8px 12px", borderTop: "1px solid var(--border)", background: "var(--bg)", color: "var(--text-primary)" },
          });
          // Tool name + description
          const topRow = el("div", { style: { display: "flex", alignItems: "baseline", gap: "8px", marginBottom: "3px" } });
          topRow.appendChild(el("code", { style: { fontSize: "13px", fontWeight: "600", color: "var(--accent)" } }, tool.name));
          topRow.appendChild(el("span", { style: { fontSize: "12px", color: "var(--text-secondary)", flex: "1" } }, (tool.description || "").slice(0, 100)));
          toolItem.appendChild(topRow);

          // Collapsible input_schema
          if (tool.input_schema && Object.keys(tool.input_schema).length > 0) {
            const toggleId = "mcp-tool-schema-" + sn + "-" + tool.name;
            const toggle = el("span", {
              style: { fontSize: "11px", color: "var(--accent)", cursor: "pointer", userSelect: "none" },
              onclick: function () {
                const body = document.getElementById(toggleId);
                if (body) body.style.display = body.style.display === "none" ? "block" : "none";
              },
            }, "▸ schema");
            toolItem.appendChild(toggle);

            const schemaBody = el("div", {
              id: toggleId,
              style: { display: "none", marginTop: "4px", padding: "6px 8px", background: "var(--panel-bg)", borderRadius: "4px", fontSize: "11px", fontFamily: "monospace", color: "var(--text-secondary)", whiteSpace: "pre-wrap", wordBreak: "break-all", maxHeight: "200px", overflowY: "auto" },
            }, JSON.stringify(tool.input_schema, null, 2));
            toolItem.appendChild(schemaBody);
          }
          section.appendChild(toolItem);
        }
        card.appendChild(section);
      }
    }

    // Add form
    const form = el("div", { style: { marginTop: "12px", padding: "12px", border: "1px solid var(--border)", borderRadius: "6px", background: "var(--bg)", color: "var(--text-primary)" } });
    form.appendChild(el("h4", { style: { margin: "0 0 8px 0", fontSize: "13px", fontWeight: "600" } }, "Add Server"));
    const types = [
      { key: "name", label: "Name", placeholder: "my-server" },
      { key: "command", label: "Cmd", placeholder: "npx" },
      { key: "args", label: "Args", placeholder: "-y @scope/mcp-server" },
    ];
    const inputs = {};
    for (const fi of types) {
      const row = el("div", { style: { display: "flex", alignItems: "center", margin: "6px 0", gap: "8px" } });
      row.appendChild(el("label", { style: { minWidth: "40px", fontSize: "12px", color: "var(--text-secondary)" } }, fi.label));
      const inp = el("input", { type: "text", placeholder: fi.placeholder, style: { flex: "1", padding: "5px 8px", fontSize: "13px", border: "1px solid var(--border)", borderRadius: "4px", background: "var(--bg)", color: "var(--text-primary)" } });
      inputs[fi.key] = inp; row.appendChild(inp); form.appendChild(row);
    }
    const addBtn = el("button", { style: { marginTop: "8px", padding: "6px 20px", fontSize: "13px", background: "var(--accent)", color: "#fff", border: "none", borderRadius: "4px", cursor: "pointer" }, onclick: async () => {
      const name = (inputs.name.value || "").trim();
      if (!name || !(inputs.command.value || "").trim()) return;
      serversData[name] = { type: "stdio", command: inputs.command.value.trim(), args: (inputs.args.value || "").split(/\s+/).filter(Boolean) };
      await save(); inputs.name.value = inputs.command.value = inputs.args.value = ""; refreshCard();
    } }, "Add Server");
    form.appendChild(addBtn);
    card.appendChild(form);

    card.appendChild(el("p", { style: { color: "var(--text-secondary)", fontSize: "11px", marginTop: "10px" } }, "Saved to ~/.minicpm/mcp.json. Needs sidecar restart to apply."));
    return card;
  }

  function refreshCard() { if (!_card || !_parent) return; const old = _card; _card = buildCard(); _parent.replaceChild(_card, old); }

  async function loadConfig() {
    try { if (window.minicpmSettings && typeof window.minicpmSettings.mcpGetConfig === "function") { const data = await window.minicpmSettings.mcpGetConfig(); serversData = (data && data.mcpServers) ? data.mcpServers : {}; } } catch { serversData = {}; }
  }

  async function loadTools() {
    try { if (window.minicpmSettings && typeof window.minicpmSettings.mcpListServers === "function") { const data = await window.minicpmSettings.mcpListServers(); toolsList = (data && data.tools) ? data.tools : []; } } catch { toolsList = []; }
  }

  async function save() {
    try { if (window.minicpmSettings && typeof window.minicpmSettings.mcpSaveConfig === "function") { await window.minicpmSettings.mcpSaveConfig(serversData); } } catch {}
  }

  async function render(parent) {
    cleanupTimers(); mounted = true; _parent = parent; parent.innerHTML = "";
    await loadConfig(); await loadTools(); renderHeader(parent);
    _card = buildCard(); parent.appendChild(_card);
  }

  function init(coreArg) { core = coreArg; helpers = core.helpers; ops = core.ops; core.tabs.mcp = { render: (parent) => { void render(parent); } }; }
  root.ClawdSettingsTabMcp = { init };
})(typeof globalThis !== "undefined" ? globalThis : window);
