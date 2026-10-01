"use strict";

const { test, describe } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const SRC = path.join(__dirname, "..", "src", "settings-tab-providers.js");

// ── 模型来源 → 本地模型: the half that moved off the Brain page ─────────
// 机长's restructure: 大脑 only reports which brain is live; picking the
// .gguf, managing scan folders, the 推理引擎 card and 高级设置 all belong to
// the local source. These tests EXECUTE the real render pipeline (minimal
// DOM stub) so a scope or render-order break fails loudly here instead of
// blanking the page in the renderer console.

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
    if (k === "value") this._value = String(v);
  }
  // A <select> reflects its chosen <option value>, which is exactly what
  // the model switcher reads back after the user picks one.
  get value() {
    if (this.tagName === "select") {
      const cur = this.children.find((c) => c.selected);
      return cur ? String(cur.value) : "";
    }
    return this._value === undefined ? "" : this._value;
  }
  set value(v) {
    this._value = String(v);
    for (const c of this.children) c.selected = String(c.value) === String(v);
  }
  appendChild(c) { this.children.push(c); return c; }
  addEventListener(type, fn) { (this._listeners[type] ||= []).push(fn); }
  fire(type) {
    return Promise.all((this._listeners[type] || [])
      .map((fn) => fn({ preventDefault() {} })));
  }
  click() { return this.fire("click"); }
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

function findAll(root, fn, out = []) {
  if (!root || root.tagName === "#text") return out;
  if (fn(root)) out.push(root);
  for (const c of root.children) findAll(c, fn, out);
  return out;
}

function countClass(n, cls) {
  return findAll(n, (x) => x.classList.contains(cls)).length;
}

function isButton(n) { return n && String(n.tagName).toUpperCase() === "BUTTON"; }

function buttonWith(root, key) {
  return findNode(root, (n) => isButton(n) && textOf(n).includes(key));
}

const MODELS = [
  { path: "C:\\models\\a.gguf", label: "a.gguf", sizeBytes: 2 * 1024 ** 3, current: true },
  { path: "C:\\models\\b.gguf", label: "b.gguf", sizeBytes: 940 * 1024 ** 2 },
];

function makeContext(opts = {}) {
  const doc = class DocNode extends StubNode {};
  const document = {
    createElement: (tag) => new doc(tag),
    createTextNode: (txt) => makeText(txt),
  };
  const window = {};
  const calls = { useModelDir: [], removed: [], pick: 0, addFolder: 0, addFile: 0 };
  if (opts.petSettings !== false) {
    // Mutable, so removing a folder really shrinks what the pane repaints.
    let folders = ["C:\\models", "D:\\llm"];
    window.petSettings = {
      getStatus: async () => ({ sidecarReady: true, health: { alive: true } }),
      listLocalModels: async () => ({ models: MODELS }),
      listModelFolders: async () => ({ folders }),
      useModelDir: async (p) => { calls.useModelDir.push(p); return { ok: true }; },
      pickModelDir: async () => { calls.pick++; return { ok: true }; },
      addModelFolder: async () => { calls.addFolder++; return { ok: true, folders: ["C:\\models"] }; },
      addModelFile: async () => { calls.addFile++; return { ok: true, folders: ["C:\\models"] }; },
      removeModelFolder: async (f) => {
        calls.removed.push(f);
        folders = folders.filter((x) => x !== f);
        return { ok: true };
      },
    };
  }
  const sandbox = { document, window, Node: doc, console };
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(SRC, "utf8"), sandbox, { filename: "settings-tab-providers.js" });

  const tabs = {};
  const mounted = [];
  const toasts = [];
  const core = {
    helpers: { t: (k) => k },
    ops: { showToast: (m) => toasts.push(m) },
    state: {
      snapshot: {
        skills: {
          defaultProvider: "local",
          modelProviders: [{ provider: "lmstudio", baseUrl: "http://127.0.0.1:1234/v1" }],
        },
      },
    },
    tabs,
    // The Brain tab hands its engine + advanced cards over through this.
    enginePanel: opts.enginePanel === false ? undefined : {
      mount: (host) => mounted.push(host),
    },
  };
  sandbox.ClawdSettingsTabProviders.init(core);
  const root = new doc("div");
  tabs.providers.render(root);
  return { root, calls, mounted, toasts, core, sandbox, window };
}

async function settle(times = 6) {
  for (let i = 0; i < times; i++) await new Promise((r) => setImmediate(r));
}

async function openLocalPane(root) {
  const mode = buttonWith(root, "provModeLocal");
  assert.ok(mode, "the 本地模型 mode button exists");
  await mode.click();
  await settle();
}

describe("模型来源 → 本地模型: model choice, folders, engine host", () => {
  test("the built-in engine detail shows the model switcher and folder list", async () => {
    const { root, mounted } = makeContext();
    await openLocalPane(root);

    assert.equal(countClass(root, "prov-local-model"), 1, "exactly one local-model block");
    const select = findNode(root, (n) => n.tagName === "select");
    assert.ok(select, "model <select> rendered");
    const options = select.children.filter((o) => o.tagName === "option");
    assert.equal(options.length, 2, "one option per scanned .gguf");
    assert.ok(textOf(options[0]).includes("a.gguf"), "option label is the file name");
    assert.ok(textOf(options[0]).includes("2.00 GB"), "option shows the size");
    assert.equal(options[0].selected, true, "the loaded model is preselected");

    const text = textOf(root);
    assert.ok(text.includes("petChangeModel"), "更换… button present");
    assert.ok(text.includes("petModelsFolderAdd"), "add-folder button present");
    assert.ok(text.includes("petModelsFileAdd"), "add-file button present");
    assert.equal(countClass(root, "pet-model-folder-item"), 2, "both scan folders listed");

    assert.equal(mounted.length, 1, "the engine + advanced host is mounted once");
  });

  test("picking another model calls useModelDir with that path", async () => {
    const { root, calls } = makeContext();
    await openLocalPane(root);
    const select = findNode(root, (n) => n.tagName === "select");
    select.value = "C:\\models\\b.gguf";
    await select.fire("change");
    await settle();

    assert.deepEqual(calls.useModelDir, ["C:\\models\\b.gguf"],
      "the switcher loads the chosen file, nothing else");
    assert.equal(select.disabled, false, "the select is usable again after the swap");
  });

  test("更换… opens the model picker and repaints the pane", async () => {
    const { root, calls } = makeContext();
    await openLocalPane(root);
    const browse = buttonWith(root, "petChangeModel");
    await browse.click();
    await settle();
    assert.equal(calls.pick, 1, "pickModelDir invoked once");
    // renderAll() rebuilt the page, so a fresh switcher is on screen.
    assert.equal(countClass(root, "prov-local-model"), 1, "pane repainted, not duplicated");
  });

  test("removing a scan folder re-renders the list from the returned folders", async () => {
    const { root, calls } = makeContext();
    await openLocalPane(root);
    const rm = findAll(root, (n) => n.classList.contains("pet-model-folder-remove"));
    assert.equal(rm.length, 2, "one remove button per folder");
    await rm[0].click();
    await settle();

    assert.deepEqual(calls.removed, ["C:\\models"], "removeModelFolder got that folder");
    assert.equal(countClass(root, "pet-model-folder-item"), 1, "list shrank to the survivor");
  });

  test("no petSettings bridge: the pane still renders, minus the local block", async () => {    const { root, mounted } = makeContext({ petSettings: false });
    await openLocalPane(root);
    assert.equal(countClass(root, "prov-local-model"), 0,
      "the local-model block needs the bridge; without it nothing is painted");
    assert.equal(mounted.length, 0, "no engine host without the bridge");
    assert.ok(textOf(root).includes("provLocalName"), "the built-in engine item is still there");
  });

  test("missing enginePanel handle degrades instead of blanking the pane", async () => {
    const { root } = makeContext({ enginePanel: false });
    await openLocalPane(root);
    assert.ok(textOf(root).includes("provLocalName"), "detail rendered past the engine host");
    assert.equal(countClass(root, "prov-local-model"), 1, "the model switcher still mounts");
  });

  test("the switcher cannot be offered a directory as a model", () => {
    // Live bug: the picker listed a row called "models" — the bundled
    // fallback MODEL *directory* was pushed into the list unconditionally,
    // and choosing it failed with "请选择 .gguf 模型".
    const src = fs.readFileSync(
      path.join(__dirname, "..", "src", "pet-chat.js"), "utf8");
    const handler = src.slice(
      src.indexOf('"pet-settings:list-local-models"'),
      src.indexOf('"pet-settings:list-local-models"') + 3000);
    assert.ok(handler.length > 100, "handler found");
    assert.match(handler, /if \(!st\.isFile\(\)\) return;/,
      "push() must drop anything that is not a regular file");
    assert.doesNotMatch(handler, /if \(st\.isFile\(\)\) size = st\.size;/,
      "the old shape recorded a size only for files but listed everything");
  });
});
