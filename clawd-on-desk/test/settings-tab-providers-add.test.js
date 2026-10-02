"use strict";

const { test, describe } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const SRC = path.join(__dirname, "..", "src", "settings-tab-providers.js");
const I18N_SRC = path.join(__dirname, "..", "src", "settings-i18n.js");

// ── Regression guards for the 添加供应商 (add provider) pane ──────────
// The captain's live bug: clicking "+ 添加供应商" in cloud mode rendered
// only up to the 云厂商 heading and stopped — mkPreset was declared inside
// the local-only branch, so the cloud preset loop threw and the rest of
// the pane (manual form, add button) never mounted. These tests EXECUTE
// the real render pipeline (with a minimal DOM stub) along the exact
// click path, so a scope or render-order break fails loudly instead of
// dying silently in the renderer console.

// Minimal DOM: just enough of Element for the providers tab to render.
function makeText(txt) {
  const n = new StubNode("#text");
  n._textValue = txt;
  return n;
}

class StubNode {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.attrs = {};
    this.style = {};
    this._cls = "";
    this.disabled = false;
    this.isConnected = true;
    this._listeners = {};
  }
  get className() { return this._cls; }
  set className(v) { this._cls = String(v); }
  get classList() {
    const self = this;
    return {
      contains(c) { return self._cls.split(/\s+/).includes(c); },
      add(...cs) { for (const c of cs) if (!self.classList.contains(c)) self._cls = (self._cls + " " + c).trim(); },
      toggle(c, on) { self._cls = self._cls.split(/\s+/).filter((x) => x && x !== c).concat(on ? [c] : []).join(" "); },
    };
  }
  set innerHTML(v) { if (v === "") this.children.length = 0; else this._html = v; }
  get innerHTML() { return this._html || ""; }
  set textContent(v) {
    // Standard DOM: assigning text replaces the subtree with one text node
    // (softBtn labels rely on this).
    this.children.length = 0;
    if (v !== "") this.children.push(makeText(String(v)));
  }
  get textContent() {
    if (this.tagName === "#text") return this._textValue;
    return this.children.map((c) => c.textContent).join("");
  }
  get childElementCount() { return this.children.length; }
  setAttribute(k, v) {
    this.attrs[k] = String(v);
    // The value attribute reflects onto the property (textInput relies on
    // reading .value back).
    if (k === "value") this.value = String(v);
  }
  appendChild(c) { this.children.push(c); return c; }
  addEventListener(type, fn) { (this._listeners[type] ||= []).push(fn); }
  click() {
    // Listeners may be async (fetch-models handlers); surface their
    // promises so tests can await completion.
    const rs = [];
    for (const fn of this._listeners.click || []) rs.push(fn({ preventDefault() {} }));
    return Promise.all(rs.filter((r) => r && typeof r.catch === "function"));
  }
  querySelector(sel) { return findNode(this, (n) => n.classList.contains(sel.slice(1))); }
}

function textOf(n) {
  if (!n) return "";
  if (n.tagName === "#text") return String(n.textContent);
  return n.children.map(textOf).join("");
}

function walk(n, fn) {
  if (!n || n.tagName === "#text") return null;
  if (fn(n)) return n;
  for (const c of n.children) {
    const hit = walk(c, fn);
    if (hit) return hit;
  }
  return null;
}

function findNode(root, fn) { return walk(root, fn); }

function countClass(n, cls) {
  if (!n || n.tagName === "#text") return 0;
  return (n.classList.contains(cls) ? 1 : 0) + n.children.reduce((s, c) => s + countClass(c, cls), 0);
}

function makeContext(snapshotSkills) {
  const doc = class DocNode extends StubNode {};
  const document = {
    createElement: (tag) => new doc(tag),
    createTextNode: (txt) => makeText(txt),
    addEventListener() {},
    removeEventListener() {},
  };
  const window = {};
  const toasts = [];
  const state = { snapshot: { skills: snapshotSkills } };
  const sandbox = { document, window, Node: doc, console };
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(SRC, "utf8"), sandbox, { filename: "settings-tab-providers.js" });

  const tabs = {};
  const root = new doc("div");
  // Inside the vm the IIFE binds to the context's own globalThis, which IS
  // the sandbox object — the export lands directly on it.
  sandbox.ClawdSettingsTabProviders.init({
    helpers: { t: (k) => k },
    ops: { showToast: (msg) => toasts.push(msg) },
    state,
    tabs,
  });
  // Mount the page (the settings shell calls core.tabs.providers.render).
  tabs.providers.render(root);
  return { sandbox, root, doc, toasts, window, state };
}

function isButton(n) { return n && n.tagName !== "#text" && String(n.tagName).toUpperCase() === "BUTTON"; }

function clickMode(root, key) {
  const btn = findNode(root, (n) => isButton(n) && textOf(n).includes(key));
  assert.ok(btn, `mode button ${key} should exist`);
  btn.click();
}

function openAddPane(root) {
  const add = findNode(root, (n) => isButton(n) && textOf(n).includes("provAddProvider"));
  assert.ok(add, "the + 添加供应商 button should exist in the source list");
  add.click();
}

describe("settings-tab-providers: 添加供应商 pane", () => {
  test("cloud mode: the click mounts the full pane — 14 vendor presets + manual form", () => {
    const { root } = makeContext({ defaultProvider: "local", modelProviders: [] });
    // Page opens in cloud mode (segmented control default), then the
    // captain's exact action: click 添加供应商.
    openAddPane(root);

    const cards = countClass(root, "prov-preset-card");
    assert.equal(cards, 14, `expected all 14 cloud vendor preset cards, got ${cards}`);
    const text = textOf(root);
    assert.ok(text.includes("provCloudPresets"), "云厂商 heading present");
    for (const vendor of ["DeepSeek", "Kimi", "智谱GLM", "通义千问", "MiniMax", "硅基流动", "火山方舟", "腾讯混元", "百度文心", "OpenAI", "Anthropic", "Gemini", "Groq", "OpenRouter"]) {
      assert.ok(text.includes(vendor), `vendor preset missing: ${vendor}`);
    }
    // The pane did not die mid-render: manual form and add action mounted.
    assert.ok(text.includes("provManualTitle"), "manual OpenAI form heading present");
    assert.ok(text.includes("provFieldKey"), "manual form fields present");
    assert.ok(findNode(root, (n) => textOf(n).includes("provAddAction")), "添加 button present");
    // No local servers leaked into the cloud half.
    assert.ok(!text.includes("LM Studio"), "cloud add pane must not mention LM Studio");
    assert.ok(!text.includes("Ollama"), "cloud add pane must not mention Ollama");
  });

  test("local mode: the same pane shows the two local server presets instead", async () => {
    const { root } = makeContext({ defaultProvider: "local", modelProviders: [] });
    // Mode switch RESETS the selection: local mode opens on the built-in
    // engine, not on whatever the cloud half had selected.
    clickMode(root, "provModeLocal");
    assert.ok(textOf(root).includes("provStatusChecking"), "local mode opens on the built-in engine");

    const addLocal = findNode(root, (n) => isButton(n) && textOf(n).includes("provAddProvider"));
    assert.ok(addLocal, "local list has its own add button");
    await addLocal.click();

    const text = textOf(root);
    assert.ok(text.includes("provPresetLmstudioDesc"), "LM Studio preset present");
    assert.ok(text.includes("provPresetOllamaDesc"), "Ollama preset present");
    assert.equal(countClass(root, "prov-preset-card"), 2, "exactly the two local presets");
    assert.ok(text.includes("provManualTitle"), "manual form still present in local mode");
  });

  test("the add-pane i18n keys exist in all five product languages (post-merge)", () => {
    // Load the real i18n module and assert against the MERGED table the
    // runtime actually serves — the source file splits keys between the
    // primary STRINGS table and the English gap-filler table, so raw
    // source slicing produces false negatives.
    const sandbox = { console };
    vm.createContext(sandbox);
    vm.runInContext(fs.readFileSync(I18N_SRC, "utf8"), sandbox, { filename: "settings-i18n.js" });
    const STRINGS = sandbox.ClawdSettingsI18n.STRINGS;
    const keys = ["provModeCloud", "provModeLocal", "provAddHintCloud", "provCloudPresets", "provPresetLmstudioDesc", "provPresetOllamaDesc", "provAddProvider", "provManualTitle", "provFetchModels", "provFetchOk", "provFetchFail", "provFetchOffline", "provModelsTitle", "provModelsAdd", "provModelsAddAll", "provCapTools", "provCapImages", "provCapThinking", "provModelsLast", "provCancel"];
    for (const lang of ["en", "zh", "zh-TW", "ko", "ja"]) {
      const block = STRINGS[lang] || {};
      for (const k of keys) {
        assert.ok(typeof block[k] === "string" && block[k].length > 0, `${k} missing from merged language table: ${lang}`);
      }
    }
  });

  test("local mode: the full list renders — server items, add button, built-in engine", () => {
    const { root } = makeContext({
      defaultProvider: "local",
      modelProviders: [{ provider: "lmstudio", apiKey: "lm-studio", baseUrl: "http://127.0.0.1:1234/v1", model: "local-model" }],
    });
    clickMode(root, "provModeLocal");

    const text = textOf(root);
    assert.ok(text.includes("provGroupLocalServers"), "local servers group title present");
    assert.ok(text.includes("provAddProvider"), "add button present in the local list");
    assert.ok(text.includes("provLocalName"), "built-in engine item present");
    // Detail defaulted to the built-in engine (defaultProvider local) and
    // mounts its status line instead of a cloud hint.
    assert.ok(text.includes("provStatusChecking"), "engine status line mounted");
    assert.ok(!text.includes("provCloudPickHint"), "no cloud hint in local mode");
  });

  test("server detail: 获取模型列表 feeds the model manager without a key field", async () => {
    const { root, toasts, window, state } = makeContext({
      defaultProvider: "local",
      modelProviders: [{ provider: "lmstudio", apiKey: "lm-studio", baseUrl: "http://127.0.0.1:1234/v1", model: "local-model" }],
    });
    window.settingsAPI = {
      // Mirror the real store: the snapshot the tab renders from must
      // reflect the save, or renderAll() re-renders stale state.
      update: async (_key, value) => {
        state.snapshot.skills = { ...state.snapshot.skills, ...value };
        window._saved = value;
      },
      discoverModels: async (payload) => {
        Object.assign(window, { _discoverPayload: payload });
        return { ok: true, models: ["z-model", "a-model"] };
      },
    };

    clickMode(root, "provModeLocal");
    const lmItem = findNode(root, (n) => isButton(n) && textOf(n).includes("LM Studio"));
    assert.ok(lmItem, "LM Studio source item present");
    await lmItem.click(); // select → renderServerDetail

    // Legacy single-model entry lazily becomes a one-row model list with
    // the legacy id marked as the active model.
    const menuList = findNode(root, (n) => n.classList.contains("prov-model-list"));
    assert.ok(menuList, "model list present");
    assert.ok(textOf(menuList).includes("local-model"), "legacy model id listed");
    assert.ok(findNode(root, (n) => n.classList.contains("prov-model-row") && n.classList.contains("is-active")),
      "legacy id is the active row");

    const fetchBtn = findNode(root, (n) => isButton(n) && textOf(n).includes("provFetchModels"));
    assert.ok(fetchBtn, "获取模型列表 button present");
    await fetchBtn.click(); // async handler: payload → menu → toast

    assert.deepEqual(JSON.parse(JSON.stringify(window._discoverPayload)), {
      baseUrl: "http://127.0.0.1:1234/v1",
      apiKey: "lm-studio",
    }, "discovery payload carries the stored dummy key, not an undefined keyInp");
    // The custom dropdown (NOT a native datalist — that one filters by the
    // input's current text and reads as dead) lists every discovered id
    // plus the "add all" footer.
    const menu = findNode(root, (n) => n.classList.contains("prov-model-menu"));
    assert.ok(menu, "model dropdown menu present");
    assert.equal(menu.hidden, false, "menu opens after discovery");
    assert.deepEqual(menu.children.map((o) => textOf(o)), ["z-model", "a-model", "provModelsAddAll"],
      "menu lists discovered ids + add-all footer");
    assert.ok(toasts.some((msg) => String(msg).includes("provFetchOk")), "success toast shown");

    // Picking a discovered id activates it AND persists the list entry
    // (gateway contract: entry.model stays the active id). The legacy
    // "local-model" entry STAYS in the list — picking adds, it never
    // silently drops existing entries. Each persist re-renders the pane,
    // so the menu must be re-located every round (stale nodes belong to a
    // detached DOM with stale closures).
    menu.children[0].click(); // z-model
    await new Promise((r) => setTimeout(r, 0)); // let the async persist settle
    assert.ok(window._saved, "model pick persisted skills");
    const savedLm = window._saved.modelProviders.find((x) => x.provider === "lmstudio");
    assert.equal(savedLm.model, "z-model", "picked id becomes the active model");
    assert.deepEqual(JSON.parse(JSON.stringify(savedLm.models.map((m) => m.id))), ["local-model", "z-model"],
      "picked id added to the list, legacy entry kept");

    // "Add all" merges the rest of the discovery result without touching
    // the active pick. Every persist re-renders, so re-locate the fetch
    // button each round too (the old one belongs to a detached DOM).
    await new Promise((r) => setTimeout(r, 0));
    let fb = findNode(root, (n) => isButton(n) && textOf(n).includes("provFetchModels"));
    await fb.click();
    let menu2 = findNode(root, (n) => n.classList.contains("prov-model-menu") && !n.hidden);
    menu2.children[1].click(); // a-model → picked as active
    await new Promise((r) => setTimeout(r, 0));
    fb = findNode(root, (n) => isButton(n) && textOf(n).includes("provFetchModels"));
    await fb.click();
    menu2 = findNode(root, (n) => n.classList.contains("prov-model-menu") && !n.hidden);
    menu2.children[2].click(); // provModelsAddAll
    await new Promise((r) => setTimeout(r, 0));
    const merged = window._saved.modelProviders.find((x) => x.provider === "lmstudio");
    assert.deepEqual(JSON.parse(JSON.stringify(merged.models.map((m) => m.id))), ["local-model", "z-model", "a-model"],
      "add-all merges without duplicates");
    assert.equal(merged.model, "a-model", "the last explicit pick stays active");
  });
});
