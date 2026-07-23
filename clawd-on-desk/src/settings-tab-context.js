"use strict";

// Conversation Context tab — Cherry-Studio-style layout.
//
// Left column: [助手] [主题] tab switcher.
//   · 助手 tab: list of assistants (one per animation theme + Default).
//     Each row has a delete (🗑) button with tooltip + confirm dialog.
//     "+ 新建助手" opens a modal that forces the user to pick an
//     animation theme as the assistant's body.
//   · 主题 tab: list of topics under the currently selected assistant.
//     Each row has a delete (🗑) button. "+ 新建对话" creates a fresh topic.
// Right column: the history of the selected assistant + topic.
//
// All reads / mutations go through `window.minicpmSettings.execRenderer`
// which calls whitelisted `window.__*` helpers inside the live chat
// renderer. Nothing here switches the live chat bubble unless the user
// explicitly clicks a topic row (which calls __setActiveTopic).

(function initSettingsTabContext(root) {
  let core = null;
  let helpers = null;
  let ops = null;
  let mounted = false;
  let refreshTimer = null;

  // Selection state for the panel (NOT the live chat). The user can
  // browse assistants/topics without yanking the chat bubble.
  let _selectedAssistant = "default";
  let _selectedTopic = null; // null = active topic of the selected assistant
  let _leftTab = "assistants"; // "assistants" | "topics"

  function t(key) { return helpers.t(key); }

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
      if (Array.isArray(c)) { c.forEach((child) => { if (child instanceof Node) e.appendChild(child); else if (typeof child === "string" || typeof child === "number") e.appendChild(document.createTextNode(String(child))); }); continue; }
      if (typeof c === "string" || typeof c === "number") e.appendChild(document.createTextNode(String(c)));
      else if (c instanceof Node) e.appendChild(c);
    }
    return e;
  }

  // === Data accessors (all via execRenderer) ===========================

  async function listAssistants() {
    if (!window.minicpmSettings || typeof window.minicpmSettings.execRenderer !== "function") return [];
    try {
      const themesById = {};
      const themes = core && core.runtime && core.runtime.themeList;
      if (Array.isArray(themes)) {
        for (const th of themes) {
          if (!th || !th.id) continue;
          themesById[th.id] = (typeof th.name === "string")
            ? th.name
            : (th.name && typeof th.name === "object" ? (th.name.en || th.name.zh) : th.id);
        }
      }
      const r = await window.minicpmSettings.execRenderer("__listAssistants", [themesById]);
      return Array.isArray(r) ? r : [];
    } catch { return []; }
  }

  async function listTopics(assistantId) {
    if (!window.minicpmSettings || typeof window.minicpmSettings.execRenderer !== "function") return [];
    try {
      const r = await window.minicpmSettings.execRenderer("__listTopicsFor", [assistantId]);
      return Array.isArray(r) ? r : [];
    } catch { return []; }
  }

  async function getHistory(assistantId, topicId) {
    if (!window.minicpmSettings || typeof window.minicpmSettings.execRenderer !== "function") return [];
    try {
      const r = await window.minicpmSettings.execRenderer("__getChatHistoryFor",
        topicId ? [assistantId, topicId] : [assistantId]);
      return Array.isArray(r) ? r : [];
    } catch { return []; }
  }

  async function clearHistory(assistantId, topicId) {
    if (!window.minicpmSettings || typeof window.minicpmSettings.execRenderer !== "function") return false;
    try {
      const r = await window.minicpmSettings.execRenderer("__clearChatHistoryFor",
        topicId ? [assistantId, topicId] : [assistantId]);
      return !!(r && r.ok);
    } catch { return false; }
  }

  async function createAssistant(name) {
    try {
      return await window.minicpmSettings.execRenderer("__createAssistant", [name]);
    } catch { return { ok: false }; }
  }

  async function deleteAssistant(name) {
    try {
      return await window.minicpmSettings.execRenderer("__deleteAssistant", [name]);
    } catch { return { ok: false }; }
  }

  async function createTopic(assistantId) {
    try {
      return await window.minicpmSettings.execRenderer("__createTopic", [assistantId]);
    } catch { return { ok: false }; }
  }

  async function renameTopic(assistantId, topicId, newName) {
    try {
      return await window.minicpmSettings.execRenderer("__renameTopic", [assistantId, topicId, newName]);
    } catch { return { ok: false }; }
  }

  async function deleteTopic(assistantId, topicId) {
    try {
      return await window.minicpmSettings.execRenderer("__deleteTopic", [assistantId, topicId]);
    } catch { return { ok: false }; }
  }

  async function setActiveTopic(assistantId, topicId) {
    try {
      return await window.minicpmSettings.execRenderer("__setActiveTopic", [assistantId, topicId]);
    } catch { return { ok: false }; }
  }

  // === Rendering =======================================================

  function renderHeader(parent) {
    parent.appendChild(el("div", { style: { padding: "0 0 16px 0", borderBottom: "1px solid var(--border)" } },
      el("h2", { style: { margin: "0 0 4px 0", fontSize: "18px", fontWeight: "600" } }, t("sidebarContext")),
      el("p", { style: { margin: "0", fontSize: "13px", color: "var(--text-secondary)" } },
        "每个桌宠助手有独立的话题列表，切换主题不会丢失对话。"),
    ));
  }

  function softBtn(label, onClick, opts = {}) {
    const b = el("button", {
      style: Object.assign({
        fontSize: "12px", padding: "4px 12px",
        border: "1px solid var(--border)", borderRadius: "4px",
        background: "var(--bg)", color: "var(--text-primary)", cursor: "pointer",
      }, opts.style || {}),
    }, label);
    if (opts.accent) {
      b.style.borderColor = "var(--accent)";
      b.style.background = "var(--accent)";
      b.style.color = "#fff";
    }
    if (opts.danger) {
      b.style.borderColor = "#ff4d4f";
      b.style.color = "#ff4d4f";
    }
    b.addEventListener("click", (ev) => { ev.stopPropagation(); onClick(b); });
    return b;
  }

  /**
   * Create a danger delete button with trash icon, tooltip, hover
   * highlight, and confirmation dialog.
   * @param {string} tooltipText  — shown on hover (e.g. "删除助手")
   * @param {string} confirmMsg  — shown in the confirm dialog
   * @param {function} onConfirm — called only if user confirms
   */
  function makeDeleteButton(tooltipText, confirmMsg, onConfirm) {
    const btn = el("button", {
      title: tooltipText,
      style: {
        fontSize: "14px",
        padding: "2px 6px",
        border: "1px solid transparent",
        borderRadius: "5px",
        background: "transparent",
        color: "var(--text-secondary)",
        cursor: "pointer",
        lineHeight: "1",
        transition: "all 0.15s ease",
        marginLeft: "4px",
        flexShrink: "0",
      },
    }, "🗑");
    btn.addEventListener("mouseenter", () => {
      btn.style.color = "#ff4d4f";
      btn.style.background = "rgba(255,77,79,0.15)";
      btn.style.border = "1px solid rgba(255,77,79,0.4)";
    });
    btn.addEventListener("mouseleave", () => {
      btn.style.color = "var(--text-secondary)";
      btn.style.background = "transparent";
      btn.style.border = "1px solid transparent";
    });
    btn.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      if (!confirm(confirmMsg)) return;
      try { await onConfirm(); } catch (e) { alert(String(e)); }
    });
    return btn;
  }

  function buildLeftTabBar() {
    const bar = el("div", {
      style: { display: "flex", gap: "0", marginBottom: "8px", borderBottom: "1px solid var(--border)" },
    });
    const mk = (id, label) => {
      const b = el("button", {
        style: {
          flex: "1", fontSize: "12px", fontWeight: "600", padding: "6px 0",
          border: "none", borderBottom: _leftTab === id ? "2px solid var(--accent)" : "2px solid transparent",
          background: "transparent", color: _leftTab === id ? "var(--accent)" : "var(--text-secondary)",
          cursor: "pointer",
        },
      }, label);
      b.addEventListener("click", () => { _leftTab = id; renderPane(_rootEl); });
      return b;
    };
    bar.appendChild(mk("assistants", "助手"));
    bar.appendChild(mk("topics", "主题"));
    return bar;
  }

  let _rootEl = null;

  async function renderPane(rootEl) {
    _rootEl = rootEl;
    rootEl.innerHTML = "";
    renderHeader(rootEl);

    const layout = el("div", {
      style: { display: "grid", gridTemplateColumns: "220px 1fr", gap: "12px", marginTop: "16px", minHeight: "400px" },
    });

    // === Left pane ===
    const left = el("div", {
      style: { background: "var(--panel-bg)", borderRadius: "8px", padding: "8px", border: "1px solid var(--border)" },
    });
    left.appendChild(buildLeftTabBar());

    if (_leftTab === "assistants") {
      left.appendChild(await buildAssistantsPane());
    } else {
      left.appendChild(await buildTopicsPane());
    }
    layout.appendChild(left);

    // === Right pane ===
    const right = el("div", {
      style: { background: "var(--panel-bg)", borderRadius: "8px", padding: "12px", border: "1px solid var(--border)" },
    });
    const headBar = el("div", { style: { display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "12px" } });
    const title = el("span", { style: { fontSize: "13px", fontWeight: "600", color: "var(--text-primary)" } },
      `当前对话 · ${_selectedAssistant}${_selectedTopic ? " · 话题" : ""}`);
    headBar.appendChild(title);
    const actions = el("div", { style: { display: "flex", gap: "8px" } });
    actions.appendChild(softBtn("刷新", async () => renderPane(rootEl)));
    actions.appendChild(softBtn("清空", async () => {
      await clearHistory(_selectedAssistant, _selectedTopic);
      renderPane(rootEl);
    }, { danger: true }));
    headBar.appendChild(actions);
    right.appendChild(headBar);

    const body = el("div", { "data-history": "messages", style: { maxHeight: "500px", overflowY: "auto" } });
    body.textContent = "加载中...";
    right.appendChild(body);
    layout.appendChild(right);
    rootEl.appendChild(layout);

    // Load history
    const hist = await getHistory(_selectedAssistant, _selectedTopic);
    paintHistory(body, hist, _selectedAssistant);
  }

  async function buildAssistantsPane() {
    const wrap = el("div", { style: { display: "flex", flexDirection: "column", gap: "2px" } });
    const list = await listAssistants();
    for (const a of list) {
      const isSel = a.id === _selectedAssistant;
      const row = el("div", {
        style: {
          display: "flex", alignItems: "center", gap: "6px",
          padding: "6px 8px", borderRadius: "6px", cursor: "pointer",
          background: isSel ? "color-mix(in srgb, var(--accent) 14%, transparent)" : "transparent",
          border: isSel ? "1px solid var(--accent)" : "1px solid transparent",
        },
        onclick: () => { _selectedAssistant = a.id; _selectedTopic = null; _leftTab = "topics"; renderPane(_rootEl); },
      }, [
        el("span", { style: { fontSize: "14px" } }, a.active ? "🌟" : "🐾"),
        el("span", { style: { flex: "1", fontSize: "12px", fontWeight: "600", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" } },
          a.themeName || a.id),
        el("span", { style: { fontSize: "10px", color: "var(--text-secondary)" } }, String(a.topicCount || 0)),
      ]);
      if (a.id !== "default") {
        const del = makeDeleteButton(
          "删除助手",
          `确定要删除助手「${a.themeName || a.id}」吗？\n该助手下的所有对话都会丢失，此操作不可恢复。`,
          async () => {
            const r = await deleteAssistant(a.id);
            if (r && r.error) { alert(r.error); return; }
            if (_selectedAssistant === a.id) _selectedAssistant = "default";
            renderPane(_rootEl);
          },
        );
        row.appendChild(del);
      }
      wrap.appendChild(row);
    }
    // + 新建助手
    const addBtn = softBtn("+ 新建助手", () => openCreateAssistantModal(), { accent: true, style: { marginTop: "8px", width: "100%" } });
    wrap.appendChild(addBtn);
    return wrap;
  }

  async function buildTopicsPane() {
    const wrap = el("div", { style: { display: "flex", flexDirection: "column", gap: "2px" } });
    const topics = await listTopics(_selectedAssistant);
    if (topics.length === 0) {
      wrap.appendChild(el("div", { style: { fontSize: "11px", color: "var(--text-secondary)", fontStyle: "italic", padding: "8px" } },
        "该助手还没有话题"));
    }
    for (const tp of topics) {
      const isSel = _selectedTopic ? (tp.id === _selectedTopic) : tp.active;
      const row = el("div", {
        style: {
          display: "flex", alignItems: "center", gap: "6px",
          padding: "6px 8px", borderRadius: "6px", cursor: "pointer",
          background: isSel ? "color-mix(in srgb, var(--accent) 14%, transparent)" : "transparent",
          border: isSel ? "1px solid var(--accent)" : "1px solid transparent",
        },
        onclick: () => { _selectedTopic = tp.id; renderPane(_rootEl); },
      }, [
        el("span", { style: { fontSize: "10px" } }, tp.active ? "●" : "○"),
        el("span", { style: { flex: "1", fontSize: "12px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" } },
          tp.name || "新对话"),
        el("span", { style: { fontSize: "10px", color: "var(--text-secondary)" } }, String(tp.messageCount || 0)),
      ]);
      // Inline rename on double-click
      row.addEventListener("dblclick", (ev) => {
        ev.stopPropagation();
        const newName = prompt("话题名称", tp.name || "新对话");
        if (newName && newName.trim()) renameTopic(_selectedAssistant, tp.id, newName.trim()).then(() => renderPane(_rootEl));
      });
      const del = makeDeleteButton(
        "删除对话",
        `确定要删除话题「${tp.name || "新对话"}」吗？\n该话题中的所有消息都会丢失，此操作不可恢复。`,
        async () => {
          const r = await deleteTopic(_selectedAssistant, tp.id);
          if (r && r.error) { alert(r.error); return; }
          if (_selectedTopic === tp.id) _selectedTopic = null;
          renderPane(_rootEl);
        },
      );
      row.appendChild(del);
      wrap.appendChild(row);
    }
    // + 新建对话
    const addBtn = softBtn("+ 新建对话", async () => {
      const r = await createTopic(_selectedAssistant);
      if (r && r.ok) { _selectedTopic = r.topicId; renderPane(_rootEl); }
    }, { accent: true, style: { marginTop: "8px", width: "100%" } });
    wrap.appendChild(addBtn);

    // "设为当前对话" button — calls __setActiveTopic which DOES switch
    // the live chat bubble. Explicit so browsing doesn't yank the chat.
    if (_selectedTopic) {
      wrap.appendChild(softBtn("设为当前对话", async () => {
        const r = await setActiveTopic(_selectedAssistant, _selectedTopic);
        if (r && r.ok) renderPane(_rootEl);
        else if (r && r.error) alert(r.error);
      }, { style: { marginTop: "4px", width: "100%" } }));
    }
    return wrap;
  }

  function openCreateAssistantModal() {
    // Must pick an animation theme — no theme = blank body.
    const existing = document.getElementById("create-assistant-overlay");
    if (existing) existing.remove();
    const overlay = el("div", {
      id: "create-assistant-overlay",
      style: { position: "fixed", top: "0", left: "0", right: "0", bottom: "0", background: "rgba(0,0,0,0.5)", zIndex: "10000", display: "flex", alignItems: "center", justifyContent: "center" },
    });
    const modal = el("div", {
      style: { background: "var(--bg, #1e1e2e)", color: "var(--text-primary, #cdd6f4)", borderRadius: "12px", padding: "24px", maxWidth: "400px", width: "90%", border: "1px solid var(--border, #45475a)" },
    });
    modal.appendChild(el("h3", { style: { margin: "0 0 8px 0", fontSize: "16px" } }, "新建助手"));
    modal.appendChild(el("p", { style: { margin: "0 0 12px 0", fontSize: "12px", color: "var(--text-secondary)" } },
      "助手必须绑定一个动画主题作为它的身体。选择一个主题："));
    // Theme selector
    const themes = (core && core.runtime && core.runtime.themeList) || [];
    const select = el("select", { style: { width: "100%", padding: "6px", fontSize: "13px", marginBottom: "12px", border: "1px solid var(--border)", borderRadius: "4px", background: "var(--bg)", color: "var(--text-primary)" } });
    for (const th of themes) {
      if (!th || !th.id) continue;
      const name = (typeof th.name === "string") ? th.name
        : (th.name && typeof th.name === "object" ? (th.name.en || th.name.zh) : th.id);
      select.appendChild(el("option", { value: th.id }, name));
    }
    modal.appendChild(select);
    // Name input
    const nameInput = el("input", {
      type: "text", placeholder: "助手名称（留空则用主题名）",
      style: { width: "100%", padding: "6px", fontSize: "13px", marginBottom: "12px", border: "1px solid var(--border)", borderRadius: "4px", background: "var(--bg)", color: "var(--text-primary)" },
    });
    modal.appendChild(nameInput);
    // Buttons
    const btnRow = el("div", { style: { display: "flex", gap: "8px", justifyContent: "flex-end" } });
    btnRow.appendChild(softBtn("取消", () => overlay.remove()));
    btnRow.appendChild(softBtn("创建", async () => {
      const themeId = select.value;
      if (!themeId) { alert("请选择一个动画主题"); return; }
      const name = nameInput.value.trim() || themeId;
      const r = await createAssistant(name);
      if (r && r.ok) {
        _selectedAssistant = name;
        _selectedTopic = null;
        overlay.remove();
        renderPane(_rootEl);
      } else if (r && r.error) {
        alert(r.error);
      }
    }, { accent: true }));
    modal.appendChild(btnRow);
    overlay.appendChild(modal);
    overlay.addEventListener("click", (ev) => { if (ev.target === overlay) overlay.remove(); });
    document.body.appendChild(overlay);
  }

  function paintHistory(body, messages, assistantName) {
    if (!body) return;
    body.innerHTML = "";
    if (!messages || messages.length === 0) {
      body.appendChild(el("p", { style: { color: "var(--text-secondary)", fontSize: "13px", fontStyle: "italic", textAlign: "center", padding: "24px 0" } },
        "暂无对话。跟桌宠聊天后这里会显示消息。"));
      return;
    }
    for (const msg of messages) {
      const isUser = msg.role === "user";
      const roleColors = { user: "#1677ff", assistant: "#52c41a", system: "#fa8c16", tool: "#722ed1" };
      const roleColor = roleColors[msg.role] || "var(--text-secondary)";
      const bg = isUser ? "var(--bg)" : "var(--panel-bg)";
      const roleLabel = isUser ? "USER" : (msg.role === "assistant" ? (assistantName || "ASSISTANT") : (msg.role || ""));
      const item = el("div", {
        style: { padding: "8px 12px", marginBottom: "4px", borderRadius: "6px", background: bg, border: "1px solid var(--border)" },
      });
      const header = el("div", { style: { display: "flex", justifyContent: "space-between", marginBottom: "4px" } });
      header.appendChild(el("span", { style: { fontSize: "11px", fontWeight: "600", color: roleColor, textTransform: "uppercase" } }, roleLabel));
      header.appendChild(el("span", { style: { fontSize: "10px", color: "var(--text-secondary)" } }, `${(msg.content || "").length} chars`));
      item.appendChild(header);
      item.appendChild(el("div", {
        style: { fontSize: "12px", color: "var(--text-primary)", lineHeight: "1.5", whiteSpace: "pre-wrap", wordBreak: "break-word", maxHeight: "120px", overflowY: "auto" },
      }, String(msg.content || "").slice(0, 2000)));
      body.appendChild(item);
    }
    body.appendChild(el("div", { style: { fontSize: "11px", color: "var(--text-secondary)", textAlign: "center", padding: "8px 0" } },
      `${messages.length} messages`));
  }

  function startAutoRefresh() {
    if (!mounted) return;
    refreshTimer = setInterval(async () => {
      if (!mounted || !_rootEl) return;
      const body = _rootEl.querySelector("[data-history='messages']");
      if (!body) return;
      const hist = await getHistory(_selectedAssistant, _selectedTopic);
      paintHistory(body, hist, _selectedAssistant);
    }, 3000);
  }

  async function render(parent) {
    cleanupTimers();
    mounted = true;
    parent.innerHTML = "";
    try {
      if (core && core.ops && typeof core.ops.fetchThemes === "function") {
        await core.ops.fetchThemes();
      }
    } catch {}
    // Default selection to the currently active theme.
    try {
      const themes = core && core.runtime && core.runtime.themeList;
      const cur = Array.isArray(themes) && themes.find((th) => th && th.active);
      if (cur && cur.id) _selectedAssistant = cur.id;
    } catch {}
    await renderPane(parent);
    startAutoRefresh();
  }

  function cleanupTimers() {
    mounted = false;
    if (refreshTimer) { clearInterval(refreshTimer); refreshTimer = null; }
  }

  function init(coreArg) {
    core = coreArg;
    helpers = core.helpers;
    ops = core.ops;
    core.tabs.context = {
      render: (parent) => {
        render(parent).catch((err) => {
          console.error("[context] render failed:", err);
          if (parent) parent.innerHTML = '<p style="color:var(--text-secondary);padding:16px;">对话上下文加载失败，请重试或重启应用。</p>';
        });
      },
    };
  }

  root.ClawdSettingsTabContext = { init };
})(typeof globalThis !== "undefined" ? globalThis : window);
