"use strict";

// Autonomous-spoken settings tab. Lets the user pick between:
//   - off       (model never auto-speaks)
//   - free      (model decides via [NEXT_CHAT:N] in every reply)
//   - interval  (fixed cadence, ignores model's [NEXT_CHAT:N])

(function initSettingsTabProactive(root) {
  let core = null;
  let helpers = null;
  let mounted = false;
  let _currentMode = "free";
  let _currentInterval = 1800;

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
    }
    return e;
  }

  async function pushPolicy(policy) {
    if (!window.settingsAPI || typeof window.settingsAPI.setProactivePolicy !== "function") return;
    try { await window.settingsAPI.setProactivePolicy(policy); } catch (err) {
      if (helpers && typeof helpers.toast === "function") helpers.toast(String(err && err.message || err), { error: true });
    }
  }

  function buildRow(label, hint, control) {
    const row = el("div", {
      style: {
        display: "flex", alignItems: "center", gap: "12px",
        padding: "12px 14px", background: "var(--panel-bg)",
        border: "1px solid var(--border)", borderRadius: "8px",
        marginBottom: "8px",
      },
    });
    const info = el("div", { style: { flex: "1" } });
    info.appendChild(el("div", { style: { fontSize: "14px", fontWeight: "600", color: "var(--text-primary)" } }, label));
    if (hint) info.appendChild(el("div", { style: { fontSize: "12px", color: "var(--text-secondary)", marginTop: "2px" } }, hint));
    row.appendChild(info);
    if (control) row.appendChild(control);
    return row;
  }

  function buildRadio(current, value, label, onClick) {
    const id = "proactive-radio-" + value;
    const labelEl = el("label", {
      for: id,
      style: { display: "flex", alignItems: "center", gap: "6px", cursor: "pointer", padding: "4px 10px", borderRadius: "4px", border: "1px solid var(--border)", background: current === value ? "var(--accent)" : "var(--bg)", color: current === value ? "#fff" : "var(--text-primary)" },
    });
    const input = el("input", {
      type: "radio", name: "proactive-mode", id, value,
      checked: current === value ? "checked" : undefined,
      style: { display: "none" },
    });
    input.addEventListener("change", () => { if (input.checked) onClick(value); });
    labelEl.appendChild(input);
    labelEl.appendChild(el("span", { style: { fontSize: "13px" } }, label));
    return labelEl;
  }

  function renderHeader(parent) {
    parent.appendChild(el("div", { style: { padding: "0 0 16px 0", borderBottom: "1px solid var(--border)" } },
      el("h2", { style: { margin: "0 0 4px 0", fontSize: "18px", fontWeight: "600" } }, "自主说话"),
      el("p", { style: { margin: "0", fontSize: "13px", color: "var(--text-secondary)" } },
        "控制桌宠在没有用户输入时主动聊天的策略。"),
    ));
  }

  function render(parent) {
    cleanupTimers();
    mounted = true;
    parent.innerHTML = "";
    renderHeader(parent);

    const segWrap = el("div", { style: { display: "flex", gap: "8px", flexWrap: "wrap", marginTop: "16px" } });
    segWrap.appendChild(buildRadio(_currentMode, "free", "自由说话（模型自己决定）", async (v) => {
      _currentMode = v;
      await pushPolicy(v);
      render(parent);
    }));
    segWrap.appendChild(buildRadio(_currentMode, "interval", "定时说话（固定间隔）", async (v) => {
      _currentMode = v;
      render(parent);
    }));
    segWrap.appendChild(buildRadio(_currentMode, "off", "关闭（不主动说话）", async (v) => {
      _currentMode = v;
      await pushPolicy(v);
      render(parent);
    }));
    parent.appendChild(segWrap);

    if (_currentMode === "interval") {
      let inputValue = _currentInterval;
      const cfgCard = el("div", {
        style: { background: "var(--panel-bg)", border: "1px solid var(--border)", borderRadius: "8px", padding: "16px", marginTop: "12px" },
      });
      cfgCard.appendChild(el("div", { style: { fontSize: "14px", fontWeight: "600", marginBottom: "8px" } }, "间隔设置"));
      cfgCard.appendChild(el("p", { style: { fontSize: "12px", color: "var(--text-secondary)", margin: "0 0 12px 0", lineHeight: "1.6" } },
        "定时模式忽略模型在回复末尾写的 [NEXT_CHAT:N]，统一用你设的间隔主动说话。最小60秒，最大86400秒（24小时）。"));
      const inputRow = el("div", { style: { display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap" } });
      inputRow.appendChild(el("span", { style: { fontSize: "13px" } }, "每隔"));
      const input = el("input", {
        type: "number", min: "60", max: "86400", value: String(inputValue),
        style: { width: "100px", padding: "4px 8px", fontSize: "13px", border: "1px solid var(--border)", borderRadius: "4px", background: "var(--bg)", color: "var(--text-primary)" },
      });
      input.addEventListener("input", () => { inputValue = Number(input.value) || 0; });
      inputRow.appendChild(input);
      const unit = el("select", { style: { padding: "4px 8px", fontSize: "13px", border: "1px solid var(--border)", borderRadius: "4px", background: "var(--bg)", color: "var(--text-primary)" } });
      [
        { tag: "s", label: "秒", mul: 1 },
        { tag: "m", label: "分", mul: 60 },
        { tag: "h", label: "小时", mul: 3600 },
      ].forEach((u) => unit.appendChild(el("option", { value: String(u.mul) }, u.label)));
      unit.value = "60";
      unit.addEventListener("change", () => { inputValue = (Number(input.value) || 0) * Number(unit.value); input.value = String(inputValue / Number(unit.value)); });
      inputRow.appendChild(unit);
      const applyBtn = el("button", {
        style: { marginLeft: "auto", padding: "4px 14px", fontSize: "13px", border: "1px solid var(--accent)", borderRadius: "4px", background: "var(--accent)", color: "#fff", cursor: "pointer" },
        onclick: async () => {
          const seconds = Math.max(60, Math.min(86400, Number(inputValue) || 1800));
          _currentInterval = seconds;
          await pushPolicy("interval:" + seconds);
        },
      }, "应用");
      inputRow.appendChild(applyBtn);
      cfgCard.appendChild(inputRow);
      const preview = el("div", { style: { fontSize: "12px", color: "var(--text-secondary)", marginTop: "8px" } });
      const updatePreview = () => {
        const s = Math.max(60, Math.min(86400, Number(inputValue) || 0));
        let label;
        if (s >= 3600) label = `${(s/3600).toFixed(s % 3600 === 0 ? 0 : 1)}小时`;
        else if (s >= 60) label = `${Math.round(s/60)}分钟`;
        else label = `${s}秒`;
        preview.textContent = `当前：每 ${label} 主动说一次（约 ${s} 秒）`;
      };
      input.addEventListener("input", updatePreview);
      unit.addEventListener("change", updatePreview);
      updatePreview();
      cfgCard.appendChild(preview);
      parent.appendChild(cfgCard);
    }

    parent.appendChild(buildRow(
      "当前生效策略",
      _currentMode === "off"
        ? "桌宠从不主动说话"
        : _currentMode === "interval"
          ? `桌宠每 ${_currentInterval} 秒主动说话一次`
          : "桌宠按模型写在回复末尾的 [NEXT_CHAT:N] 标签决定间隔",
      el("span", {
        style: { fontSize: "12px", fontFamily: "monospace", padding: "2px 8px", borderRadius: "10px", background: "color-mix(in srgb, var(--accent) 12%, transparent)", color: "var(--accent)" },
      }, _currentMode === "interval" ? `interval:${_currentInterval}` : _currentMode),
    ));

    parent.appendChild(el("div", {
      style: { marginTop: "16px", padding: "12px", borderRadius: "6px", background: "color-mix(in srgb, var(--accent, #89b4fa) 8%, transparent)", border: "1px solid var(--border)", fontSize: "12px", color: "var(--text-secondary)", lineHeight: "1.6" },
    },
      el("p", { style: { margin: "0 0 6px 0", fontWeight: "600", color: "var(--text-primary)" } }, "提示"),
      el("p", { style: { margin: "0" } }, "自由说话模式下，模型回复末尾的 [NEXT_CHAT:N] 数字就是下一次主动说话的秒数，写 0 表示关闭。用户打字或聊天中不会被打扰。"),
    ));
  }

  function init(coreArg) {
    core = coreArg; helpers = core.helpers;
    core.tabs.proactive = { render: (parent) => { void render(parent); } };
  }

  root.ClawdSettingsTabProactive = { init };
})(typeof globalThis !== "undefined" ? globalThis : window);
