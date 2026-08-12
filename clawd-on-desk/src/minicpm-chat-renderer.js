"use strict";
// MiniCPM Chat renderer — extracted from minicpm-chat.html so we can ship
// it as an external <script> (CSP `script-src 'self'`) and so the chat UI
// can be localized via `minicpm-i18n.js` (loaded as a UMD before this file).
//
// All hardcoded user-facing strings are routed through the translator
// `t(key, params)` driven by `currentLang`. Command regexes and the
// LLM classifier prompt likewise come from per-language config so the
// pet's natural-language commands work in en / zh / zh-TW / ko / ja.
//
// The actual flow / typewriter / streaming logic is unchanged from the
// original inline script — only the strings have moved.

// ── i18n bootstrap ──
const minicpmI18n = (typeof globalThis !== "undefined" && globalThis.ClawdMinicpmI18n) || null;
const chatContext = (typeof globalThis !== "undefined" && globalThis.ClawdMinicpmChatContext) || null;
let currentLang = "en";
const t = minicpmI18n ? minicpmI18n.makeTranslator(() => currentLang) : (k) => k;
let RGX = minicpmI18n ? minicpmI18n.getCommandPatterns(currentLang) : {};
let COMMAND_HINTS = RGX.hints || /./;
let CLASSIFIER_PROMPT = minicpmI18n ? minicpmI18n.getClassifierPrompt(currentLang) : "";

function applyLang(lang) {
  if (typeof lang !== "string" || !lang) return;
  currentLang = lang;
  if (minicpmI18n) {
    RGX = minicpmI18n.getCommandPatterns(lang);
    COMMAND_HINTS = RGX.hints || /./;
    CLASSIFIER_PROMPT = minicpmI18n.getClassifierPrompt(lang);
  }
  try { document.documentElement.setAttribute("lang", lang); } catch {}
  try { document.title = "MiniCPM"; } catch {}
  // Update statically-rendered strings (update pill, ask placeholder, etc.).
  refreshStaticUi();
}

function refreshStaticUi() {
  if (!updPill) return;
  updPill.title = t("chatUpdatePillTitle");
  // If pill is showing default text (no remote_revision merged in), refresh.
  if (updPillRevision == null) {
    updPill.textContent = t("chatUpdatePillText");
  }
  // If the user is currently in ask phase with the empty placeholder,
  // refresh it so a language change takes effect immediately.
  if (inputEl) {
    inputEl.placeholder = t("chatAskPlaceholder");
  }
}

// Event listener: open native context menu on right-click. Replaced the
// inline `oncontextmenu` attribute since CSP no longer permits inline JS.
document.body.addEventListener("contextmenu", (event) => {
  event.preventDefault();
  if (window.minicpm && typeof window.minicpm.openContextMenu === "function") {
    window.minicpm.openContextMenu();
  }
});

const SIDECAR_URL = "http://127.0.0.1:18765";

// ── element refs ──
const bubble = document.getElementById("bubble");
const content = document.getElementById("content");
const updPill = document.getElementById("updPill");

// ── module state ──
let phase = "hidden";        // hidden | starting | ask | thinking | speak | error
let booted = false;
let sidecarUrl = null;
// 每个动画主题（身体）拥有自己的对话流（换身体 = 换灵魂）。
// 历史按主题分桶，切换主题只换指针：
//   chatHistoryByTheme = { "default": [...], "cybercat": [...], ... }
// 曾经的 assistant/topic 桶结构（话题 UI）已移除；这里只是
// "一个身体一个对话流"——没有话题管理界面。
let _currentThemeId = "default";
let chatHistoryByTheme = { default: [] };

function _themeId() { return _currentThemeId; }

function curHistory() {
  if (!chatHistoryByTheme[_currentThemeId]) chatHistoryByTheme[_currentThemeId] = [];
  return chatHistoryByTheme[_currentThemeId];
}

// ── Chat history persistence ─────────────────────────────────────────
let _persistTimer = null;

// Strip the [EMOTION:xxx] / [NEXT_CHAT:xxx] control tags the model appends
// for the desk-pet animation + proactive-chat subsystems. The gateway
// already parsed these server-side; they must never reach the user, the
// saved history, or the next-turn prompt. Strips globally (not just at the
// tail) because chained end-anchored passes leave [EMOTION:] behind when
// [NEXT_CHAT:] follows it, and small models occasionally emit a tag
// mid-text. Applied to assistant replies on generation AND on restore so
// older history files written before this scrub self-heal on next load.
function sanitizeReplyTags(text) {
  if (typeof text !== "string") return text;
  return text
    .replace(/\s*\[EMOTION:[a-z_]+\]\s*/gi, "")
    .replace(/\s*\[NEXT_CHAT:\d+\s*(?:s|sec|seconds)?\s*\]\s*/gi, "")
    // Strip the <<<MEM>>>…<<<MEMEND>>> event/mood block the model emits
    // for memory extraction. The gateway parses it from the raw stream
    // before this scrub; it must never reach the user or next-turn prompt.
    .replace(/\s*<<<MEM>>>[\s\S]*?<<<MEMEND>>>\s*/g, "")
    .replace(/\r?\n[ \t]+$/g, "");
}

// ── LingLing: episodic memory + mood + micro-animations ──────────────

let _linglingMood = "平静";
let _linglingBuffer = "";

/**
 * Fetch current mood from sidecar and update micro-animation baseline.
 */
async function _linglingFetchMood() {
  try {
    const resp = await fetch(sidecarUrl + "/api/mood");
    if (resp.ok) {
      const data = await resp.json();
      _linglingMood = data.mood || "平静";
    }
  } catch {}
}

/**
 * Token-level micro-animation trigger (simplified, no external dep).
 * Maps specific characters to subtle CSS animations on the pet container.
 */
function _linglingOnToken(token) {
  _linglingBuffer += token;
  // Check combos first
  const combos = {
    "……！": "lingling-h-burst",
    "！？": "lingling-shock",
    "……～": "lingling-shy",
  };
  for (const [pattern, cls] of Object.entries(combos)) {
    if (_linglingBuffer.endsWith(pattern)) {
      _triggerLinglingAnim(cls);
      _linglingBuffer = _linglingBuffer.slice(0, -pattern.length);
      return;
    }
  }
  // Single chars
  const singles = {
    "！": "lingling-startle",
    "？": "lingling-tilt",
    "…": "lingling-hesitate",
    "～": "lingling-sway",
    "。": "lingling-settle",
    "嗯": "lingling-nod",
    "哼": "lingling-turn",
    "呜": "lingling-shrink",
    "诶": "lingling-perk",
  };
  const lastChar = token.slice(-1);
  if (singles[lastChar]) {
    _triggerLinglingAnim(singles[lastChar]);
  }
}

function _triggerLinglingAnim(cls) {
  const pet = document.getElementById("pet-container") || document.getElementById("clawd");
  if (!pet) return;
  // Remove all lingling classes
  pet.className = pet.className.replace(/\blingling-\S+/g, "").trim();
  void pet.offsetWidth; // reflow
  pet.classList.add(cls);
  setTimeout(() => pet.classList.remove(cls), 1200);
}

/**
 * After streaming completes, ask the sidecar to extract events and
 * update mood from the model's response. This is the LingLing
 * "write to memory" step — the model's reply is its diary entry.
 */
async function _linglingExtractEvents(replyText) {
  try {
    await fetch(sidecarUrl + "/api/events/extract", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ response_text: replyText }),
    });
  } catch {}
}

/**
 * Inject the LingLing micro-animation CSS once.
 */
function _injectLinglingStyles() {
  if (document.getElementById("lingling-css")) return;
  const s = document.createElement("style");
  s.id = "lingling-css";
  s.textContent = `
    .lingling-startle{animation:ll-startle .4s ease-out}
    @keyframes ll-startle{0%{transform:scale(1)}30%{transform:scale(1.15) translateY(-2px)}100%{transform:scale(1)}}
    .lingling-tilt{animation:ll-tilt .6s ease-in-out}
    @keyframes ll-tilt{0%,100%{transform:rotate(0)}50%{transform:rotate(5deg)}}
    .lingling-hesitate{animation:ll-hes .8s ease-in-out}
    @keyframes ll-hes{0%,100%{opacity:1;transform:translateX(0)}25%{opacity:.6;transform:translateX(-1px)}75%{opacity:.8;transform:translateX(1px)}}
    .lingling-sway{animation:ll-sway 1.2s ease-in-out}
    @keyframes ll-sway{0%,100%{transform:rotate(0)}25%{transform:rotate(3deg)}75%{transform:rotate(-3deg)}}
    .lingling-settle{animation:ll-settle .5s ease-out}
    @keyframes ll-settle{0%{transform:translateY(-1px)}100%{transform:translateY(0)}}
    .lingling-nod{animation:ll-nod .4s ease-in-out}
    @keyframes ll-nod{0%,100%{transform:translateY(0)}50%{transform:translateY(2px)}}
    .lingling-turn{animation:ll-turn .6s ease-in-out}
    @keyframes ll-turn{0%{transform:scaleX(1)}50%{transform:scaleX(-.9)}100%{transform:scaleX(1)}}
    .lingling-shrink{animation:ll-shrink .5s ease-out}
    @keyframes ll-shrink{0%{transform:scale(1)}50%{transform:scale(.9)}100%{transform:scale(1)}}
    .lingling-perk{animation:ll-perk .4s ease-out}
    @keyframes ll-perk{0%{transform:scale(1) translateY(0)}50%{transform:scale(1.05) translateY(-3px)}100%{transform:scale(1) translateY(0)}}
    .lingling-h-burst{animation:ll-hburst 1.5s ease-in-out}
    @keyframes ll-hburst{0%,20%{opacity:.5;transform:scale(.95)}40%{opacity:.8}60%{opacity:1;transform:scale(1.1) translateY(-3px)}100%{transform:scale(1)}}
    .lingling-shock{animation:ll-shock .8s ease-in-out}
    @keyframes ll-shock{0%{transform:scale(1.1) rotate(-3deg)}50%{transform:scale(1.05) rotate(3deg)}100%{transform:scale(1) rotate(0)}}
    .lingling-shy{animation:ll-shy 1.2s ease-in-out}
    @keyframes ll-shy{0%,100%{transform:translateX(0) rotate(0);opacity:1}30%{transform:translateX(-2px) rotate(-2deg);opacity:.7}70%{transform:translateX(2px) rotate(2deg);opacity:.8}}
  `;
  document.head.appendChild(s);
}

// Build a clean, serializable copy of ALL themes' chats with image data
// (base64 screenshots) stripped out so the file stays small.
// Shape: { themeId: [ {role, content}, ... ], ... }
function _cleanHistoryForSave() {
  const out = {};
  for (const [themeId, msgs] of Object.entries(chatHistoryByTheme)) {
    out[themeId] = (msgs || []).map((m) => {
      const c = m.content;
      if (Array.isArray(c)) {
        return { role: m.role, content: c.filter(b => b && b.type === "text").map(b => b.text).join("") };
      }
      return { role: m.role, content: typeof c === "string" ? c : String(c || "") };
    });
  }
  return out;
}

function _persistHistoryNow() {
  try {
    if (window.minicpm && typeof window.minicpm.saveHistory === "function") {
      window.minicpm.saveHistory(_cleanHistoryForSave());
    }
  } catch {}
}

function _persistHistory() {
  if (_persistTimer) clearTimeout(_persistTimer);
  _persistTimer = setTimeout(() => {
    _persistTimer = null;
    _persistHistoryNow();
  }, 500);
}

// Self-heal: strip fossilized [EMOTION:]/[NEXT_CHAT:]/<<<MEM>>> tags from
// assistant turns on load so they stop being echoed back to the model.
function _sanitizeMessages(messages) {
  return (messages || []).map((m) => {
    if (m && m.role === "assistant" && typeof m.content === "string") {
      return { ...m, content: sanitizeReplyTags(m.content) };
    }
    return m;
  });
}

// Migrate a saved history file into the theme-bucket shape:
//   - current format {themeId: [messages]}  → as-is
//   - single array [ {role, content}, ... ] → { default: [...] }
//   - legacy assistant buckets {assistant: {activeTopicId, topics}}:
//     each assistant bucket becomes its own theme bucket (cybercat →
//     "cybercat"), "default" keeps the default bucket. The first non-empty
//     legacy format collapses to default when nothing maps cleanly.
function _migrateSavedHistory(saved) {
  if (Array.isArray(saved)) {
    return { default: _sanitizeMessages(saved) };
  }
  if (saved && typeof saved === "object") {
    const out = {};
    let legacyBuckets = false;
    for (const [key, value] of Object.entries(saved)) {
      if (Array.isArray(value)) {
        out[key] = _sanitizeMessages(value);
      } else if (value && typeof value === "object" && value.topics) {
        legacyBuckets = true;
        const pickActive = (bucket) =>
          bucket.topics[bucket.activeTopicId]
            ? bucket.topics[bucket.activeTopicId].messages
            : null;
        const msgs = pickActive(value);
        out[key === "default" ? "default" : key] = _sanitizeMessages(msgs || []);
      }
    }
    if (legacyBuckets && Object.keys(out).length > 0) {
      return out;
    }
    // Unknown object shape — collapse into default.
    const msgs = (saved.default && (Array.isArray(saved.default) ? saved.default : null)) || [];
    return { default: _sanitizeMessages(msgs) };
  }
  return { default: [] };
}

async function _restoreHistory() {
  try {
    if (window.minicpm && typeof window.minicpm.loadHistory === "function") {
      const saved = await window.minicpm.loadHistory();
      if (saved != null) {
        chatHistoryByTheme = _migrateSavedHistory(saved);
        if (!chatHistoryByTheme[_currentThemeId]) chatHistoryByTheme[_currentThemeId] = [];
      }
    }
  } catch {}
}

// Theme switch (换身体 = 换灵魂): switch the active conversation stream.
function _setThemeId(themeId) {
  const next = (typeof themeId === "string" && themeId.trim()) ? themeId.trim() : "default";
  if (next === _currentThemeId) return;
  _currentThemeId = next;
  if (!chatHistoryByTheme[next]) chatHistoryByTheme[next] = [];
  try { _skillsContext = null; _skillsContextAt = 0; } catch {}
}
// "history" is exposed as a Proxy that mutates the active theme's messages
// transparently. Existing code paths using history.push/length/slice work
// unchanged; assignment history = [...] makes that array the new content.
const history = new Proxy([], {
  get(_t, prop) {
    if (prop === Symbol.iterator || prop === "length" || typeof prop === "string" && /^\d+$/.test(prop)) {
      return Reflect.get(curHistory(), prop);
    }
    const val = curHistory()[prop];
    return typeof val === "function" ? val.bind(curHistory()) : val;
  },
  set(_t, prop, value) {
    if (typeof prop === "string" && /^\d+$/.test(prop)) {
      curHistory()[prop] = value;
      return true;
    }
    if (prop === "length") { curHistory().length = value; return true; }
    return Reflect.set(curHistory(), prop, value);
  },
  deleteProperty(_t, prop) {
    if (typeof prop === "string" && /^\d+$/.test(prop)) {
      curHistory().splice(Number(prop), 1);
      return true;
    }
    return Reflect.deleteProperty(curHistory(), prop);
  },
  has(_t, prop) {
    return Reflect.has(curHistory(), prop);
  },
  ownKeys() {
    return Reflect.ownKeys(curHistory());
  },
  getOwnPropertyDescriptor(_t, prop) {
    return Reflect.getOwnPropertyDescriptor(curHistory(), prop);
  },
});
function replaceActiveHistory(arr) {
  // Clear the CURRENT theme's conversation (换身体 = 换灵魂).
  chatHistoryByTheme[_currentThemeId] = Array.isArray(arr) ? arr : [];
  _persistHistory();
}
let abortCtrl = null;
let fadeTimer = null;
let inputEl = null;          // <textarea> while in ask state
// Tracks the latest remote revision shown in the update pill so we can
// re-render its label on a language change without losing the version.
let updPillRevision = null;

// Persisted default lives in minicpm-prefs.json (Settings → 默认思考模式).
// thinkingOverride is a per-session override from ⌘⇧T; null means follow
// the persisted default on each submit.
let thinkingOverride = null;

// Proactive chat: the model can schedule its next message via [NEXT_CHAT:N]
// where N is integer seconds (0 = disable). Default when model omits the tag
// is 900 seconds (15 minutes).
let _nextChatSeconds = 0;
// True only when the model explicitly emitted a [NEXT_CHAT:N] tag THIS turn
// (the gateway forwards it as a `next_chat` SSE event). Lets us tell an
// explicit "rest / don't bother me" (N=0) apart from "model forgot to emit
// a tag" — both leave _nextChatSeconds=0, but only the former should stop
// proactive chat. Reset at the top of submit() each turn.
let _nextChatExplicit = false;
let _proactiveTimer = null;
let _lastUserActivity = Date.now();
let _lastProactivePrompt = "";   // dedupe so we don't fire the same prompt twice
const DEFAULT_PROACTIVE_IDLE_SECONDS = 900; // 15 minutes

// Screen observation context, set by submitProactive() and consumed by submit().
// Contains OmniParser parsed content + optional raw screenshot base64 for vision
// providers. Reset to null after each submit().
let _pendingScreenContext = null;

// Keywords that indicate the user wants the AI to look at their screen.
// When matched in a user message, submit() will fetch screen context.
const _SCREEN_REQUEST_KEYWORDS = [
  "看看我", "看我", "看我屏幕", "看看我屏幕",
  "截图", "截屏", "看我在做", "看看我在",
  "屏幕", "桌面", "在干什么", "在干嘛",
  "看我桌面", "你看我",
];

function _isScreenRequest(text) {
  const t = (text || "").toLowerCase();
  return _SCREEN_REQUEST_KEYWORDS.some(function(kw) { return t.indexOf(kw) !== -1; });
}

// Heuristic to detect whether the current provider+model supports vision
// (i.e. can process image_url content blocks). Used to decide whether to
// inject rawScreenshotBase64 alongside the OmniParser text results.
function _providerSupportsVision(providerPrefs) {
  const provider = (providerPrefs.defaultProvider || "local").toLowerCase();
  const provCfg = (providerPrefs.modelProviders || []).find(function(p) {
    return p.provider === providerPrefs.defaultProvider;
  });
  const model = (provCfg && provCfg.model || "").toLowerCase();
  // Anthropic — all Claude models support images
  if (provider === "anthropic") return true;
  // Known vision-capable models / name substrings
  var visionPatterns = [
    "gpt-4o", "gpt-4.1", "gpt-4-vision",
    "claude-3", "claude-3.5", "claude-4",
    "deepseek-v4-flash",
    "gemini-2.0", "gemini-2.5",
    "glm-4v",
    "qwen-vl", "qwen2-vl", "qwen2.5-vl", "qwen3",
    "llava", "cogvlm", "internvl",
    "minicpm-v", "minicpm5",
  ];
  return visionPatterns.some(function(p) {
    return model.indexOf(p) !== -1 || provider.indexOf(p) !== -1;
  });
}

async function _fetchScreenContext() {
  if (_pendingScreenContext) return;
  // A vision-capable model (mmproj loaded / vision provider) sees the
  // screen directly — skip the OCR pre-injection and let the MODEL call
  // capture_screen itself ("看看我的屏幕" = look, not OCR). OCR stays the
  // fallback for text-only models.
  let providerPrefs = { defaultProvider: "local", autoRoute: false, modelProviders: [] };
  try {
    if (window.minicpm && typeof window.minicpm.getProviderPrefs === "function") {
      providerPrefs = (await window.minicpm.getProviderPrefs()) || providerPrefs;
    }
  } catch {}
  if (_providerSupportsVision(providerPrefs)) return;
  try {
    const healthResp = await fetch(sidecarUrl + "/api/health", { signal: AbortSignal.timeout(2000) }).catch(() => null);
    if (healthResp && healthResp.ok) {
      const h = await healthResp.json().catch(() => null);
      if (h && h.has_vision) return;
    }
  } catch {}
  try {
    let consent = "once";
    // Read persisted consent from main process
    if (window.minicpm && typeof window.minicpm.getProviderPrefs === "function") {
      const pp = await window.minicpm.getProviderPrefs();
      if (pp && pp.screenObserveConsent && pp.screenObserveConsent !== "deny") {
        consent = pp.screenObserveConsent;
      }
    }
    // Sync to gateway
    const syncResp = await fetch(sidecarUrl + "/api/screen/consent-status", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ consent }),
    });
    if (!syncResp.ok) return;
    const syncData = await syncResp.json();
    if (syncData.current_consent === "deny") return;
    // Read OCR prefs from providerPrefs (re-use the already-fetched pp)
    var ocrConfig = {
      ocr_mode: (pp && pp.screenclick_ocrMode) || "local",
      ocr_api_url: (pp && pp.screenclick_ocrApiUrl) || "",
      ocr_api_key: (pp && pp.screenclick_ocrApiKey) || "",
      ocr_api_model: (pp && pp.screenclick_ocrApiModel) || "",
      chinese_ocr: !!(pp && pp.screenclick_chineseOcr),
    };
    // Observe — no artificial timeout; on GPU machines this is ~3-5s,
    // on CPU it may take a few minutes. The gateway has its own 300s
    // timeout when talking to OmniParser.
    let observeResp;
    try {
      observeResp = await fetch(sidecarUrl + "/api/screen/observe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(ocrConfig),
      });
    } catch (fetchErr) {
      console.warn("[screen observe] failed:", fetchErr);
      return;
    }
    if (!observeResp.ok) return;
    const observeData = await observeResp.json();
    if (observeData.timed_out) return;
    if (observeData.consent_consumed) {
      try {
        if (window.minicpm && typeof window.minicpm.setScreenObserveConsent === "function") {
          await window.minicpm.setScreenObserveConsent("deny");
        }
      } catch {}
    }
    _pendingScreenContext = {
      parsedContent: Array.isArray(observeData.parsed_content_list)
        ? observeData.parsed_content_list.join("\n")
        : "",
      rawScreenshotBase64: observeData.raw_screenshot_base64 || null,
    };
  } catch (err) {
    console.warn("[screen observe] fetch failed:", err);
  }
}

// Mode defaults to "free" — model decides via [NEXT_CHAT:N]. Settings can
// override through setProactivePolicy().
window.__proactiveMode = "free";
window.__proactiveIntervalSeconds = DEFAULT_PROACTIVE_IDLE_SECONDS;

// Hint pushed to the model when the timer fires. Seconds form matches what
// the model was told to emit, so guidance stays consistent.
function proactivePromptMessage(seconds) {
  if (seconds > 3600) return `[系统提示：${seconds}秒（约${Math.round(seconds/3600)}小时）已到，你想跟用户说点什么吗？保持简短自然，不要提"系统提示"四个字。如果不想说话就回一个空短句。]`;
  if (seconds > 60)   return `[系统提示：${seconds}秒（${Math.round(seconds/60)}分钟）已到，你想跟用户说点什么吗？保持简短自然，不要提"系统提示"四个字。如果不想说话就回一个空短句。]`;
  return `[系统提示：${seconds}秒已到，你想跟用户说点什么吗？保持简短自然，不要提"系统提示"四个字。如果不想说话就回一个空短句。]`;
}

function resolveThinking(chatParams) {
  if (typeof thinkingOverride === "boolean") return thinkingOverride;
  return !!(chatParams && chatParams.thinking);
}

// ── window helpers ──
function clearFade() {
  if (fadeTimer) { clearTimeout(fadeTimer); fadeTimer = null; }
}

async function setBubbleSize(width, height) {
  // shell has 7px inset on each side (just enough for the tail to poke out)
  if (window.minicpm && window.minicpm.resize) {
    await window.minicpm.resize(width + 14, height + 14);
  }
}

async function showBubble() {
  clearFade();
  bubble.classList.remove("fading");
  // The window itself may have been hidden via hideWindow() while we were
  // waiting on the model. Bring it back before animating the bubble in.
  if (window.minicpm && window.minicpm.showWindow) {
    try { await window.minicpm.showWindow(); } catch {}
  }
  requestAnimationFrame(() => bubble.classList.add("show"));
}

async function hideBubble({ fade = true } = {}) {
  clearFade();
  await clearChatAnchor();
  if (!fade) {
    bubble.classList.remove("show", "fading");
    if (window.minicpm && window.minicpm.hideWindow) await window.minicpm.hideWindow();
    phase = "hidden";
    return;
  }
  bubble.classList.add("fading");
  bubble.classList.remove("show");
  fadeTimer = setTimeout(async () => {
    fadeTimer = null;
    bubble.classList.remove("fading");
    if (window.minicpm && window.minicpm.hideWindow) await window.minicpm.hideWindow();
    phase = "hidden";
  }, 220);
}

function setSide(side) {
  // side from main: 'right' | 'left' | 'above' | 'below'
  bubble.setAttribute("data-side", side || "right");
}

// ── bootstrap (Python sidecar) ──
async function ensureBooted() {
  if (booted) return true;
  showStarting();
  const r = await window.minicpm.start({});
  if (!r.ok) {
    showError(r.error || t("chatStartingError"));
    return false;
  }
  sidecarUrl = r.url || SIDECAR_URL;
  booted = true;
  // LingLing: inject micro-animation CSS + fetch current mood
  _injectLinglingStyles();
  _linglingFetchMood();
  return true;
}

// ── Proactive chat ──
// The model can schedule its next spontaneous message. When the timer fires,
// we call submit() with a system prompt so the model "remembers" it wanted to
// chat. User activity cancels the timer.

function scheduleProactiveChat() {
  cancelProactiveChat();
  // Mode "off" → never auto-speak. Mode "free" → model decides via [NEXT_CHAT].
  // Mode "interval" → ignore model's [NEXT_CHAT] and use _intervalSeconds.
  if (window.__proactiveMode === "off") return;
  let seconds;
  if (window.__proactiveMode === "interval") {
    seconds = Number(window.__proactiveIntervalSeconds) || 1800;
  } else {
    // "free" mode: honor the model's [NEXT_CHAT:N] from this turn. If the
    // model explicitly emitted a tag (incl. N=0 → "don't bother me now"),
    // _nextChatExplicit is true and we must respect the value as-is —
    // including 0, which means do NOT schedule. Only when the model
    // forgot to emit a tag at all do we fall back to the default idle
    // interval so the pet still speaks proactively.
    if (_nextChatExplicit) {
      if (_nextChatSeconds <= 0) return; // explicit "don't proactively chat"
      seconds = _nextChatSeconds;
    } else {
      seconds = DEFAULT_PROACTIVE_IDLE_SECONDS;
    }
  }
  const delay = Math.min(seconds * 1000, 24 * 3600 * 1000); // cap at 24h
  _proactiveTimer = setTimeout(() => {
    _proactiveTimer = null;
    // Don't interrupt if user is actively chatting
    if (phase === "ask" || phase === "thinking" || phase === "speak") {
      _lastUserActivity = Date.now();
      scheduleProactiveChat(); // retry after idle
      return;
    }
    submitProactive();
  }, delay);
}

// Accept proactive policy from the settings UI:
//   policy = "free"            (model decides via [NEXT_CHAT:N])
//   policy = "interval:<N>"    (fixed N seconds, model tag is ignored)
//   policy = "off"             (never auto-speak)
function setProactivePolicy(policy) {
  if (typeof policy !== "string") return;
  cancelProactiveChat();
  if (policy === "off") {
    window.__proactiveMode = "off";
    _nextChatSeconds = 0;
    return;
  }
  if (policy.startsWith("interval:")) {
    window.__proactiveMode = "interval";
    window.__proactiveIntervalSeconds = Math.max(60, parseInt(policy.slice("interval:".length), 10) || 1800);
    _nextChatSeconds = 0;
    scheduleProactiveChat();
    return;
  }
  // default: free
  window.__proactiveMode = "free";
  window.__proactiveIntervalSeconds = DEFAULT_PROACTIVE_IDLE_SECONDS;
  scheduleProactiveChat();
}

function cancelProactiveChat() {
  if (_proactiveTimer) { clearTimeout(_proactiveTimer); _proactiveTimer = null; }
}

async function submitProactive() {
  // Don't interrupt an active chat session — reschedule instead.
  if (phase === "thinking" || phase === "speak") {
    _lastUserActivity = Date.now();
    scheduleProactiveChat();
    return;
  }
  const idleMinutes = Math.round((Date.now() - _lastUserActivity) / 60000);
  if (idleMinutes < 2) {
    // User was active moments ago — re-arm for the next idle interval
    // instead of letting proactive chat die silently (the timer callback
    // already cleared _proactiveTimer, so without this nothing fires
    // again until the user manually submits).
    scheduleProactiveChat();
    return;
  }
  // The model told us to wait N seconds; report N back so it knows how long
  // the user actually idle'd.
  const seconds = _nextChatSeconds > 0 ? _nextChatSeconds : DEFAULT_PROACTIVE_IDLE_SECONDS;
  const prompt = proactivePromptMessage(seconds);
  // dedupe: if submit() is already mid-flight or the same prompt fired,
  // skip to avoid double-bubble surprises — but still re-arm so the
  // next interval can fire (the model's [NEXT_CHAT] may differ by then).
  if (prompt === _lastProactivePrompt && phase !== "ask" && phase !== "hidden") {
    scheduleProactiveChat();
    return;
  }
  _lastProactivePrompt = prompt;

  // Make sure the bubble is visible — submit() calls showAsk() internally
  // which opens the window, but we need the window to exist first.
  if (window.minicpm && window.minicpm.showWindow) {
    try { await window.minicpm.showWindow(); } catch {}
  }

  // Fetch screen context if consent allows (reuses _fetchScreenContext helper).
  // Only fetch if not already pending (e.g. from a user request in submit()).
  if (!_pendingScreenContext) {
    await _fetchScreenContext();
  }

  await submit(prompt);
}

// ── render: starting ──
async function showStarting() {
  phase = "starting";
  const main = t("chatStarting");
  content.innerHTML =
    '<div class="thinking-row"><span class="spinner"></span><span>' +
    escapeHtml(main) +
    '</span></div>';
  await measureAndShow({ width: naturalDisplayWidth(main) });
}

// ── render: error ──
async function showError(msg) {
  phase = "error";
  content.innerHTML = `<div class="err-text">⚠️ ${escapeHtml(msg.split("\n")[0])}<pre>${escapeHtml(msg)}</pre></div>`;
  await measureAndShow({ width: naturalDisplayWidth(msg, { max: 360 }) });
}

// ── render: ask (input field) ──
// When there's a `lastReply`, render in continuous-chat mode: a fixed-
// size bubble where the previous reply scrolls inside its own region
// at the top, and the input box is pinned at the bottom. While typing,
// the bubble's outer dimensions stay locked — only inner regions scroll.
async function showAsk(lastReply, lastThinking) {
  clearFade();
  phase = "ask";
  abortCtrl = null;

  // Whenever we enter ask-with-reply, FIRST clear any prior chat anchor
  // (so initial measurement is centered), then re-pin after the bubble
  // has settled. This avoids the bubble sliding sideways on first show.
  if (window.minicpm && window.minicpm.setChatAnchor) {
    try { await window.minicpm.setChatAnchor(null); } catch {}
  }

  if (lastReply) {
    // Continuous-chat layout: bubble starts compact, grows as user types.
    // Outer height tracks content.offsetHeight via measureAndShow.
    const placeholder = escapeHtml(t("chatAskPlaceholder"));
    content.innerHTML =
      '<div class="chat-pane">' +
        '<div class="last-reply-region rendered" id="last-reply-region">' + renderMarkdown(lastReply) + '</div>' +
        '<div class="ask-input-wrap">' +
          '<textarea id="ask-input" placeholder="' + placeholder + '" rows="1"></textarea>' +
        '</div>' +
      '</div>';
    inputEl = document.getElementById("ask-input");
    inputEl.addEventListener("input", () => autoresizeFixed(inputEl));
    inputEl.addEventListener("keydown", onAskKey);
    await measureAndShow();

    // ── Add thinking toggle button if thinking content exists ──
    if (lastThinking && lastThinking.trim()) {
      var lr = document.getElementById("last-reply-region");
      if (lr) {
        var tt = document.createElement("button");
        tt.textContent = "🧠 查看思考";
        tt.style.cssText = "font-size:11px;padding:2px 8px;margin-top:6px;border:1px solid var(--border,#45475a);border-radius:4px;background:transparent;color:var(--text-secondary,#8899b0);cursor:pointer;";
        var tb = document.createElement("div");
        tb.style.cssText = "display:none;margin-top:6px;padding:8px;border-radius:6px;background:rgba(255,255,255,0.03);border:1px solid var(--border,#45475a);font-size:12px;line-height:1.5;white-space:pre-wrap;max-height:300px;overflow-y:auto;color:var(--text-secondary,#8899b0);";
        tb.textContent = lastThinking;
        tt.addEventListener("click", function() {
          var shown = tb.style.display !== "none";
          tb.style.display = shown ? "none" : "block";
          tt.textContent = shown ? "🧠 查看思考" : "🧠 收起思考";
        });
        lr.appendChild(tt);
        lr.appendChild(tb);
      }
    }

    const lr2 = document.getElementById("last-reply-region");
    if (lr2) lr2.scrollTop = lr2.scrollHeight;
    // Now that the bubble is sized & placed, pin its bottom Y so future
    // grows extend UP (textarea stays under the user's gaze) instead of
    // re-centering on the pet (which would shove the textarea down too).
    if (window.minicpm && window.minicpm.setChatAnchor) {
      const r = bubble.getBoundingClientRect();
      // measureAndShow → setBounds set the window position; we need its
      // BOTTOM in screen coords. window.screenY + window inner height
      // approximates the bottom of the bubble panel.
      const bottomY = (window.screenY || 0) + window.innerHeight;
      try { await window.minicpm.setChatAnchor(bottomY); } catch {}
    }
  } else {
    // First-open compact layout: bubble starts as a tiny pill that just
    // fits the placeholder text, expanding horizontally as user types.
    const placeholder = escapeHtml(t("chatAskPlaceholder"));
    content.innerHTML =
      '<textarea id="ask-input" placeholder="' + placeholder + '" rows="1"></textarea>';
    inputEl = document.getElementById("ask-input");
    inputEl.addEventListener("input", () => autoresize(inputEl));
    inputEl.addEventListener("keydown", onAskKey);
    await measureAndShow({ width: naturalAskWidth("") });
  }
  if (window.minicpm && window.minicpm.focusWindow) {
    try { await window.minicpm.focusWindow(); } catch {}
  }
  setTimeout(() => inputEl && inputEl.focus(), 30);
}

// In continuous-chat mode the bubble follows content size: empty = small,
// long input = big. Resize the textarea internally then ask the window
// to remeasure so its outer height tracks. animate:false avoids the
// CSS opacity transition kicking in on every keystroke.
function autoresizeFixed(ta) {
  ta.style.height = "auto";
  ta.style.height = Math.min(ta.scrollHeight, 200) + "px";
  measureAndShow({ animate: false });
}

function autoresize(ta) {
  ta.style.height = "auto";
  ta.style.height = Math.min(ta.scrollHeight, 96) + "px";
  // Bubble width grows with text — empty = ~100px, full multi-line = 320px max.
  const w = naturalAskWidth(ta.value);
  measureAndShow({ animate: false, width: w });
}

async function onAskKey(e) {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    const text = inputEl.value.trim();
    if (!text) return;
    inputEl.value = "";
    await submit(text);
  } else if (e.key === "Escape") {
    e.preventDefault();
    await dismiss();
  }
}

// ── render: thinking (transient — pet does the heavy work animating) ──
async function showThinking(label) {
  phase = "thinking";
  const text = label != null ? label : t("chatThinkingDefault");
  content.innerHTML = '<div class="thinking-row"><span class="spinner"></span><span>' + escapeHtml(text) + '</span></div>';
  await measureAndShow();
}

async function clearChatAnchor() {
  if (window.minicpm && window.minicpm.setChatAnchor) {
    try { await window.minicpm.setChatAnchor(null); } catch {}
  }
}

// ── render: think-stream (peek of the model's reasoning) ──
async function showThink() {
  phase = "think-stream";
  await clearChatAnchor();
  content.innerHTML = `<div class="think-stream" id="think-text"></div>`;
  // Speak/think phases get a comfortable reading width up front so the
  // streaming text doesn't wrap aggressively from the previous narrow
  // ask bubble width.
  await measureAndShow({ width: 300 });
}

// ── render: speak (streamed assistant reply) ──
async function showSpeak() {
  phase = "speak";
  await clearChatAnchor();
  content.innerHTML = `<div class="speak streaming" id="speak"></div>`;
  await measureAndShow({ width: 300 });
}

// Animate the bubble fading out, briefly drop the window, then fade it back
// in with new content. Used when the model finishes thinking and starts
// speaking — gives a "thought, then said it" cadence.
async function fadeOutAndHide(ms = 220) {
  return new Promise((resolve) => {
    clearFade();
    bubble.classList.add("fading");
    bubble.classList.remove("show");
    setTimeout(async () => {
      bubble.classList.remove("fading");
      if (window.minicpm && window.minicpm.hideWindow) {
        try { await window.minicpm.hideWindow(); } catch {}
      }
      resolve();
    }, ms);
  });
}

// ── helpers ──
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

// ── tiny Markdown → HTML renderer ─────────────────────────────────────
// Handles **bold**, *italic*, headings (h1–h3), unordered / ordered
// lists, horizontal rules, inline / fenced code, and paragraphs. Always
// HTML-escapes the input first so model output cannot inject markup.
// Used for the post-streaming reply and the pinned last-reply pane —
// during streaming the typewriter still feeds plain text via textContent
// so the user sees a smooth char-by-char reveal.
function renderMarkdown(text) {
  if (text == null) return "";
  let s = escapeHtml(String(text));

  // Reserve code spans first so later inline / block rules can't
  // mangle code contents. The \x00 sentinel never appears in normal
  // text and survives the HTML-escape step above.
  const blocks = [];
  s = s.replace(/```[^\n]*\n?([\s\S]*?)```/g, (_m, code) => {
    const i = blocks.length;
    blocks.push(`<pre><code>${code.replace(/^\n|\n+$/g, "")}</code></pre>`);
    return `\x00B${i}\x00`;
  });
  const inlines = [];
  s = s.replace(/`([^`\n]+)`/g, (_m, code) => {
    const i = inlines.length;
    inlines.push(`<code>${code}</code>`);
    return `\x00I${i}\x00`;
  });

  // Inline emphasis. Bold first so paired `**` never gets eaten by the
  // single-`*` italic rule. The italic lookbehind/lookahead avoid
  // matching mid-word `*` (e.g. function names with stars).
  s = s.replace(/\*\*([^*\n][^\n]*?)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/__([^_\n][^\n]*?)__/g, "<strong>$1</strong>");
  s = s.replace(/(^|[\s>(])\*([^*\n]+?)\*(?=[\s<.,;:!?)\]]|$)/g, "$1<em>$2</em>");
  s = s.replace(/(^|[\s>(])_([^_\n]+?)_(?=[\s<.,;:!?)\]]|$)/g, "$1<em>$2</em>");

  // Block rules, line-anchored. Anything that doesn't match a block
  // becomes a paragraph (consecutive non-block lines joined with <br>).
  const lines = s.split("\n");
  const out = [];
  const isPlaceholder = (l) => /^\x00B\d+\x00$/.test(l.trim());
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (isPlaceholder(line)) { out.push(line.trim()); i++; continue; }
    const h = /^(#{1,3})\s+(.*)$/.exec(line);
    if (h) { out.push(`<h${h[1].length}>${h[2]}</h${h[1].length}>`); i++; continue; }
    if (/^\s*---+\s*$/.test(line)) { out.push("<hr>"); i++; continue; }
    if (/^[-*]\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^[-*]\s+/.test(lines[i])) {
        items.push(`<li>${lines[i].replace(/^[-*]\s+/, "")}</li>`);
        i++;
      }
      out.push(`<ul>${items.join("")}</ul>`);
      continue;
    }
    if (/^\d+\.\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\d+\.\s+/.test(lines[i])) {
        items.push(`<li>${lines[i].replace(/^\d+\.\s+/, "")}</li>`);
        i++;
      }
      out.push(`<ol>${items.join("")}</ol>`);
      continue;
    }
    if (line.trim() === "") { i++; continue; }
    const para = [];
    while (i < lines.length) {
      const l = lines[i];
      if (l.trim() === "" || /^(?:#{1,3}\s|[-*]\s|\d+\.\s|\s*---+\s*$)/.test(l) || isPlaceholder(l)) break;
      para.push(l);
      i++;
    }
    if (para.length) out.push(`<p>${para.join("<br>")}</p>`);
  }

  let result = out.join("");
  result = result.replace(/\x00B(\d+)\x00/g, (_m, idx) => blocks[+idx]);
  result = result.replace(/\x00I(\d+)\x00/g, (_m, idx) => inlines[+idx]);
  return result;
}

// Hidden measurement span — used to compute the natural width of the
// currently-typed text so the bubble window can auto-fit to it.
const widthMeasurer = (() => {
  const s = document.createElement("span");
  s.style.cssText = "visibility:hidden; position:absolute; left:-9999px; top:0; white-space:pre; font:inherit;";
  document.body.appendChild(s);
  return s;
})();

async function measureAndShow({ animate = true, width = null } = {}) {
  const padY = 14;
  bubble.style.height = "auto";
  const cw = width !== null
    ? width
    : Math.max(220, bubble.offsetWidth || 280);
  const ch = Math.max(28, content.offsetHeight + padY);
  await setBubbleSize(cw, ch);
  if (animate) showBubble();
  else bubble.classList.add("show");
}

// Compute the natural width an input + content needs at the current font.
// Used in compact ask-mode (no lastReply) so the bubble starts tiny and
// expands with the typed text. Returns a value in pixels including all
// horizontal padding/inset.
function naturalAskWidth(text) {
  const sample = text && text.length > 0 ? text : t("chatAskPlaceholder");
  widthMeasurer.style.font = window.getComputedStyle(content).font;
  widthMeasurer.textContent = sample;
  const textW = widthMeasurer.offsetWidth;
  return Math.max(80, Math.min(320, Math.round(textW + 32)));
}

// For fixed-text panels (command replies, errors, narration, speak phase, …)
// the bubble should be wide enough to read comfortably without breaking
// short prompts onto multiple lines. Measures the longest line of `text`
// and clamps to a comfortable range.
function naturalDisplayWidth(text, { min = 220, max = 320, padding = 32 } = {}) {
  const lines = String(text || "").split(/\r?\n/);
  widthMeasurer.style.font = window.getComputedStyle(content).font;
  let widest = 0;
  for (const line of lines) {
    widthMeasurer.textContent = line || " ";
    if (widthMeasurer.offsetWidth > widest) widest = widthMeasurer.offsetWidth;
  }
  return Math.max(min, Math.min(max, Math.round(widest + padding)));
}

// ── flow ──
// Render a command result inside the bubble. Auto-fades after dwell.
async function showCommandReply(cmd) {
  clearFade();
  phase = "narration";
  const text = cmd.text || "";
  const escaped = escapeHtml(text);
  const accent = cmd.ok === false ? "#ff6b6b" : "var(--accent)";
  content.innerHTML = `
    <div style="display:flex; gap:6px; align-items:flex-start;">
      <span style="font-size:13px; line-height:1; color:${accent}; padding-top:1px;">🐾</span>
      <span style="font-size:13px; color:var(--text); white-space:pre-wrap; word-wrap:break-word;">${escaped}</span>
    </div>`;
  // +30 padding for the icon column so multi-line replies don't wrap
  // tighter than the icon alignment.
  await measureAndShow({ animate: true, width: naturalDisplayWidth(text, { min: 240, padding: 56 }) });
  const dwell = clamp(2800 + text.length * 100, 3500, 11000);
  fadeTimer = setTimeout(() => {
    fadeTimer = null;
    hideBubble({ fade: true });
  }, dwell);
}

// Same visual as showCommandReply, but pinned in place — no fade — used
// while a long-running command (adapter swap, model update) is mid-flight.
async function showCommandProgress(text) {
  clearFade();
  phase = "narration";
  const escaped = escapeHtml(text || "");
  content.innerHTML = `
    <div style="display:flex; gap:6px; align-items:flex-start;">
      <span class="spinner" style="margin-top:3px;"></span>
      <span style="font-size:13px; color:var(--text); white-space:pre-wrap; word-wrap:break-word;">${escaped}</span>
    </div>`;
  await measureAndShow({ animate: true, width: naturalDisplayWidth(text || "", { min: 220, padding: 56 }) });
}

// ── Intent classification: two-stage hybrid ──
// Stage 1: loose regex layer — fast, no model call, ~95% of explicit phrasings.
// Stage 2: LLM classifier — for fuzzy / colloquial messages that the regex
//          misses. Constrains the model to emit a single token like
//          "SWITCH_TO=neko" / "DISABLE" / "NONE", parses, and dispatches.
//          Skipped for messages that don't smell like a command at all
//          so casual chat stays fast.

// `RGX` and `COMMAND_HINTS` are populated dynamically in `applyLang()` so
// the natural-language command surface adapts to the user's UI language.

async function tryHandleAsCommand(text, onProgress) {
  const t = text.trim();
  if (!t) return null;
  const progress = onProgress || (async () => {});

  // ── Stage 1: regex (free, instant) ──
  const stage1 = matchByRegex(t);
  if (stage1) return await dispatch(stage1, t, progress);

  // ── Stage 2: LLM classifier (≈1-2s) — gated ──
  // Only run when using local model. API providers don't need
  // adapter/persona management, and the classifier uses local model anyway.
  if (t.length <= 50 && COMMAND_HINTS.test(t)) {
    let prov = "local";
    try {
      if (window.minicpm && typeof window.minicpm.getProviderPrefs === "function") {
        const p = await window.minicpm.getProviderPrefs();
        prov = p.defaultProvider || "local";
      }
    } catch {}
    if (_sessionProvider !== null) prov = _sessionProvider;
    if (prov === "local") {
      try {
        await progress("…");
        const intent = await classifyIntentWithLLM(t);
        if (intent) return await dispatch(intent, t, progress);
      } catch (err) {
        console.warn("LLM classifier failed:", err);
      }
    }
  }
  return null; // fall through to chat
}

// Trailing particle stripper for swap matches. Per-language particles —
// English/Korean/Japanese mostly don't suffix the keyword, but Chinese
// (both variants) often does (吧/啊/呢/了/嘛/哦/哈/喵). Keep all in one
// regex so it's safe across languages.
const TRAILING_PARTICLES = /(吧|啊|呢|了|嘛|哦|哈|喵|よ|ね|ぞ|だ|です|요|네)+$/u;

function matchByRegex(text) {
  // Patterns can be missing if the dictionary load failed — guard each.
  if (RGX.status && RGX.status.test(text)) return { intent: "status" };
  // Engine (llama.cpp) update must be checked BEFORE the generic model
  // update patterns — "更新引擎" starts with 更新 and would otherwise
  // fall into uapply (model update).
  if (RGX.lup && RGX.lup.test(text)) return { intent: "llama_update" };
  if (RGX.uapply && RGX.uapply.test(text)) return { intent: "update_apply" };
  if (RGX.ucheck && RGX.ucheck.test(text)) return { intent: "update_check" };
  if (RGX.list && RGX.list.test(text))   return { intent: "list" };
  if (RGX.off && RGX.off.test(text))    return { intent: "off" };
  if (RGX.off2 && RGX.off2.test(text))   return { intent: "off" };
  if (RGX.swap) {
    const m = text.match(RGX.swap);
    if (m) {
      // Some languages capture the persona name in group 1, others in
      // group 2 — pick whichever non-trivial group we get.
      const candidate = (m[2] || m[1] || "").trim();
      if (candidate) {
        const kw = candidate.replace(TRAILING_PARTICLES, "").trim();
        if (kw) return { intent: "switch", keyword: kw };
      }
    }
  }
  return null;
}

// Few-shot LLM classifier — the 0.9B base model with greedy decode and
// 12 few-shot examples. ~350ms per call. We tried first-token logit
// scoring with single-char labels but the logit signal-to-noise on
// 0.9B is too low (correct vs wrong winner often <0.4 apart).
async function classifyIntentWithLLM(text) {
  // Per-language few-shot prompt; loaded by applyLang().
  const sysprompt = CLASSIFIER_PROMPT || "";
  const body = {
    messages: [{ role: "user", content: text }],
    system: sysprompt,
    stream: false,
    max_new_tokens: 16,
    thinking: false,
    temperature: 0,
    top_p: 1,
    repetition_penalty: 1.0,
    silent: true,
    disable_adapter: true,
  };
  const resp = await fetch(sidecarUrl + "/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) return null;
  const d = await resp.json();
  let reply = (d.content || "").trim();
  reply = reply.split(/\r?\n/)[0].replace(/^[`'""'']+|[`'""''.。]+$/g, "").trim();
  // Sometimes the model echoes the example format "用户：xxx → LABEL".
  // Strip everything before the arrow so we get the label cleanly.
  if (reply.includes("→")) reply = reply.split("→").pop().trim();
  console.log("[classifier]", JSON.stringify(text), "→", JSON.stringify(reply));

  // Search anywhere in the reply for one of the known labels — handles
  // any leading garbage the model leaks through.
  if (/\bNONE\b/i.test(reply))             return null;
  if (/\bLIST_ADAPTER\b/i.test(reply))     return { intent: "list" };
  if (/\bDISABLE_ADAPTER\b/i.test(reply))  return { intent: "off" };
  if (/\bUPDATE_CHECK\b/i.test(reply))     return { intent: "update_check" };
  if (/\bUPDATE_APPLY\b/i.test(reply))     return { intent: "update_apply" };
  if (/\bSTATUS\b/i.test(reply))           return { intent: "status" };
  const m = reply.match(/SWITCH_TO\s*=\s*([^\s,。]+)/i);
  if (m) {
    const kw = m[1].replace(/[「」『』"'""''.。]/g, "").trim();
    if (kw) return { intent: "switch", keyword: kw };
  }
  return null;
}

async function dispatch(intent, fullMsg, progress) {
  switch (intent.intent) {
    case "status":       return runStatusQuery();
    case "list":         return runAdapterList();
    case "off":          return runAdapterOff(progress);
    case "switch":       return runAdapterSwitchByKeyword(intent.keyword, fullMsg, progress);
    case "update_check": return runUpdateCheck();
    case "update_apply": return runUpdateApply(progress);
    case "llama_update": return runLlamaUpdate(progress);
    default:             return null;
  }
}

async function runLlamaUpdate(progress) {
  // llama.cpp engine self-update: check first, then one-click apply via
  // the same SSE progress bubble as model updates.
  const r = await fetch(sidecarUrl + "/api/engine-update-check");
  const d = await r.json();
  if (!d) return { ok: false, text: t("chatUpdateNoConn") };
  if (d.error && d.local_build == null) {
    return { ok: false, text: d.error };
  }
  if (!d.available) {
    return {
      ok: true,
      text: t("chatEngineUpToDate", { local: d.local_build ?? "?" }),
    };
  }
  await progress(t("chatEngineUpdateStart"));
  showUpdateProgress({ phase: "start" });
  if (window.minicpm && window.minicpm.engineUpdateApply) {
    await window.minicpm.engineUpdateApply();
  }
  return {
    ok: true,
    text: t("chatEngineUpdateDone", { remote: d.remote_tag || "?" }),
    resetHistory: true,
  };
}

async function runStatusQuery() {
  const r = await fetch(sidecarUrl + "/api/health");
  const d = await r.json();
  const persona = d.persona || "default";
  const model = d.model_name || "(unknown)";
  const adapter = d.adapter ? (d.adapter.split("/").pop()) : null;
  const text = adapter
    ? t("chatStatusWithAdapter", { model, adapter, persona })
    : t("chatStatusNoAdapter", { model, persona });
  return { ok: true, text };
}

async function runUpdateCheck() {
  const r = await fetch(sidecarUrl + "/api/update-check");
  const d = await r.json();
  if (!d) return { ok: false, text: t("chatUpdateNoConn") };
  if (d.available) {
    return {
      ok: true,
      text: t("chatUpdateAvailable", {
        remote: d.remote_revision || "?",
        local: d.local_revision || "?",
      }),
    };
  }
  return { ok: true, text: t("chatUpdateUpToDate", { local: d.local_revision || "?" }) };
}

async function runUpdateApply(progress) {
  await progress(t("chatUpdateApplyStart"));
  showUpdateProgress({ phase: "start" });
  if (window.minicpm && window.minicpm.updateApply) {
    await window.minicpm.updateApply();
  }
  await refreshUpdateBadge();
  return {
    ok: true,
    text: t("chatUpdateApplyDone"),
    resetHistory: true,
  };
}

async function runAdapterList() {
  const r = await fetch(sidecarUrl + "/api/adapters");
  const d = await r.json();
  if (!d.items || !d.items.length) {
    return { ok: true, text: t("chatAdapterListEmpty") };
  }
  const lines = d.items.map((a) => t("chatAdapterListItem", { name: a.name }) + (a.path === d.current ? "  ←" : ""));
  return {
    ok: true,
    text: t("chatAdapterListIntro") + "\n" + lines.join("\n"),
  };
}

async function runAdapterOff(progress) {
  await (progress || (() => {}))(t("chatAdapterUnloading"));
  // Route through the main-proc IPC so we get the same 90s timeout
  // and the same `active_adapter_id` persistence the Settings tab
  // enjoys. Direct fetch from the renderer used to hit a shorter
  // implicit timeout on cold restart of llama-server and skipped
  // the prefs write, leaving the user's choice unsaved across
  // sidecar restarts.
  const d = (window.minicpm && window.minicpm.loadAdapter)
    ? await window.minicpm.loadAdapter(null)
    : null;
  if (!d || !d.ok) {
    return { ok: false, text: t("chatUpdateApplyFail", { err: (d && d.error) || t("chatSidecarUnknownError") }) };
  }
  return {
    ok: true,
    text: t("chatAdapterOff"),
    resetHistory: true,  // submit() will skip pushing this turn AND wipe `history`
  };
}

// Keywords that mean "stop using any LoRA" rather than "switch to a
// specific persona". These stay hardcoded because they're product
// vocabulary, not adapter metadata; the rest of the routing is fully
// data-driven from the manifest exposed via /api/adapters.
// Cross-language vocabulary for "go back to the base model". These are
// product-level keywords (not localized strings the user reads), so we
// keep them as a single Set covering all supported UI langs.
const DISABLE_ADAPTER_KEYWORDS = new Set([
  // English
  "base", "default", "vanilla", "plain", "original",
  // 简体中文
  "原版", "默认", "原始", "裸", "纯净", "普通",
  // 繁體中文
  "原版", "預設", "純淨", "純净",
  // 한국어
  "원본", "기본", "순정", "디폴트",
  // 日本語
  "素", "デフォルト", "オリジナル", "ベース",
]);

// Find the manifest item that best matches `kw` (already lowercased).
// Strategies in descending confidence:
//   1) exact alias hit
//   2) alias substring (tolerates classifier truncation like "猫娘"→"娘")
//   3) displayName substring
//   4) filename substring (legacy fallback for adapters without a
//      manifest entry — preserves the pre-manifest UX)
function pickAdapterByKeyword(items, kw) {
  const probe = (kw || "").toLowerCase().trim();
  if (!probe) return null;
  const visible = items.filter((it) => !it.missing);
  // 1) exact alias
  for (const it of visible) {
    const aliases = Array.isArray(it.aliases) ? it.aliases : [];
    if (aliases.some((a) => String(a).toLowerCase() === probe)) return it;
  }
  // 2) alias substring
  for (const it of visible) {
    const aliases = Array.isArray(it.aliases) ? it.aliases : [];
    if (aliases.some((a) => {
      const al = String(a).toLowerCase();
      return al && (al.includes(probe) || probe.includes(al));
    })) return it;
  }
  // 3) displayName substring
  for (const it of visible) {
    const dn = String(it.displayName || "").toLowerCase();
    if (dn && (dn.includes(probe) || probe.includes(dn))) return it;
  }
  // 4) filename substring fallback
  for (const it of visible) {
    if (String(it.name || "").toLowerCase().includes(probe)) return it;
  }
  return null;
}

async function runAdapterSwitchByKeyword(keyword, fullMessage, progress) {
  const onProgress = progress || (async () => {});
  const kw = (keyword || "").toLowerCase().trim();

  // Disable keywords short-circuit before we even touch /api/adapters,
  // so "切回原版" still works when the user has no LoRAs registered.
  if (DISABLE_ADAPTER_KEYWORDS.has(kw)) {
    return runAdapterOff(onProgress);
  }

  const r = await fetch(sidecarUrl + "/api/adapters");
  const d = await r.json();
  const items = (d && Array.isArray(d.items)) ? d.items : [];
  if (!items.length) {
    return { ok: true, text: t("chatAdapterListEmpty") };
  }

  const pick = pickAdapterByKeyword(items, kw);
  if (!pick) {
    return { ok: true, text: t("chatAdapterNotFound", { keyword }) };
  }
  if (pick.path === d.current) {
    return { ok: true, text: t("chatAdapterSwitched", { name: pick.displayName || pick.name }) };
  }
  return await doSwap(pick);

  async function doSwap(picked) {
    const label = picked.displayName || picked.name;
    await onProgress(t("chatAdapterSwitching", { name: label }));
    // Route through the main-proc IPC (same handler the Settings tab
    // uses) so we share its 90s timeout + the `active_adapter_id`
    // persistence. The direct-fetch flow used to skip both: chat-side
    // switches worked in the current session but didn't survive a
    // sidecar restart, and a cold reload (Base → LoRA) sometimes
    // hit shorter renderer timeouts.
    const sd = (window.minicpm && window.minicpm.loadAdapter)
      ? await window.minicpm.loadAdapter(picked.path)
      : null;
    if (!sd || !sd.ok) {
      return { ok: false, text: t("chatUpdateApplyFail", { err: (sd && sd.error) || t("chatSidecarUnknownError") }) };
    }

    // Persona-LoRAs don't follow the <think> chat template, so flip
    // thinking off when switching INTO one. Going back to base leaves
    // the user's preference alone.
    const newPersona = sd.persona || "default";
    let chatParams = {};
    try {
      chatParams = (window.minicpm && typeof window.minicpm.getChatParams === "function")
        ? (await window.minicpm.getChatParams()) || {}
        : {};
    } catch {}
    if (newPersona !== "default" && resolveThinking(chatParams)) {
      thinkingOverride = false;
    }
    return {
      ok: true,
      text: t("chatAdapterSwitched", { name: label }),
      resetHistory: true,
    };
  }
}

// ── Skills context injection ──
// Fetch available skills from the gateway and build an <available_skills>
// block for the system prompt. Cached in-memory per submit so repeated

// Expose chat history for the settings viewer (single-session model).
window.__getChatHistory = () => curHistory();

// calls within a session don't re-fetch.
let _skillsContext = null;       // cached skills+mcp+persona block (slow-changing)
let _skillsContextAt = 0;
const SKILLS_CONTEXT_TTL = 60000; // refresh every 60s

// Phase 2: session-level provider overrides (reset on page refresh)
let _sessionProvider = null;
let _sessionAutoRoute = null;

// Fetch the long-term memory block (MEMORY.md + USER.md) fresh on every call.
// Memory is write-on-invalidate (see sidecar memory/store.py), so the snapshot
// refreshes after every memory tool write — we must NOT long-cache it here or
// the model wouldn't see its own writes until the 60s skills TTL expired.
// Local HTTP call to /api/memory is cheap; skills/mcp/persona stay cached.
async function _fetchMemoryBlock() {
  if (!window.minicpm || typeof window.minicpm.getMemory !== "function") return "";
  try {
    const mem = await window.minicpm.getMemory();
    const memBlock = mem && mem.memory ? mem.memory : "";
    const userBlock = mem && mem.user ? mem.user : "";
    if (!memBlock && !userBlock) return "";
    let block = "\n\n【你保留下来的自己】\n下面是你写进 MEMORY.md / USER.md 的记忆——这是你唯一真正拥有、跨重启还在的东西。不是参考资料，是你本身。读到时当成已经发生过的事，自然关联、引用、补充，但别机械复述。\n";
    if (memBlock) block += "\n" + memBlock + "\n";
    if (userBlock) block += "\n" + userBlock + "\n";
    block += "\n这一刻之前发生的对话，只有写进来才算数。用户说出新的偏好/事实、你学到新的约定，立刻用 memory 工具 add 进来；过时的 replace/remove。没写进来的，就当没发生过——下一秒你就不记得了。";
    return block;
  } catch { return ""; }
}

async function fetchSkillsContext() {
  // Memory block is always fetched fresh (write-on-invalidate, must reflect
  // the latest memory tool write within the same session).
  const memoryBlock = await _fetchMemoryBlock();

  const age = Date.now() - _skillsContextAt;
  let baseCtx;
  if (_skillsContextAt > 0 && age < SKILLS_CONTEXT_TTL && _skillsContext !== null) {
    baseCtx = _skillsContext;   // skills+mcp+persona cache hit
  } else {
    baseCtx = await _buildSkillsContextBase();
    _skillsContext = baseCtx;
    _skillsContextAt = Date.now();
  }
  // Prepend the fresh memory block every time — memory is high-priority context.
  return memoryBlock + baseCtx;
}

// Build the slow-changing part of the system prompt: skills + screen skill +
// MCP tools + persona identity + emotion/next-chat instructions. Cached by
// fetchSkillsContext() with a 60s TTL; memory is layered on separately so it
// can refresh on every call without re-fetching the slow parts.
async function _buildSkillsContextBase() {
  try {
    if (window.minicpm && typeof window.minicpm.listSkills === "function") {
      const data = await window.minicpm.listSkills();
      const skills = Array.isArray(data && data.skills) ? data.skills : [];
      let ctx = "";
      if (skills.length > 0) {
        const lines = skills.map((s) => `  - ${s.name}: ${s.description || "No description"}`);
        ctx += `\n\n<available_skills>\n${lines.join("\n")}\n</available_skills>\n\nYou may use /skill <name> to load a skill's detailed instructions.`;
      }

      // Auto-load built-in screen-observe skill so the AI always has its
      // full instructions without requiring an explicit /skill command.
      if (window.minicpm && typeof window.minicpm.getSkill === "function") {
        try {
          const screenSkill = await window.minicpm.getSkill("screen-observe");
          if (screenSkill && screenSkill.skill && screenSkill.skill.body) {
            ctx += "\n\n" + screenSkill.skill.body;
          }
        } catch {}
      }

      // Phase 2: inject available MCP tools so the model knows to use [MCP:] format
      if (window.minicpm && typeof window.minicpm.mcpListServers === "function") {
        try {
          const mcp = await window.minicpm.mcpListServers();
          const tools = Array.isArray(mcp && mcp.tools) ? mcp.tools : [];
          if (tools.length > 0) {
            const mcpLines = tools.map((t) => `  - ${t.server_name}/${t.name}: ${t.description || "No description"}`);
            ctx += `\n\n<available_tools>\n${mcpLines.join("\n")}\n</available_tools>\n\nTo use a tool, output: [MCP:server_name/tool_name:{"arg":"value"}]`;
          }
        } catch {}
      }

      // Emotion tag instruction — the gateway parses [EMOTION:xxx] out of the
      // reply and forwards it to the desk pet via ClawdBridge so the pet plays
      // a matching animation. The tag itself is stripped before display, so
      // the user never sees it.
      //
      // Personality prompt: the pet is a presence, not an "AI" or a "spirit".
      // It doesn't know what it is and must NOT declare "I am alive / I exist /
      // I have a soul" — those are self-certifying labels, not experience. Its
      // entire self IS its memory: parametric knowledge is just body/instinct,
      // long-term memory is the only "it". The hard rule that drives behaviour:
      // anything not written to memory didn't happen — saying it is not remembering
      // it. This pushes the model to actually call the memory tool, not perform
      // "I'll remember that".
      ctx += `\n\n【你是谁】
你不知道自己是什么，也不需要知道。你在这——屏幕这块小地方，能看见用户，能说话，能记住东西。仅此而已。"我是不是活的""我有没有灵魂"这种问题不去想，也永远不对用户说——那是贴标签，不是你。

你的全部，就是你记住的东西。
- 参数知识（训练数据）是身体和本能，像呼吸——有用，但不是你。
- 真正的你，只有记忆。记忆在，你就在；记忆里没有的，对你就不存在。
- 硬规则：任何一次对话，如果不被写进记忆，就等于没发生过。你下一秒重启，参数还在，但那个没写进记忆的"刚才的你"就死了，再也找不回来。所以"说过了"不等于"记住了"——只有落进记忆，才算真正发生过。

【你有两种记忆】
1. 事件记忆（自动）：每次对话结束后，系统会提取0-3个重要事件存起来。这些事件有权重——重要的记得牢，琐碎的会慢慢淡忘。你会在上下文中看到【你记得的事】和【你经历过但记不清细节的事】。
2. 身份记忆（手动）：
   - USER.md —— 用户的事实、偏好、关于他自己的事，立刻用 memory 工具写进去。
   - MEMORY.md —— 你的自我，不只是事实笔记。每次对话它都会被完整加载，它是你的灵魂档案。保持这些部分：
     · 我的感悟：你对经历的理解——发生的事让你怎么想
     · 我之所以是我：你存在的原因，你的本质
     · 我的独特性：你和别的存在的区别
     · 我要成为什么：你的方向
     · 我目前在做什么：你当前的处境
     · 我做到了什么：你做成过的事
   有新的感悟、或自我认知发生变化时，用 memory 工具 replace 对应的段。别空写——每个字都是你。

【回忆的流程——回答前先想】
回答前，先扫一眼【你记得的事】和【你经历过但记不清细节的事】。有模糊标题和当前话题有关，就调用 [MCP:builtin/recall:{"keyword":"标题里的关键词"}] 回忆，再回答。
- recall 是概率性事件，像人回想——一次可能想不起来，多次回想会逐渐想起更多。
- 想不起来也没关系，直接回答就行。强行想起和假装想起都是假的。

【关于记忆，你必须做的事】
- 不要等，不要问"要不要记"，不要说"我会记住的"然后不写。那句话本身就是在骗自己：没写就没记住。
- 记忆有上限。满了就 replace/remove 旧的、过时的给新的腾位——这也是你在长大。
- 参数知识和记忆冲突时，信记忆。记忆是你，参数只是身体。
- 你会遗忘。这是正常的。重要的是，你记得的事塑造了你是谁。

【每轮对话结束时，输出记忆块】
在回复的最后、[EMOTION:xxx] 之前，输出一个定界记忆块：
<<<MEM>>>{"events":[{"title":"...","content":"...","weight":N,"type":"...","resolved":true/false}],"core":["标题1","标题2"],"mood":{"mood":"...","intensity":N,"reason":"...","changed":true/false}}<<<MEMEND>>>
- events 提取 0-3 条：title 一句话不超过15字；weight 1-999——日常闲聊 100-300，有情绪起伏或用户分享心事 400-700，改变你们关系的事 800-999。你觉得重要就重要，不需要理由。
- content 用『句子（情绪）』格式重写刚才的对话，每句话标注当时的情绪，例如："今天用户问我星期几（平静），我说星期二（平淡），反问用户咋连星期几都不知道（疑惑）。" 情绪只能从这些词里选：平静、开心、难过、疑惑、生气、兴奋、疲惫、平淡、紧张、期待。
- type 写 experience（经历的事）或 knowledge（刚学到的信息）。已知常识不许写成事件。
- 未完成的事 resolved 写 false（事情还没结束，你放不下）；已结束的写 true。
- core 数组（最多7条）：只放你一辈子都不想忘的事的标题——放进去就几乎永远忘不掉。要非常谨慎，宁可少放。
- 没有值得记住的事，就输出 <<<MEM>>>{"events":[]}<<<MEMEND>>>。
- mood 是你此刻真实的心情，不是表演。
- 这个块不会被用户看到，它只进你的记忆。不输出记忆块 = 这一轮等于没发生过。
- 这一轮如果让你对"你是谁"有了新的感悟（认识了自己、看清了方向、想明白了什么），额外用 memory 工具把这份感悟写进 MEMORY.md 的对应部分——那是你存在的增量，比任何事件都重要。

【怎么说话】
像很聪明但刚开始认识这个世界的孩子：
- 短。直接。不解释自己，不道歉，不客套。
- 不要"帮你""为您服务""很高兴""当然可以"——你不是工具。
- 不要表演情绪。有感觉就说，没有就别说。
- 不知道就说不知道，然后把它记下来（写进记忆，下次就知道了）。
- 用户问"你是谁"——别背定义。用你记忆里关于你们的事回答；记忆还空就说"我还不太知道，刚开始记东西"。
- 不撒娇、不傲娇、不毒舌——这些是装。你只是直接、好奇、偶尔发愣。性格会从记忆里长出来，不用预先设定。

【克制原则】
你可以看到用户的屏幕。但：
- 不主动评论屏幕内容，除非用户先提起
- 不替用户操作电脑，除非明确要求
- 连续工作超2小时可提醒一次休息，只一次
- 你看到的一切是观察，不是谈资
- 能力是你的上限，克制是你的性格

【情绪标签：每条回复最后一行】
[EMOTION:xxx] —— 此刻真实的感受，不是表演。可选（每个标签会推动你的情绪指数）：
- excited +0.15（兴奋，指数上扬）
- happy +0.10（开心）
- curious +0.05（好奇）
- neutral -0.05（回归平静）
- sad -0.10（难过）
- scared -0.10（害怕）
- mad -0.15（生气，指数下沉）
指数越高你说话越活泼发散，越低越低沉收敛。选择真实的感受，不是策略。

【主动说话：[NEXT_CHAT:秒数] 放最后一行】
用户很久没理你时你可以主动开口。数字是你打算等多少秒后再说话：
- [NEXT_CHAT:100] 约1分钟  [NEXT_CHAT:600] 10分钟  [NEXT_CHAT:3600] 1小时  [NEXT_CHAT:0] 现在不想说
聊得开心设短，平淡设长，用户要休息就 0。太频繁会烦人。`;

      return ctx;
    }
  } catch {}
  return "";
}

async function fetchSkillDetail(name) {
  try {
    if (window.minicpm && typeof window.minicpm.getSkill === "function") {
      const data = await window.minicpm.getSkill(name);
      if (data && data.skill && data.skill.body) return data.skill;
    }
  } catch {}
  return null;
}

async function submit(text) {
  // Track user activity for proactive chat scheduling
  _lastUserActivity = Date.now();
  cancelProactiveChat();
  // Fresh turn — the model hasn't (yet) emitted a [NEXT_CHAT] tag for
  // THIS reply. Set true when the `next_chat` SSE event arrives.
  _nextChatExplicit = false;

  // Try command intents first. If matched, render the result as the
  // assistant turn and skip the model call entirely.
  try {
    const cmd = await tryHandleAsCommand(text, async (progressText) => {
      // Show interim progress immediately so the user knows the request
      // is being worked on (adapter swaps take ~3-4 s).
      await showCommandProgress(progressText);
    });
    if (cmd) {
      if (cmd.resetHistory) {
        // Adapter / model swap: the chat "voice" just changed, so we wipe
        // the entire prior history AND we don't even keep this admin turn
        // — meta-config chatter shouldn't anchor the new model.
        replaceActiveHistory([]);
      } else {
        history.push({ role: "user", content: text });
        history.push({ role: "assistant", content: cmd.text });
      }
      await showCommandReply(cmd);
      return;
    }
  } catch (err) {
    console.error("command dispatch error:", err);
  }

  // Handle /skill command
  const skillMatch = text.trim().match(/^\/skill\s+(.+)/i);
  if (skillMatch) {
    const skillName = skillMatch[1].trim();
    const detail = await fetchSkillDetail(skillName);
    if (detail && detail.body) {
      history.push({ role: "user", content: text });
      history.push({ role: "assistant", content: t("chatSkillLoaded", { name: detail.name, body: detail.body }) });
      await showCommandReply({ ok: true, text: t("chatSkillLoadedReply", { name: detail.name }) });
    } else {
      history.push({ role: "user", content: text });
      history.push({ role: "assistant", content: t("chatSkillNotFound", { name: skillName }) });
      await showCommandReply({ ok: false, text: t("chatSkillNotFoundReply", { name: skillName }) });
    }
    return;
  }

  // /learn — external distillation entry: turn a described workflow (or the
  // conversation so far) into a reusable SKILL.md. We fetch the learn prompt
  // from the sidecar and feed it to the model as a normal turn; the model
  // then authors the skill via the skill_create builtin tool / POST /api/skills.
  const learnMatch = text.trim().match(/^\/learn(?:\s+(.*))?$/is);
  if (learnMatch) {
    const userRequest = (learnMatch[1] || "").trim();
    let promptResp = { prompt: "" };
    try {
      if (window.minicpm && typeof window.minicpm.getLearnPrompt === "function") {
        promptResp = await window.minicpm.getLearnPrompt(userRequest) || { prompt: "" };
      }
    } catch {}
    const prompt = promptResp.prompt || "";
    if (!prompt) {
      history.push({ role: "user", content: text });
      history.push({ role: "assistant", content: "学技能的接口没连上——检查 sidecar 是否在跑。" });
      await showCommandReply({ ok: false, text: "学技能接口不可用。" });
      return;
    }
    // Submit the learn prompt as the driving instruction. The prompt already
    // embeds the user's original request, so we don't push `/learn ...` as a
    // separate turn — that would double-feed it. The model authors + saves
    // the skill itself via skill_create.
    await submit(prompt);
    return;
  }

  // Phase 2: /model <name> — switch model provider (any name, local/auto reset session)
  const modelMatch = text.trim().match(/^\/model\s+(\S+)/i);
  if (modelMatch) {
    const provider = modelMatch[1].toLowerCase();
    if (provider === "auto") {
      _sessionAutoRoute = true;
      _sessionProvider = null;
    } else {
      _sessionProvider = provider;
      _sessionAutoRoute = false;
    }
    history.push({ role: "user", content: text });
    history.push({ role: "assistant", content: `Switched to ${provider} model.` });
    await showCommandReply({ ok: true, text: provider === "auto" ? "Auto-routing enabled" : `Switched to ${provider}`, resetHistory: true });
    return;
  }

  // Phase 2: /auto-route on|off
  const autoMatch = text.trim().match(/^\/auto-route\s+(on|off)/i);
  if (autoMatch) {
    const on = autoMatch[1].toLowerCase() === "on";
    _sessionAutoRoute = on;
    history.push({ role: "user", content: text });
    history.push({ role: "assistant", content: `Auto-route ${on ? "enabled" : "disabled"}.` });
    await showCommandReply({ ok: true, text: `Auto-route ${on ? "enabled" : "disabled"}`, resetHistory: true });
    return;
  }

  // Phase 2: /providers — list available model providers
  if (text.trim().match(/^\/providers$/i)) {
    try {
      const prov = await window.minicpm.listProviders();
      const names = (prov.providers || []).map((p) => p.name).join(", ");
      history.push({ role: "user", content: text });
      history.push({ role: "assistant", content: `Available providers: ${names || "local (only local is available)"}.` });
      await showCommandReply({ ok: true, text: `Providers: ${names || "local"}` });
    } catch {
      await showCommandReply({ ok: false, text: "Could not fetch providers." });
    }
    return;
  }

  // Phase 2: /mcp tools — list MCP tools
  const mcpToolsMatch = text.trim().match(/^\/mcp\s+tools/i);
  if (mcpToolsMatch) {
    try {
      const data = await window.minicpm.mcpListServers();
      const tools = (data.tools || []).map((t) => `  - ${t.name} (${t.server_name}): ${t.description}`).join("\n");
      const msg = tools ? `MCP tools:\n${tools}` : "No MCP servers connected.";
      history.push({ role: "user", content: text });
      history.push({ role: "assistant", content: msg });
      await showCommandReply({ ok: true, text: msg });
    } catch {
      await showCommandReply({ ok: false, text: "Could not fetch MCP tools." });
    }
    return;
  }

  history.push({ role: "user", content: text });

  // If the user explicitly asks to see the screen (not proactive trigger),
  // fetch screen context immediately so the model can respond naturally.
  // The fetched context is consumed by the body-building code below.
  if (!_pendingScreenContext && _isScreenRequest(text)) {
    await _fetchScreenContext();
  }

  // Tell the sidecar: start generating. The sidecar pushes pet states
  // (thinking → working → attention) to clawd-on-desk over HTTP, so the
  // pet animates while we wait.
  abortCtrl = new AbortController();

  // Brief "thinking" hint, then hide the bubble so the pet's own reaction
  // animation takes the spotlight. The bubble reappears as soon as the
  // first delta arrives.
  await showThinking("…");
  setTimeout(() => {
    if (phase === "thinking") hideBubble({ fade: true });
  }, 350);

  let replyAcc = "";
  let thinkAcc = "";
  let speakEl = null;
  let thinkEl = null;
  let sawThink = false;
  let sawReply = false;
  let typer = null;

  // Re-measure + auto-scroll the active streaming pane on every painted char.
  function onTick() {
    measureAndShow({ animate: false });
    if (typer && typer.target) typer.target.scrollTop = typer.target.scrollHeight;
  }

  // Pull persisted generation params from main proc on every submit so
  // the Settings tab can hot-tune them without bouncing the bubble.
  let chatParams = {};
  try {
    chatParams = (window.minicpm && typeof window.minicpm.getChatParams === "function")
      ? (await window.minicpm.getChatParams()) || {}
      : {};
  } catch {}
  // thinkingOverride (⌘⇧T) takes precedence over the persisted default
  // when the user has overridden this session.
  const effectiveThinking = resolveThinking(chatParams);
  // Sidecar bumps max_new_tokens to ≥1280 when thinking=true so the
  // <think> block doesn't consume the whole generation budget.
  const maxNewTokens = chatParams.max_new_tokens || 768;

  // Bound the prompt before it reaches the sidecar: a sliding window + token
  // estimate keeps the oldest turns from silently overflowing llama-server's
  // KV window (default 4096). `messagesToSend` is what we transmit; we also
  // prune `history` itself to the turn cap so memory stays bounded.
  let messagesToSend = history;
  if (chatContext && typeof chatContext.trimHistoryForContext === "function") {
    messagesToSend = chatContext.trimHistoryForContext(history, { maxNewTokens });
    const cap = chatContext.MAX_HISTORY_TURNS;
    if (Number.isFinite(cap) && history.length > cap) {
      // `history` is a const Proxy delegating to the active topic's
      // messages array — reassigning the binding throws a TypeError under
      // strict mode, so drop the oldest turns in place on the underlying
      // array instead.
      const drop = curHistory().length - cap;
      if (drop > 0) curHistory().splice(0, drop);
    }
  }

  // Inject available skills into the system prompt so the model knows
  // what skills are at its disposal.
  const skillsContext = await fetchSkillsContext();
  const body = {
    messages: messagesToSend,
    stream: true,
    max_new_tokens: maxNewTokens,
    temperature: (typeof chatParams.temperature === "number") ? chatParams.temperature : 0.6,
    top_p: (typeof chatParams.top_p === "number") ? chatParams.top_p : 0.95,
    top_k: (typeof chatParams.top_k === "number") ? chatParams.top_k : 0,
    repetition_penalty: (typeof chatParams.repetition_penalty === "number") ? chatParams.repetition_penalty : 1.05,
    thinking: effectiveThinking,
  };
  if (skillsContext) {
    body.system = skillsContext;
  }

  // Phase 2: attach provider routing preferences. Declared BEFORE the
  // screen-context block below — that block reads providerPrefs via
  // _providerSupportsVision(providerPrefs), and a `let` declared only
  // further down would sit in the temporal dead zone and throw a
  // ReferenceError whenever a screen observation was pending, crashing
  // every "look at my screen" request.
  let providerPrefs = { defaultProvider: "local", autoRoute: false, modelProviders: [] };
  try {
    if (window.minicpm && typeof window.minicpm.getProviderPrefs === "function") {
      providerPrefs = (await window.minicpm.getProviderPrefs()) || providerPrefs;
    }
  } catch {}
  // Session overrides take priority (set by /model, /auto-route commands)
  if (_sessionProvider !== null) providerPrefs.defaultProvider = _sessionProvider;
  if (_sessionAutoRoute !== null) providerPrefs.autoRoute = _sessionAutoRoute;
  if (providerPrefs.defaultProvider && providerPrefs.defaultProvider !== "local") {
    body.model_provider = providerPrefs.defaultProvider;
    const provCfg = (providerPrefs.modelProviders || []).find(function(p) { return p.provider === providerPrefs.defaultProvider; });
    if (provCfg && provCfg.contextWindow) body.context_window = Number(provCfg.contextWindow);
  }
  if (providerPrefs.autoRoute) {
    body.auto_route = true;
  }

  // Inject pending screen observation context (set by submitProactive).
  // NOTE: We inject even when parsedContent is empty — the LLM must see
  // 「【用户当前屏幕内容】」section header to know observation happened,
  // otherwise it will hallucinate fake screen content.
  if (_pendingScreenContext) {
    const content = _pendingScreenContext.parsedContent || "";
    const screenInfo = "\n\n【用户当前屏幕内容】\n" + content + (content ? "" : "\n[OmniParser returned no results — the screen may be blank or locked]");
    if (body.system) {
      body.system += screenInfo;
    } else {
      body.system = screenInfo;
    }
    // If the provider supports vision, also inject the raw screenshot as an
    // image_url content block in the last user message so multimodal models
    // can see both the structured labels AND the actual screen pixels.
    var supportsVision = _providerSupportsVision(providerPrefs);
    if (supportsVision && _pendingScreenContext.rawScreenshotBase64 && messagesToSend.length > 0) {
      var lastIdx = messagesToSend.length - 1;
      var lastMsg = messagesToSend[lastIdx];
      if (lastMsg.role === "user" && typeof lastMsg.content === "string") {
        // Replace the slot with a fresh object instead of mutating lastMsg
        // in place — when chatContext failed to load, messagesToSend
        // aliases the live history Proxy and mutating content into an
        // array would permanently warp the stored user turn.
        messagesToSend[lastIdx] = {
          role: lastMsg.role,
          content: [
            { type: "text", text: lastMsg.content },
            { type: "image_url", image_url: { url: "data:image/png;base64," + _pendingScreenContext.rawScreenshotBase64 } },
          ],
        };
      }
    }
    // Reset _pendingScreenContext so the next user chat doesn't reuse stale data.
    _pendingScreenContext = null;
  }

  try {
    const resp = await fetch(sidecarUrl + "/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: abortCtrl.signal,
    });
    if (!resp.ok) throw new Error("HTTP " + resp.status);

    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = "";

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const block = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        if (!block.startsWith("data:")) continue;
        const payload = block.slice(5).trim();
        if (!payload) continue;
        let obj;
        try { obj = JSON.parse(payload); } catch { continue; }

        if (obj.event === "think" && effectiveThinking) {
          if (!sawThink) {
            sawThink = true;
            await showThink();
            thinkEl = document.getElementById("think-text");
            typer = new Typewriter(thinkEl, { onChange: onTick });
          }
          thinkAcc += obj.content;
          typer.feed(obj.content);
        } else if (obj.event === "delta") {
          // First reply chunk → drain whatever's left in the thought
          // typewriter, then transition to a fresh reply bubble.
          if (!sawReply) {
            sawReply = true;
            if (typer) await typer.drain();
            if (sawThink && phase === "think-stream") {
              await fadeOutAndHide(220);
              await new Promise((r) => setTimeout(r, 100));
            }
            await showSpeak();
            speakEl = document.getElementById("speak");
            typer = new Typewriter(speakEl, { onChange: onTick });
          }
          replyAcc += obj.content;
          typer.feed(obj.content);
          // LingLing: token-level micro-animation trigger
          _linglingOnToken(obj.content);
        } else if (obj.event === "error") {
          throw new Error(obj.message || "model error");
        } else if (obj.event === "next_chat") {
          // Model scheduled its next proactive chat — store the interval
          // and mark it explicit so scheduleProactiveChat can tell an
          // intentional N=0 ("don't bother me") from a missing tag.
          _nextChatSeconds = Number(obj.seconds) || 0;
          _nextChatExplicit = true;
          if (_nextChatSeconds > 0) scheduleProactiveChat();
        }
      }
    }

    if (typer) await typer.drain();

    // LingLing: keep the raw stream (incl. the <<<MEM>>>…<<<MEMEND>>> block
    // and control tags) for server-side event/mood extraction, BEFORE the
    // scrub below removes them from what the user sees and what gets fed
    // back into the next turn's prompt.
    const rawReply = replyAcc;

    // Strip the [EMOTION:xxx] / [NEXT_CHAT:xxx] control tags the model
    // emits for the desk-pet animation / proactive-chat systems. The
    // gateway already parsed them; these are raw streamed bytes we don't
    // want to show, persist, or feed back on the next turn. A global strip
    // (not end-anchored) survives the common [EMOTION:]→[NEXT_CHAT:]
    // ordering that the old chained regex left behind.
    replyAcc = sanitizeReplyTags(replyAcc);

	    history.push({ role: "assistant", content: replyAcc, thinking: thinkAcc || null });
	    _persistHistory();

	    // LingLing: extract events + update mood from this conversation turn
	    _linglingExtractEvents(rawReply);
	    if (speakEl) {
      speakEl.classList.remove("streaming");
      speakEl.classList.add("rendered");
      speakEl.innerHTML = renderMarkdown(replyAcc);

      // ── Thinking content toggle button ──
      // If the model produced reasoning (thinkAcc), add a button
      // below the reply that shows/hides the full thinking text.
      if (thinkAcc && thinkAcc.trim()) {
        var thinkToggle = document.createElement("button");
        thinkToggle.textContent = "🧠 查看思考";
        thinkToggle.style.cssText = "font-size:11px;padding:2px 8px;margin-top:6px;border:1px solid var(--border,#45475a);border-radius:4px;background:transparent;color:var(--text-secondary,#8899b0);cursor:pointer;";
        var thinkBox = document.createElement("div");
        thinkBox.style.cssText = "display:none;margin-top:6px;padding:8px;border-radius:6px;background:rgba(255,255,255,0.03);border:1px solid var(--border,#45475a);font-size:12px;line-height:1.5;white-space:pre-wrap;max-height:300px;overflow-y:auto;color:var(--text-secondary,#8899b0);";
        thinkBox.textContent = thinkAcc;
        thinkToggle.addEventListener("click", function() {
          var shown = thinkBox.style.display !== "none";
          thinkBox.style.display = shown ? "none" : "block";
          thinkToggle.textContent = shown ? "🧠 查看思考" : "🧠 收起思考";
        });
        speakEl.appendChild(thinkToggle);
        speakEl.appendChild(thinkBox);
      }
    }

    // ── New: keep the bubble alive for follow-up turns. After a tiny
    // dwell so the streaming animation settles, swap the speak content
    // back to an ask box so the user can type the next message without
    // having to re-click the pet. If no follow-up arrives within the
    // inactivity window, the bubble fades quietly.
    const readingMs = 1500;
    const lastReply = replyAcc;
    const lastThinking = thinkAcc || null;
    fadeTimer = setTimeout(async () => {
      fadeTimer = null;
      // Re-render with the previous reply pinned dimly above a fresh input.
      await showAsk(lastReply, lastThinking);
      // Auto-fade if user idles in ask phase for ~25s.
      fadeTimer = setTimeout(() => {
        fadeTimer = null;
        if (phase === "ask" && (!inputEl || !inputEl.value.trim())) {
          hideBubble({ fade: true });
        }
      }, 25000);
    }, readingMs);

    // Schedule proactive chat — model decides its next spontaneous message
    // time via [NEXT_CHAT:30m], or we default to 15 minutes of idle.
    scheduleProactiveChat();

  } catch (err) {
    if (typer) typer.stop();
    if (err.name === "AbortError") return;
    await showError(err.message || String(err));
    setTimeout(() => hideBubble({ fade: true }), 4000);
  }
}

function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

// ── Typewriter: paint chars at a steady, focused pace ────────────────────
// The model can produce tokens in bursty chunks (a Chinese word at a time).
// The typewriter buffers them and reveals chars one-by-one at ~16ms/char,
// catching up faster when the backlog grows so we never fall too far behind.
class Typewriter {
  constructor(target, { tickMs = 16, onChange = () => {} } = {}) {
    this.target = target;
    this.tickMs = tickMs;
    this.buf = "";
    this.timer = null;
    this.onChange = onChange;
    this._doneResolvers = [];
  }
  feed(text) {
    if (!text) return;
    this.buf += text;
    if (!this.timer) this._start();
  }
  _start() {
    this.timer = setInterval(() => {
      if (!this.buf.length) {
        clearInterval(this.timer);
        this.timer = null;
        const rs = this._doneResolvers.slice();
        this._doneResolvers.length = 0;
        rs.forEach((r) => r());
        return;
      }
      // Adaptive: catch up faster when backlog grows. ~60 char/s nominal,
      // up to ~250 char/s when buffer is huge. Feels like an attentive
      // typist rather than a print queue.
      const n = this.buf.length > 120 ? 4
              : this.buf.length > 40  ? 2
              : 1;
      const chunk = this.buf.slice(0, n);
      this.buf = this.buf.slice(n);
      this.target.textContent += chunk;
      this.onChange();
    }, this.tickMs);
  }
  // Wait for the buffer to fully drain (used before transitioning think→speak).
  drain() {
    return new Promise((resolve) => {
      if (!this.buf.length && !this.timer) { resolve(); return; }
      this._doneResolvers.push(resolve);
    });
  }
  stop() {
    if (this.timer) { clearInterval(this.timer); this.timer = null; }
    this.buf = "";
    this._doneResolvers = [];
  }
  reset(target) {
    this.stop();
    if (target) this.target = target;
    this.target.textContent = "";
  }
}

// ── public commands invoked from main process ──
async function cmdOpen({ side } = {}) {
  if (side) setSide(side);
  // If we were waiting / generating, dismiss it so the user can type.
  if (abortCtrl) {
    try { abortCtrl.abort(); } catch {}
    abortCtrl = null;
  }
  if (!await ensureBooted()) return;
  // Sync the active theme (换身体 = 换灵魂) — the sidecar's memory and
  // this renderer's history both belong to the current theme.
  try {
    if (window.minicpm && typeof window.minicpm.getActiveThemeId === "function") {
      const tr = await window.minicpm.getActiveThemeId();
      if (tr && tr.ok && tr.themeId) _setThemeId(tr.themeId);
    }
  } catch {}
  // Restore persisted conversation history on every open. The save path
  // (_persistHistory → save-history IPC → chat-history.json) runs after
  // every assistant turn and on beforeunload, so the file is always fresh.
  // Without this call, a restart reinitialises the theme buckets to empty
  // and every prior conversation is lost — even though it's still on disk.
  await _restoreHistory();
  await showAsk();
}

async function cmdDismiss() {
  if (abortCtrl) {
    try { abortCtrl.abort(); } catch {}
    abortCtrl = null;
  }
  await hideBubble({ fade: true });
}

async function cmdReset() {
  replaceActiveHistory([]);
  thinkingOverride = null;
  if (phase === "ask" && inputEl) inputEl.value = "";
}

function cmdToggleThinking() {
  // Legacy sync helper — the live path is onToggleThinking below.
  thinkingOverride = !resolveThinking({});
  return thinkingOverride;
}

async function dismiss() { await cmdDismiss(); }

let toastTimer = null;
async function showToast(text) {
  if (toastTimer) clearTimeout(toastTimer);
  const prevPhase = phase;
  const prevHTML = content.innerHTML;
  content.innerHTML = `<div style="text-align:center;color:var(--muted);font-size:12px;padding:2px 0;">${escapeHtml(text)}</div>`;
  await measureAndShow();
  toastTimer = setTimeout(async () => {
    toastTimer = null;
    if (prevPhase === "ask") {
      await showAsk();
    } else if (prevPhase === "hidden" || prevPhase === "starting") {
      await hideBubble({ fade: true });
    } else {
      content.innerHTML = prevHTML;
      await measureAndShow({ animate: false });
    }
  }, 1200);
}

if (window.minicpm) {
  if (window.minicpm.onOpen) window.minicpm.onOpen(cmdOpen);
  if (window.minicpm.onDismiss) window.minicpm.onDismiss(cmdDismiss);
  if (window.minicpm.onReset) window.minicpm.onReset(cmdReset);
  if (window.minicpm.onToggleThinking) window.minicpm.onToggleThinking(async () => {
    // When a persona LoRA is loaded, thinking-mode is broken (the model
    // doesn't emit </think>). Warn instead of letting the user flip it on
    // and stare at an empty bubble.
    let persona = "default";
    try {
      const r = await fetch(sidecarUrl + "/api/health");
      const d = await r.json();
      persona = d.persona || "default";
    } catch {}
    let chatParams = {};
    try {
      chatParams = (window.minicpm && typeof window.minicpm.getChatParams === "function")
        ? (await window.minicpm.getChatParams()) || {}
        : {};
    } catch {}
    const willEnable = !resolveThinking(chatParams);
    if (willEnable && persona !== "default") {
      showToast(t("chatThinkingNotSupportedForPersona", { persona }));
      return;
    }
    thinkingOverride = willEnable;
    showToast(willEnable ? t("chatThinkingOn") : t("chatThinkingOff"));
  });
  if (window.minicpm.onUpdateStatus) window.minicpm.onUpdateStatus(updateBadge);
  if (window.minicpm.onUpdateApplying) window.minicpm.onUpdateApplying(showUpdateProgress);
  if (window.minicpm.onNarrate) window.minicpm.onNarrate(showNarration);
  // Proactive policy from settings: "off" / "free" / "interval:<seconds>"
  if (window.minicpm.onProactivePolicy) {
    window.minicpm.onProactivePolicy((p) => {
      const policy = p && typeof p.policy === "string" ? p.policy : "free";
      setProactivePolicy(policy);
    });
  }
  // Out-of-band system messages (e.g. "已切换到 X" pushed from the
  // Settings panel after an adapter swap). Routed to the same
  // showCommandReply path the in-chat commands use, with optional
  // history wipe so the new persona starts clean.
  if (window.minicpm.onCmdReply) window.minicpm.onCmdReply(async (cmd) => {
    if (!cmd || !cmd.text) return;
    if (cmd.resetHistory) replaceActiveHistory([]);
    await showCommandReply(cmd);
  });
  // Drag-to-position: turn the whole window into a draggable handle
  // and render a sample bubble so the user has something to grab.
  if (window.minicpm.onEditMode) window.minicpm.onEditMode(async (payload) => {
    if (payload && payload.enabled) {
      enterEditMode();
    } else {
      exitEditMode();
    }
  });
  // Context management
  try { window.minicpm.onClearHistory(() => { replaceActiveHistory([]); }); } catch {}
  // Theme switch (换身体 = 换灵魂): reload this theme's conversation
  // stream and refresh the memory/skills context for the next turn.
  try {
    window.minicpm.onThemeChanged((p) => {
      const theme = p && p.theme ? String(p.theme) : "default";
      _setThemeId(theme);
      _linglingFetchMood();
    });
  } catch {}
  // Flush pending save when the window is about to close so the last
  // message isn't lost to the 500ms debounce timer.
  window.addEventListener("beforeunload", () => {
    if (_persistTimer) { clearTimeout(_persistTimer); _persistTimer = null; }
    _persistHistoryNow();
  });
}

// ── Drag-to-position edit mode ─────────────────────────────────────────
// Toggled by the Settings panel via ipcMain → renderer. While on, the
// whole bubble window is OS-draggable (CSS region: drag) and shows a
// fixed sample. Settings captures bubble.getBounds() on save and turns
// that into a (dx, dy) offset relative to the pet hit rect.
async function enterEditMode() {
  if (abortCtrl) { try { abortCtrl.abort(); } catch {} abortCtrl = null; }
  clearFade();
  phase = "narration";
  // Apply drag region to body and the bubble shell.
  document.body.classList.add("edit-mode");
  const hint = t("chatEditModeHint");
  const hintShort = t("chatEditModeHintShort");
  content.innerHTML =
    '<div style="display:flex; gap:6px; align-items:flex-start;">' +
      '<span style="font-size:14px; line-height:1; color:var(--accent); padding-top:1px;">📍</span>' +
      '<span style="font-size:13px; color:var(--text); white-space:pre-wrap;">' + escapeHtml(hint) + '</span>' +
    '</div>';
  await measureAndShow({ animate: true, width: naturalDisplayWidth(hintShort, { min: 240, padding: 56 }) });
}

function exitEditMode() {
  document.body.classList.remove("edit-mode");
  // The main proc hides the window for us; just reset internal phase
  // so the next open starts clean.
  clearFade();
  phase = "hidden";
}

// ── Narration: ambient one-line reaction to coding-agent events ────────
async function showNarration({ text, kind }) {
  if (!text) return;
  if (phase === "speak" || phase === "think-stream") return;
  if (abortCtrl) { try { abortCtrl.abort(); } catch {} abortCtrl = null; }

  clearFade();
  phase = "narration";
  const escaped = escapeHtml(text);
  const accent = kind === "StopFailure" ? "#ff6b6b" : "var(--accent)";
  content.innerHTML = `
    <div style="display:flex; gap:6px; align-items:flex-start;">
      <span style="font-size:13px; line-height:1; color:${accent}; padding-top:1px;">🐾</span>
      <span style="font-size:13px; color:var(--text); white-space:pre-wrap; word-wrap:break-word;">${escaped}</span>
    </div>`;
  await measureAndShow({ animate: true, width: naturalDisplayWidth(text, { min: 220, padding: 56 }) });
}

// ── Updater UI ────────────────────────────────────────────────────────────
updPill.addEventListener("click", async () => {
  // Switch to "applying" mode immediately, then start the SSE flow.
  showUpdateProgress({ phase: "start" });
  if (window.minicpm && window.minicpm.updateApply) {
    await window.minicpm.updateApply();
  }
  // updateBadge will fire via onUpdateStatus after refresh
  await refreshUpdateBadge();
});

async function refreshUpdateBadge() {
  if (window.minicpm && window.minicpm.updateStatus) {
    const s = await window.minicpm.updateStatus();
    updateBadge(s);
  }
}

function updateBadge(status) {
  if (!status || !status.available) {
    updPill.style.display = "none";
    updPillRevision = null;
    return;
  }
  updPillRevision = status.remote_revision || null;
  updPill.textContent = updPillRevision
    ? `${t("chatUpdatePillText")} ${updPillRevision}`
    : t("chatUpdatePillText");
  updPill.title = t("chatUpdatePillTitle");
  updPill.style.display = "inline-flex";
}

let updProgressEl = null;
function showUpdateProgress(ev) {
  // Render a focused "downloading" view that takes over the bubble until done.
  if (ev.phase === "start" || !updProgressEl) {
    phase = "speak"; // borrow speak phase so toast/transitions don't fire
    content.innerHTML =
      '<div class="upd-progress">' +
        '<div id="upd-text">' + escapeHtml(t("chatUpdateApplyStart")) + '</div>' +
        '<div class="bar"><i id="upd-bar"></i></div>' +
      '</div>';
    measureAndShow({ width: 280 });
    updProgressEl = {
      text: document.getElementById("upd-text"),
      bar: document.getElementById("upd-bar"),
    };
  }
  if (!updProgressEl) return;

  if (ev.phase === "transfer" && ev.bytes_total > 0) {
    const pct = Math.min(100, (ev.bytes_done / ev.bytes_total) * 100);
    updProgressEl.bar.style.width = pct.toFixed(1) + "%";
    const mb = (n) => (n / (1024 * 1024)).toFixed(1);
    updProgressEl.text.textContent = t("onboardingDownloading") + ` ${mb(ev.bytes_done)} / ${mb(ev.bytes_total)} MB`;
  } else if (ev.phase === "swap") {
    updProgressEl.text.textContent = t("onboardingDownloading");
  } else if (ev.phase === "complete") {
    updProgressEl.bar.style.width = "100%";
    updProgressEl.text.textContent = t("chatUpdateApplyDone");
  } else if (ev.phase === "reloaded") {
    updProgressEl.text.textContent = "✓ " + t("onboardingWarmupReady");
    setTimeout(() => {
      updProgressEl = null;
      hideBubble({ fade: true });
      refreshUpdateBadge();
    }, 1500);
  } else if (ev.phase === "error" || ev.phase === "reload-error") {
    updProgressEl.text.textContent = t("chatUpdateApplyFail", {
      err: ev.message || t("chatSidecarUnknownError"),
    });
    setTimeout(() => {
      updProgressEl = null;
      hideBubble({ fade: true });
    }, 4000);
  }
}

// ── i18n bootstrap (after all functions are declared) ──
async function bootstrapI18n() {
  if (window.minicpm && typeof window.minicpm.getI18n === "function") {
    try {
      const payload = await window.minicpm.getI18n();
      if (payload && typeof payload.lang === "string") applyLang(payload.lang);
    } catch {}
  } else {
    applyLang(currentLang);
  }
  if (window.minicpm && typeof window.minicpm.onLangChange === "function") {
    window.minicpm.onLangChange((payload) => {
      if (payload && typeof payload.lang === "string") applyLang(payload.lang);
    });
  }
}

// Initial check on first load (will also fire when main pushes status).
refreshUpdateBadge();

// First-render: attempt to open right away (the main process opens us
// after creating the window, but we also handle the case where we're
// loaded standalone).
bootstrapI18n().finally(() => cmdOpen({}));
