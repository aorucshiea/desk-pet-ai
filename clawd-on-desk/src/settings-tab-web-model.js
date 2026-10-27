"use strict";

// ── Web model settings tab ──
// The DeepSeek web-chat bridge is an optional, experimental model source.
// This page owns the three things a user must be able to see/do:
//   * is the local bridge + browser extension alive?
//   * how many web messages were used today?
//   * make the web model the active model (or switch back).
// The heavy lifting (bridge process, health, test prompt) lives in
// settings-ipc.js; the provider config is written through the same
// settings-controller path as the Providers page.

(function initSettingsTabWebModel(root) {
  let core = null;
  let helpers = null;
  let ops = null;
  let timer = null;
  let mounted = false;

  const WEB_PROVIDER = {
    provider: "deepseek-web",
    apiKey: "local",
    baseUrl: "http://127.0.0.1:8766/v1",
    model: "deepseek-web",
    thinking: false,
    reasoningEffort: null,
    contextWindow: 65536,
    functionCalling: false,
    toolsEnabled: true,
    images: true,
  };

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    if (attrs && typeof attrs === "object" && !Array.isArray(attrs)) {
      for (const [key, value] of Object.entries(attrs)) {
        if (key === "className") node.className = value;
        else if (key === "style" && typeof value === "object") Object.assign(node.style, value);
        else if (key.startsWith("on")) node.addEventListener(key.slice(2).toLowerCase(), value);
        else if (value !== undefined && value !== null) node.setAttribute(key, value);
      }
    }
    for (const child of children) {
      if (child == null) continue;
      if (typeof child === "string" || typeof child === "number") {
        node.appendChild(document.createTextNode(String(child)));
      } else if (child instanceof Node) {
        node.appendChild(child);
      }
    }
    return node;
  }

  function softBtn(label, onClick, opts = {}) {
    const button = el("button", {
      type: "button",
      className: "soft-btn" + (opts.accent ? " accent" : ""),
      onClick,
    }, label);
    if (opts.id) button.id = opts.id;
    return button;
  }

  function toast(text, error) {
    if (ops && typeof ops.showToast === "function") {
      ops.showToast(text, { error: !!error });
    }
  }

  function snapshotSkills() {
    const snapshot = (core && core.state && core.state.snapshot) || {};
    return snapshot.skills || {};
  }

  function providerList() {
    const skills = snapshotSkills();
    return Array.isArray(skills.modelProviders) ? skills.modelProviders.slice() : [];
  }

  function currentProvider() {
    return snapshotSkills().defaultProvider || "local";
  }

  function hasWebProvider() {
    return providerList().some((p) => p && p.provider === "deepseek-web");
  }

  function statusCard() {
    const card = el("div", {
      style: {
        background: "var(--bg-secondary, rgba(255,255,255,0.04))",
        border: "1px solid var(--border, rgba(255,255,255,0.12))",
        borderRadius: "10px",
        padding: "16px 18px",
        marginBottom: "16px",
      },
    });
    const rows = {};
    const makeRow = (label, key) => {
      const row = el("div", {
        style: {
          display: "flex",
          justifyContent: "space-between",
          gap: "16px",
          padding: "7px 0",
          borderBottom: "1px solid var(--border, rgba(255,255,255,0.06))",
          fontSize: "13px",
        },
      });
      row.appendChild(el("span", { style: { color: "var(--text-secondary, #9aa4b2)" } }, label));
      const value = el("span", { style: { textAlign: "right", fontVariantNumeric: "tabular-nums" } }, "—");
      row.appendChild(value);
      card.appendChild(row);
      rows[key] = value;
    };
    makeRow("Bridge", "bridge");
    makeRow("浏览器扩展", "extension");
    makeRow("今日网页消息", "quota");
    makeRow("网页会话", "session");
    makeRow("当前模型", "current");
    makeRow("Provider 配置", "provider");
    return { card, rows };
  }

  async function refresh() {
    if (!mounted) return;
    let status = { status: "offline", bridge: null, paths: {} };
    try {
      if (window.petSettings && typeof window.petSettings.webModelStatus === "function") {
        status = await window.petSettings.webModelStatus();
      }
    } catch {}
    const bridge = status.bridge || {};
    const online = status.status === "ok";
    const connected = !!bridge.extension_connected;
    const current = currentProvider();
    const configured = hasWebProvider();

    if (rows.bridge) {
      rows.bridge.textContent = online ? "在线 · 127.0.0.1:8766" : "离线";
      rows.bridge.style.color = online ? "var(--success, #4ade80)" : "var(--danger, #f87171)";
    }
    if (rows.extension) {
      rows.extension.textContent = connected
        ? "已连接"
        : online
          ? "未连接（检查 DeepSeek 标签页/扩展）"
          : "—";
      rows.extension.style.color = connected ? "var(--success, #4ade80)" : "var(--text-secondary, #9aa4b2)";
    }
    if (rows.quota) {
      const used = Number(bridge.messages_today || 0);
      const limit = Number(bridge.daily_limit || 0);
      rows.quota.textContent = online
        ? (limit > 0 ? `${used} / ${limit} 条` : `${used} 条（未设上限）`)
        : "—";
      if (online && limit > 0 && used >= limit) {
        rows.quota.style.color = "var(--danger, #f87171)";
      }
    }
    if (rows.session) {
      rows.session.textContent = online
        ? (bridge.session_active ? `复用中 · ${Number(bridge.session_turns || 0)} 轮` : "新会话")
        : "—";
    }
    if (rows.current) {
      rows.current.textContent = current === "deepseek-web" ? "网页模型（已启用）" : current;
      rows.current.style.color = current === "deepseek-web" ? "var(--success, #4ade80)" : undefined;
    }
    if (rows.provider) {
      rows.provider.textContent = configured ? "已配置" : "未配置";
    }

    if (enableSwitch) {
      const on = current === "deepseek-web";
      enableSwitch.classList.toggle("on", on);
      enableSwitch.setAttribute("aria-checked", on ? "true" : "false");
    }
    if (startButton) startButton.disabled = online;
    const remaining = bridge.remaining;
    const exhausted = online && typeof remaining === "number" && remaining <= 0;
    if (testButton) testButton.disabled = !online || !connected || exhausted;
    if (selectButton) selectButton.disabled = current === "deepseek-web" || !online || !connected || exhausted;
    if (limitInput) {
      limitInput.disabled = !online;
      if (online && document.activeElement !== limitInput) {
        limitInput.value = String(Number(bridge.daily_limit ?? 100));
      }
    }
    if (limitButton) limitButton.disabled = !online;
    if (copyButton && status.paths && status.paths.extension) {
      copyButton.dataset.path = status.paths.extension;
    }
  }

  async function ensureWebProvider() {
    const skills = snapshotSkills();
    const list = providerList();
    const index = list.findIndex((p) => p && p.provider === "deepseek-web");
    const next = list.slice();
    if (index < 0) next.push({ ...WEB_PROVIDER });
    else next[index] = { ...next[index], ...WEB_PROVIDER };
    const result = await window.settingsAPI.update("skills", {
      ...skills,
      modelProviders: next,
      defaultProvider: "deepseek-web",
    });
    if (result && result.status === "error") throw new Error(result.message || "设置保存失败");
    if (window.petSettings && typeof window.petSettings.saveProvidersConfig === "function") {
      await window.petSettings.saveProvidersConfig(next);
    }
  }

  async function disableWebModel() {
    const skills = snapshotSkills();
    const list = providerList();
    const fallback = list.find((p) => p && p.provider !== "deepseek-web" && p.provider === "deepseek")
      || list.find((p) => p && p.provider !== "deepseek-web")
      || null;
    const result = await window.settingsAPI.update("skills", {
      ...skills,
      defaultProvider: fallback ? fallback.provider : "local",
    });
    if (result && result.status === "error") throw new Error(result.message || "设置保存失败");
    toast(fallback ? `已切回 ${fallback.provider}` : "已切回内置模型");
  }

  async function setEnabled(next) {
    try {
      if (next) {
        let status = await window.petSettings.webModelStatus();
        if (status.status !== "ok") {
          status = await window.petSettings.webModelStart();
          if (status.status !== "ok") throw new Error(status.message || "Bridge 启动失败");
        }
        if (!(status.bridge && status.bridge.extension_connected)) {
          throw new Error("Bridge 已启动，但浏览器扩展还没连接：请打开 https://chat.deepseek.com 并确认扩展已加载");
        }
        if (typeof status.bridge.remaining === "number" && status.bridge.remaining <= 0) {
          throw new Error("今日网页消息已达上限，请明天再试，或在设置里切回 API/本地模型");
        }
        await ensureWebProvider();
        toast("网页模型已设为当前模型");
      } else {
        await disableWebModel();
      }
    } catch (err) {
      toast(String((err && err.message) || err), true);
    }
    await refresh();
  }

  async function test() {
    if (testButton) testButton.disabled = true;
    if (testResult) testResult.textContent = "测试中…（会消耗 1 条网页消息）";
    try {
      const result = await window.petSettings.webModelTest();
      if (result.status !== "ok") throw new Error(result.message || "测试失败");
      if (testResult) {
        testResult.textContent = `测试成功 · ${(result.elapsed_ms / 1000).toFixed(1)}s · ${result.reply || "（空回复）"}`;
      }
    } catch (err) {
      if (testResult) testResult.textContent = `测试失败：${String((err && err.message) || err)}`;
    } finally {
      if (testButton) testButton.disabled = false;
    }
  }

  let rows = {};
  let enableSwitch = null;
  let startButton = null;
  let testButton = null;
  let selectButton = null;
  let copyButton = null;
  let testResult = null;
  let limitInput = null;
  let limitButton = null;

  function render(parent) {
    mounted = true;
    if (timer) { clearInterval(timer); timer = null; }

    const wrap = el("div", { className: "pet-page-header" });
    const textCol = el("div", { className: "pet-page-header-text" });
    textCol.appendChild(el("h1", {}, "网页模型"));
    textCol.appendChild(el("p", { className: "subtitle" }, "用已登录的 DeepSeek 网页版给桌宠当模型；免费额度，浏览器自动化。"));
    wrap.appendChild(textCol);
    parent.appendChild(wrap);

    parent.appendChild(el("div", {
      style: {
        padding: "12px 14px",
        marginBottom: "16px",
        borderRadius: "8px",
        border: "1px solid rgba(245,158,11,0.45)",
        background: "rgba(245,158,11,0.08)",
        color: "var(--text-secondary, #c9d1d9)",
        fontSize: "12px",
        lineHeight: "1.6",
      },
    }, "实验功能：这不是 DeepSeek 官方 API。网页改版/限流会失效，每轮消耗一条网页消息，也可能违反平台服务条款；只建议低频试用。"));

    const status = statusCard();
    rows = status.rows;
    parent.appendChild(status.card);

    const switchRow = el("div", {
      style: {
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        gap: "16px",
        padding: "14px 16px",
        marginBottom: "16px",
        borderRadius: "10px",
        border: "1px solid var(--border, rgba(255,255,255,0.12))",
      },
    });
    const switchText = el("div", {});
    switchText.appendChild(el("div", { style: { fontWeight: "600", marginBottom: "4px" } }, "启用网页模型"));
    switchText.appendChild(el("div", {
      style: { fontSize: "12px", color: "var(--text-secondary, #9aa4b2)", lineHeight: "1.5" },
    }, "开启后，桌宠的对话和工具调用都交给网页版 DeepSeek；关闭则切回 API 或本地模型。"));
    switchRow.appendChild(switchText);
    enableSwitch = el("div", {
      className: "switch",
      role: "switch",
      tabindex: "0",
      "aria-checked": "false",
    });
    const toggle = () => { void setEnabled(!enableSwitch.classList.contains("on")); };
    enableSwitch.addEventListener("click", toggle);
    enableSwitch.addEventListener("keydown", (ev) => {
      if (ev.key === " " || ev.key === "Enter") { ev.preventDefault(); toggle(); }
    });
    switchRow.appendChild(enableSwitch);
    parent.appendChild(switchRow);

    const actions = el("div", { style: { display: "flex", flexWrap: "wrap", gap: "8px", marginBottom: "12px" } });
    startButton = softBtn("启动 Bridge", async () => {
      const result = await window.petSettings.webModelStart();
      if (result.status !== "ok") toast(result.message || "启动失败", true);
      else toast("Bridge 已启动");
      await refresh();
    });
    testButton = softBtn("测试对话", () => { void test(); });
    selectButton = softBtn("设为当前模型", async () => {
      await setEnabled(true);
    }, { accent: true });
    copyButton = softBtn("复制扩展路径", async () => {
      const path = copyButton.dataset.path || "";
      try {
        await navigator.clipboard.writeText(path);
        toast("扩展路径已复制");
      } catch {
        toast(path || "路径不可用", true);
      }
    });
    actions.appendChild(startButton);
    actions.appendChild(testButton);
    actions.appendChild(selectButton);
    actions.appendChild(copyButton);
    actions.appendChild(softBtn("打开 DeepSeek", async () => {
      await window.petSettings.webModelOpen();
    }));
    actions.appendChild(softBtn("打开 Bridge 目录", async () => {
      await window.petSettings.webModelReveal();
    }));
    parent.appendChild(actions);

    testResult = el("div", {
      style: { fontSize: "12px", color: "var(--text-secondary, #9aa4b2)", minHeight: "18px", marginBottom: "16px" },
    }, "");
    parent.appendChild(testResult);

    const limitRow = el("div", {
      style: {
        display: "flex",
        alignItems: "center",
        gap: "10px",
        flexWrap: "wrap",
        marginBottom: "16px",
        fontSize: "13px",
      },
    });
    limitRow.appendChild(el("span", { style: { color: "var(--text-secondary, #9aa4b2)" } }, "每日上限"));
    limitInput = el("input", {
      type: "number",
      min: "0",
      max: "10000",
      step: "1",
      style: {
        width: "86px",
        padding: "5px 8px",
        borderRadius: "6px",
        border: "1px solid var(--border, rgba(255,255,255,0.18))",
        background: "var(--bg-secondary, rgba(255,255,255,0.05))",
        color: "var(--text-primary, #fff)",
        fontSize: "13px",
      },
    });
    limitRow.appendChild(limitInput);
    limitRow.appendChild(el("span", {
      style: { color: "var(--text-tertiary, #7d8590)", fontSize: "12px" },
    }, "条 / 天（0 = 不限）"));
    limitButton = softBtn("保存上限", async () => {
      const raw = limitInput.value.trim();
      if (!raw) {
        toast("请输入每日上限（0 = 不限）", true);
        return;
      }
      const value = Number(raw);
      if (!Number.isInteger(value) || value < 0 || value > 10_000) {
        toast("每日上限必须是 0–10000 的整数（0 = 不限）", true);
        return;
      }
      const result = await window.petSettings.webModelSetLimit(value);
      if (result.status !== "ok") {
        toast(result.message || "保存失败", true);
        return;
      }
      toast(value === 0 ? "每日上限已设为不限" : `每日上限已设为 ${value} 条`);
      await refresh();
    });
    limitRow.appendChild(limitButton);
    parent.appendChild(limitRow);

    const steps = el("div", {
      style: {
        borderRadius: "10px",
        border: "1px solid var(--border, rgba(255,255,255,0.12))",
        padding: "16px 18px",
        fontSize: "13px",
        lineHeight: "1.8",
        color: "var(--text-secondary, #c9d1d9)",
      },
    });
    steps.appendChild(el("div", { style: { fontWeight: "600", color: "var(--text-primary, #fff)", marginBottom: "8px" } }, "首次使用"));
    steps.appendChild(el("div", {}, "1. 点上面的「启动 Bridge」，或在 pet-sidecar/deepseek_web 里运行 start_bridge.bat。"));
    steps.appendChild(el("div", {}, "2. 在 Chrome/Edge 打开 chrome://extensions → 开发者模式 → 加载解压缩扩展 → 选择 deepseek_web/extension。"));
    steps.appendChild(el("div", {}, "3. 打开 https://chat.deepseek.com 并登录，保持这个标签页（建议独立窗口，别最小化整个浏览器）。"));
    steps.appendChild(el("div", {}, "4. 回到这里点「设为当前模型」；「测试对话」会真的发一条网页消息，用来确认链路。"));
    steps.appendChild(el("div", {}, "每日上限可在上方直接改（按 /v1/chat/completions 请求计，0 = 不限）；没有配置时默认 100，可用 PET_WEB_DAILY_LIMIT 覆盖。"));
    steps.appendChild(el("div", {}, "看屏幕：先开启「屏幕点击 → 总是允许查看屏幕」。网页模型会把截图作为附件上传到 DeepSeek 网页；上传入口失效会报错，可改用 observe_screen 或切 API/视觉 provider。"));
    steps.appendChild(el("div", {
      style: { marginTop: "8px", fontSize: "12px", color: "var(--text-tertiary, #7d8590)" },
    }, "扩展只读取当前 DeepSeek 标签页的输入/输出，不读取 cookie；Bridge 只监听 127.0.0.1:8766。"));
    parent.appendChild(steps);

    // Keep the primary controls above the fold: the status card is useful,
    // but "set as current model" must not sit below a scroll boundary.
    parent.insertBefore(switchRow, status.card);
    parent.insertBefore(actions, status.card);
    parent.insertBefore(testResult, status.card);
    parent.insertBefore(limitRow, status.card);

    void refresh();
    timer = setInterval(() => { void refresh(); }, 3000);
  }

  function onExit() {
    mounted = false;
    if (timer) { clearInterval(timer); timer = null; }
  }

  function init(coreArg) {
    core = coreArg;
    helpers = core.helpers;
    ops = core.ops;
    core.tabs.webModel = { render: (parent) => { void render(parent); }, onExit };
  }

  root.ClawdSettingsTabWebModel = { init };
})(typeof globalThis !== "undefined" ? globalThis : window);
