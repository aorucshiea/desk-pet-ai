"use strict";

// Renderer for the "大脑流" window. The gateway pushes structured events
// over SSE; this file only renders them (never executes model text) and
// reconnects with a small backoff when the stream is unavailable.

(() => {
  const BACKOFF_MS = [1000, 3000, 5000];
  const MAX_EVENTS_IN_DOM = 1500;
  const MAX_SEEN_IDS = 5000;
  const LONG_TEXT_CHARS = 500;
  const NEAR_BOTTOM_PX = 48;
  const SVG_NS = "http://www.w3.org/2000/svg";

  const TYPE_META = {
    round_start: { label: "开始" },
    provider: { label: "模型" },
    summary: { label: "总结" },
    d_decision: { label: "D" },
    prompt: { label: "提示" },
    first_token: { label: "首字" },
    think: { label: "思考" },
    inner: { label: "心声" },
    tool_call: { label: "工具" },
    tool_result: { label: "结果" },
    slow: { label: "慢直觉" },
    dstate: { label: "状态" },
    speak: { label: "说" },
    quiet: { label: "安静" },
    error: { label: "错误" },
    round_end: { label: "结束" },
  };

  // Which toolbar chip a card belongs to. Types without a group only show
  // under "全部" (e.g. provider / prompt).
  const TYPE_GROUP = {
    d_decision: "d",
    dstate: "d",
    think: "think",
    inner: "inner",
    slow: "inner",
    tool_call: "tool",
    tool_result: "tool",
    summary: "summary",
    speak: "speak",
    first_token: "speak",
    quiet: "quiet",
    error: "error",
  };

  const feed = document.getElementById("feed");
  const emptyState = document.getElementById("emptyState");
  const jumpBtn = document.getElementById("jumpBtn");
  const eventCountEl = document.getElementById("eventCount");
  const statusDot = document.getElementById("statusDot");
  const statusText = document.getElementById("statusText");
  const filtersEl = document.getElementById("filters");
  const pauseBtn = document.getElementById("pauseBtn");
  const clearBtn = document.getElementById("clearBtn");
  const autoscrollBtn = document.getElementById("autoscrollBtn");
  const pausedBadge = document.getElementById("pausedBadge");

  /** @type {Map<string, any>} */
  const rounds = new Map();
  /** @type {Array<{el: HTMLElement, round: any}>} */
  let rendered = [];
  const seenIds = new Set();
  const seenOrder = [];
  let eventCount = 0;
  let currentFilter = "all";
  let paused = false;
  let pendingEvents = [];
  let autoscroll = true;
  let retryTimer = null;
  let retryAttempt = 0;
  let generation = 0;
  let activeController = null;
  let disposed = false;

  // ── small DOM helpers ────────────────────────────────────────────────

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function pad2(n) {
    return String(n).padStart(2, "0");
  }

  function formatTime(value) {
    const date = value ? new Date(value) : new Date();
    if (Number.isNaN(date.getTime())) return "--:--:--";
    return `${pad2(date.getHours())}:${pad2(date.getMinutes())}:${pad2(date.getSeconds())}`;
  }

  function formatElapsed(ms) {
    const value = Number(ms);
    if (!Number.isFinite(value)) return "";
    if (value >= 1000) return `${(value / 1000).toFixed(1)}s`;
    return `${Math.round(value)}ms`;
  }

  function formatPercent(value) {
    const num = Number(value);
    if (!Number.isFinite(num)) return String(value);
    return num <= 1 ? `${Math.round(num * 100)}%` : `${Math.round(num)}%`;
  }

  function primitive(value) {
    if (typeof value === "string") return value;
    if (typeof value === "number" && Number.isFinite(value)) return String(value);
    if (typeof value === "boolean") return value ? "是" : "否";
    return "";
  }

  function hasNumber(value) {
    return value !== null && value !== undefined && value !== ""
      && Number.isFinite(Number(value));
  }

  function charsLabel(value) {
    return hasNumber(value) ? `${value} 字` : "";
  }

  function setStatus(kind, text) {
    statusDot.className = `status-dot ${kind}`;
    statusText.textContent = text;
  }

  function updateEventCount() {
    eventCountEl.textContent = `${eventCount} 条`;
  }

  function isNearBottom() {
    return feed.scrollHeight - feed.scrollTop - feed.clientHeight < NEAR_BOTTOM_PX;
  }

  function scrollToBottom() {
    feed.scrollTop = feed.scrollHeight;
  }

  function maybeScroll() {
    if (autoscroll && isNearBottom()) scrollToBottom();
  }

  function setAutoscroll(value, options = {}) {
    autoscroll = !!value;
    autoscrollBtn.classList.toggle("active", autoscroll);
    autoscrollBtn.setAttribute("aria-pressed", String(autoscroll));
    if (autoscroll) {
      jumpBtn.hidden = true;
      scrollToBottom();
    } else if (!options.silent) {
      jumpBtn.hidden = isNearBottom();
    }
  }

  // ── filters ──────────────────────────────────────────────────────────

  function cardMatchesFilter(card) {
    if (currentFilter === "all") return true;
    return card.dataset.group === currentFilter;
  }

  function updateRoundVisibility(round) {
    if (currentFilter === "all") {
      round.section.classList.remove("filtered-out");
    } else {
      let anyVisible = false;
      for (const child of round.body.children) {
        if (!child.classList.contains("filtered-out")) {
          anyVisible = true;
          break;
        }
      }
      round.section.classList.toggle("filtered-out", !anyVisible);
    }
    updateEmptyState();
  }

  function applyFilters() {
    for (const item of rendered) {
      item.el.classList.toggle("filtered-out", !cardMatchesFilter(item.el));
    }
    for (const round of rounds.values()) updateRoundVisibility(round);
    updateEmptyState();
  }

  function updateEmptyState() {
    let hasVisibleRound = false;
    for (const round of rounds.values()) {
      if (!round.section.classList.contains("filtered-out")) {
        hasVisibleRound = true;
        break;
      }
    }
    emptyState.hidden = hasVisibleRound;
    emptyState.textContent = currentFilter === "all" ? "等待网关事件…" : "当前筛选下没有事件";
  }

  // ── event normalization / dedup ──────────────────────────────────────

  function normalizeEvent(raw) {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
    const type = typeof raw.type === "string" && raw.type ? raw.type : "unknown";
    const idNumber = Number(raw.id);
    return {
      id: raw.id !== null && raw.id !== undefined && Number.isFinite(idNumber) ? idNumber : null,
      ts: typeof raw.ts === "string" ? raw.ts : new Date().toISOString(),
      round: typeof raw.round === "string" ? raw.round : "",
      iteration: Number.isFinite(Number(raw.iteration)) ? Number(raw.iteration) : 0,
      type,
      title: typeof raw.title === "string" ? raw.title : "",
      text: typeof raw.text === "string" ? raw.text : "",
      meta: raw.meta && typeof raw.meta === "object" && !Array.isArray(raw.meta) ? raw.meta : {},
    };
  }

  function acceptEvent(raw) {
    const event = normalizeEvent(raw);
    if (!event) return;
    if (event.id !== null) {
      const key = String(event.id);
      if (seenIds.has(key)) return;
      seenIds.add(key);
      seenOrder.push(key);
      while (seenOrder.length > MAX_SEEN_IDS) {
        const oldest = seenOrder.shift();
        if (oldest !== undefined) seenIds.delete(oldest);
      }
    }
    eventCount += 1;
    updateEventCount();

    if (paused) {
      pendingEvents.push(event);
      updatePausedBadge();
      return;
    }
    renderEvent(event);
    if (!autoscroll && !isNearBottom()) jumpBtn.hidden = false;
    maybeScroll();
  }

  // ── rounds ───────────────────────────────────────────────────────────

  function roundKeyFor(event) {
    return event.round ? `round:${event.round}` : "round:__default__";
  }

  function ensureRound(event) {
    const key = roundKeyFor(event);
    const existing = rounds.get(key);
    if (existing) return existing;

    const section = element("section", "round");
    const header = element("div", "round-header");
    const dot = element("span", "round-dot unknown");
    const title = element("span", "round-title", `轮 · ${formatTime(event.ts)} · 用户`);
    const meta = element("span", "round-meta");
    header.append(dot, title, meta);

    const body = element("div", "round-body");
    section.append(header, body);
    feed.appendChild(section);

    const round = {
      key,
      section,
      dot,
      titleEl: title,
      metaEl: meta,
      body,
      ts: event.ts,
      proactive: typeof event.meta.proactive === "boolean" ? event.meta.proactive : null,
      toolCalls: 0,
      ended: false,
    };
    rounds.set(key, round);
    return round;
  }

  function refreshRoundHeader(round) {
    const mode = round.proactive === true ? "主动" : "用户";
    round.titleEl.textContent = `轮 · ${formatTime(round.ts)} · ${mode}`;
    round.dot.className = "round-dot";
    if (round.proactive === true) round.dot.classList.add("proactive");
    else if (round.proactive === false) round.dot.classList.add("user");
    else round.dot.classList.add("unknown");
  }

  function handleRoundStart(event) {
    const round = ensureRound(event);
    round.ts = event.ts;
    if (typeof event.meta.proactive === "boolean") {
      round.proactive = event.meta.proactive;
    } else if (typeof event.meta.user_text === "string" && event.meta.user_text) {
      round.proactive = false;
    }
    refreshRoundHeader(round);
    updateRoundVisibility(round);
  }

  function handleRoundEnd(event) {
    const round = ensureRound(event);
    round.ended = true;
    round.section.classList.add("ended");

    let toolCount = round.toolCalls;
    const tools = event.meta.tools;
    if (Array.isArray(tools)) toolCount = tools.length;
    else if (Number.isFinite(Number(tools)) && tools !== null && tools !== "") {
      toolCount = Math.max(0, Math.floor(Number(tools)));
    }

    const parts = [];
    const elapsed = formatElapsed(event.meta.elapsed_ms);
    if (elapsed) parts.push(elapsed);
    if (toolCount > 0) parts.push(`工具 ${toolCount}`);
    round.metaEl.textContent = parts.join(" · ");
    updateRoundVisibility(round);
  }

  // ── cards ────────────────────────────────────────────────────────────

  function createLockIcon() {
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "1.8");
    svg.setAttribute("stroke-linecap", "round");
    svg.setAttribute("stroke-linejoin", "round");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("class", "lock-icon");

    const rect = document.createElementNS(SVG_NS, "rect");
    rect.setAttribute("x", "5");
    rect.setAttribute("y", "11");
    rect.setAttribute("width", "14");
    rect.setAttribute("height", "10");
    rect.setAttribute("rx", "2");
    const shackle = document.createElementNS(SVG_NS, "path");
    shackle.setAttribute("d", "M8 11V8a4 4 0 0 1 8 0v3");
    svg.append(rect, shackle);
    return svg;
  }

  function isToolType(type) {
    return type === "tool_call" || type === "tool_result";
  }

  function addTextBlock(card, event) {
    if (!event.text) return;
    const wrapper = element("div");
    if (event.text.length > LONG_TEXT_CHARS) {
      const details = element("details", "event-details");
      const summary = element("summary", "", `展开全文 · ${event.text.length} 字`);
      const full = element("div", "event-text", event.text);
      if (isToolType(event.type)) full.classList.add("mono");
      details.append(summary, full);
      wrapper.appendChild(details);
    } else {
      wrapper.className = "event-text";
      if (isToolType(event.type)) wrapper.classList.add("mono");
      wrapper.textContent = event.text;
    }
    card.appendChild(wrapper);
  }

  function addMetaChip(row, label, value) {
    if (value === null || value === undefined || value === "") return;
    const text = primitive(value);
    if (!text) return;
    row.appendChild(element("span", "meta-chip", label ? `${label} ${text}` : text));
  }

  function addMetaChips(card, event) {
    const meta = event.meta;
    const row = element("div", "event-meta");

    if (event.type === "d_decision") {
      addMetaChip(row, "强度", meta.strength);
      addMetaChip(row, "反应", meta.reaction);
      addMetaChip(row, "动作", meta.action_label || meta.action);
      addMetaChip(row, "上限", meta.max_tokens);
      addMetaChip(row, "来源", meta.source);
      if (hasNumber(meta.latency_ms)) {
        addMetaChip(row, "延迟", formatElapsed(meta.latency_ms));
      }
      if (hasNumber(meta.confidence)) {
        addMetaChip(row, "置信", formatPercent(meta.confidence));
      }
    } else if (event.type === "provider") {
      addMetaChip(row, "提供方", meta.provider);
      addMetaChip(row, "模型", meta.model);
      if (typeof meta.is_local === "boolean") addMetaChip(row, "", meta.is_local ? "本地" : "云端");
      if (typeof meta.allow_tools === "boolean") addMetaChip(row, "", meta.allow_tools ? "工具开" : "工具关");
    } else if (event.type === "summary") {
      addMetaChip(row, "摘要", charsLabel(meta.summary_chars));
      addMetaChip(row, "tokens", meta.tokens);
    } else if (event.type === "tool_call" || event.type === "tool_result") {
      addMetaChip(row, "", meta.tool);
      addMetaChip(row, "", meta.server);
      if (hasNumber(meta.elapsed_ms)) {
        addMetaChip(row, "", formatElapsed(meta.elapsed_ms));
      }
      if (meta.is_error === true) addMetaChip(row, "", "错误");
      addMetaChip(row, "tokens", meta.tokens);
    } else if (event.type === "quiet") {
      addMetaChip(row, "", charsLabel(meta.chars));
      if (hasNumber(meta.hidden_chars)) addMetaChip(row, "", `隐藏 ${meta.hidden_chars} 字`);
    } else if (event.type === "speak") {
      addMetaChip(row, "", charsLabel(meta.chars));
      addMetaChip(row, "tokens", meta.tokens);
    } else if (event.type === "dstate") {
      addMetaChip(row, "迭代", meta.iterations);
      addMetaChip(row, "更新", meta.updated ? formatTime(meta.updated) : "");
    } else if (event.type === "prompt") {
      addMetaChip(row, "", charsLabel(meta.chars));
      if (hasNumber(meta.hidden_chars)) addMetaChip(row, "", `隐藏 ${meta.hidden_chars} 字`);
    } else if (event.type === "first_token") {
      if (hasNumber(meta.latency_ms)) addMetaChip(row, "等待", formatElapsed(meta.latency_ms));
    } else if (event.type === "slow") {
      addMetaChip(row, "反应", meta.reaction);
      if (hasNumber(meta.latency_ms)) addMetaChip(row, "延迟", formatElapsed(meta.latency_ms));
      if (hasNumber(meta.confidence)) addMetaChip(row, "置信", formatPercent(meta.confidence));
    } else if (event.type === "think") {
      addMetaChip(row, "tokens", meta.tokens);
    }

    if (row.childElementCount === 0) return;
    card.appendChild(row);
  }

  function buildCard(event) {
    const card = element("article", `event type-${String(event.type).replace(/[^a-zA-Z0-9_-]/g, "") || "unknown"}`);
    card.dataset.group = TYPE_GROUP[event.type] || "";

    const head = element("div", "event-head");
    head.appendChild(element("span", "event-time", formatTime(event.ts)));
    if (event.type === "inner") head.appendChild(createLockIcon());
    const meta = TYPE_META[event.type];
    head.appendChild(element("span", "event-badge", meta ? meta.label : event.type));
    if (event.title) head.appendChild(element("span", "event-title", event.title));
    card.appendChild(head);

    addTextBlock(card, event);
    addMetaChips(card, event);
    return card;
  }

  function trimDom() {
    while (rendered.length > MAX_EVENTS_IN_DOM) {
      const oldest = rendered.shift();
      if (!oldest) break;
      if (oldest.el.parentNode) oldest.el.remove();
      const round = oldest.round;
      if (round && round.body.childElementCount === 0) {
        if (round.section.parentNode) round.section.remove();
        if (rounds.get(round.key) === round) rounds.delete(round.key);
      }
    }
    updateEmptyState();
  }

  function renderEvent(event) {
    if (event.type === "round_start") {
      handleRoundStart(event);
      return;
    }
    if (event.type === "round_end") {
      handleRoundEnd(event);
      return;
    }

    const round = ensureRound(event);
    if (event.type === "tool_call") round.toolCalls += 1;
    if (typeof event.meta.proactive === "boolean") round.proactive = event.meta.proactive;

    const card = buildCard(event);
    if (!cardMatchesFilter(card)) card.classList.add("filtered-out");
    round.body.appendChild(card);

    rendered.push({ el: card, round });
    refreshRoundHeader(round);
    updateRoundVisibility(round);
    if (rendered.length > MAX_EVENTS_IN_DOM) trimDom();
  }

  // ── pause / clear ────────────────────────────────────────────────────

  function updatePausedBadge() {
    if (!paused) {
      pausedBadge.hidden = true;
      return;
    }
    pausedBadge.hidden = false;
    pausedBadge.textContent = `已暂停 · ${pendingEvents.length} 条`;
  }

  function setPaused(value) {
    paused = !!value;
    pauseBtn.textContent = paused ? "继续" : "暂停";
    pauseBtn.classList.toggle("active", paused);
    pauseBtn.setAttribute("aria-pressed", String(paused));
    if (!paused) {
      const queued = pendingEvents;
      pendingEvents = [];
      for (const event of queued) renderEvent(event);
      if (autoscroll) scrollToBottom();
    }
    updatePausedBadge();
  }

  function clearFeed() {
    feed.textContent = "";
    rounds.clear();
    rendered = [];
    seenIds.clear();
    seenOrder.length = 0;
    pendingEvents = [];
    eventCount = 0;
    updateEventCount();
    updatePausedBadge();
    jumpBtn.hidden = true;
    updateEmptyState();
  }

  // ── SSE connection ───────────────────────────────────────────────────

  function scheduleReconnect() {
    if (disposed || retryTimer) return;
    const delay = BACKOFF_MS[Math.min(retryAttempt, BACKOFF_MS.length - 1)];
    retryAttempt += 1;
    setStatus("disconnected", `断开，重连中（${delay / 1000}s）`);
    retryTimer = setTimeout(() => {
      retryTimer = null;
      if (!disposed) void connect();
    }, delay);
  }

  function handleSseBlock(block) {
    if (!block) return;
    const lines = block.split(/\r?\n/);
    const dataLines = [];
    for (const line of lines) {
      if (!line.startsWith("data:")) continue;
      let value = line.slice(5);
      if (value.startsWith(" ")) value = value.slice(1);
      dataLines.push(value);
    }
    if (!dataLines.length) return;
    const payload = dataLines.join("\n");
    if (!payload.trim()) return;
    try {
      acceptEvent(JSON.parse(payload));
    } catch {
      // A malformed frame must never kill the stream.
    }
  }

  async function connect() {
    if (disposed) return;
    if (retryTimer) {
      clearTimeout(retryTimer);
      retryTimer = null;
    }
    if (activeController) {
      try { activeController.abort(); } catch {}
    }

    const myGeneration = ++generation;
    const controller = new AbortController();
    activeController = controller;
    setStatus("connecting", retryAttempt > 0 ? "重连中" : "连接中");

    try {
      if (!window.petBrain || typeof window.petBrain.info !== "function") {
        throw new Error("brain preload unavailable");
      }
      const info = await window.petBrain.info();
      if (myGeneration !== generation || controller.signal.aborted) return;
      const baseUrl = info && typeof info.url === "string" ? info.url.replace(/\/+$/, "") : "";
      if (!baseUrl) throw new Error("gateway url unavailable");

      const response = await fetch(`${baseUrl}/api/brain/stream`, {
        method: "GET",
        headers: { "x-pet-token": info.token || "" },
        cache: "no-store",
        signal: controller.signal,
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      if (!response.body || typeof response.body.getReader !== "function") {
        throw new Error("streaming unsupported");
      }

      setStatus("connected", "已连接");
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const blocks = buffer.split(/\r?\n\r?\n/);
        buffer = blocks.pop() || "";
        for (const block of blocks) handleSseBlock(block);
      }
      buffer += decoder.decode();
      if (buffer.trim()) handleSseBlock(buffer);
    } catch (err) {
      if (disposed || myGeneration !== generation || controller.signal.aborted) return;
    }

    if (disposed || myGeneration !== generation) return;
    scheduleReconnect();
  }

  // ── wiring ───────────────────────────────────────────────────────────

  filtersEl.addEventListener("click", (event) => {
    const button = event.target instanceof Element ? event.target.closest("[data-filter]") : null;
    if (!button) return;
    currentFilter = button.dataset.filter || "all";
    for (const chip of filtersEl.querySelectorAll(".chip")) {
      chip.classList.toggle("active", chip === button);
    }
    applyFilters();
  });

  pauseBtn.addEventListener("click", () => setPaused(!paused));
  clearBtn.addEventListener("click", clearFeed);

  autoscrollBtn.addEventListener("click", () => {
    setAutoscroll(!autoscroll);
  });

  jumpBtn.addEventListener("click", () => {
    setAutoscroll(true);
  });

  feed.addEventListener("scroll", () => {
    if (isNearBottom()) {
      jumpBtn.hidden = true;
      return;
    }
    if (autoscroll) setAutoscroll(false, { silent: true });
    jumpBtn.hidden = false;
  });

  window.addEventListener("beforeunload", () => {
    disposed = true;
    generation += 1;
    if (retryTimer) {
      clearTimeout(retryTimer);
      retryTimer = null;
    }
    if (activeController) {
      try { activeController.abort(); } catch {}
    }
  });

  updateEventCount();
  updateEmptyState();
  setAutoscroll(true);
  void connect();
})();
