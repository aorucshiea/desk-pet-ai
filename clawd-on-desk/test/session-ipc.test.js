"use strict";

const test = require("node:test");
const assert = require("node:assert");

const { registerSessionIpc } = require("../src/session-ipc");

class FakeIpcMain {
  constructor() {
    this.handlers = new Map();
    this.listeners = new Map();
  }

  handle(channel, listener) {
    this.handlers.set(channel, listener);
  }

  on(channel, listener) {
    this.listeners.set(channel, listener);
  }

  removeHandler(channel) {
    this.handlers.delete(channel);
  }

  removeListener(channel, listener) {
    if (this.listeners.get(channel) === listener) this.listeners.delete(channel);
  }

  invoke(channel, ...args) {
    const listener = this.handlers.get(channel);
    assert.strictEqual(typeof listener, "function", `missing IPC handler ${channel}`);
    return listener({ sender: "sender-web-contents" }, ...args);
  }

  send(channel, ...args) {
    const listener = this.listeners.get(channel);
    assert.strictEqual(typeof listener, "function", `missing IPC listener ${channel}`);
    return listener({ sender: "sender-web-contents" }, ...args);
  }
}

function createHarness(overrides = {}) {
  const calls = [];
  const ipcMain = new FakeIpcMain();
  const runtime = registerSessionIpc({
    ipcMain,
    getI18n: overrides.getI18n || (() => ({ lang: "en", translations: { title: "Sessions" } })),
    focusSession: overrides.focusSession || ((sessionId, options) => {
      calls.push(["focusSession", sessionId, options]);
    }),
    setSessionHudPinned: overrides.setSessionHudPinned || ((value) => {
      calls.push(["setSessionHudPinned", value]);
    }),
    ackSessionCompletion: overrides.ackSessionCompletion || ((sessionId) => {
      calls.push(["ackSessionCompletion", sessionId]);
      return true;
    }),
  });
  return { ipcMain, runtime, calls };
}

test("session IPC registers owned channels and disposes them", () => {
  const { ipcMain, runtime } = createHarness();

  assert.deepStrictEqual([...ipcMain.handlers.keys()].sort(), [
    "session-hud:get-i18n",
    "session:ack-completion",
  ]);
  assert.deepStrictEqual([...ipcMain.listeners.keys()].sort(), [
    "session-hud:focus-session",
    "session-hud:set-pinned",
  ]);

  runtime.dispose();

  assert.strictEqual(ipcMain.handlers.size, 0);
  assert.strictEqual(ipcMain.listeners.size, 0);
});

test("session IPC delegates HUD behavior", async () => {
  const { ipcMain, calls } = createHarness();

  assert.deepStrictEqual(await ipcMain.invoke("session-hud:get-i18n"), {
    lang: "en",
    translations: { title: "Sessions" },
  });
  ipcMain.send("session-hud:focus-session", "hud-session");
  ipcMain.send("session-hud:set-pinned", true);
  ipcMain.send("session-hud:set-pinned", 0);

  assert.deepStrictEqual(calls, [
    ["focusSession", "hud-session", { requestSource: "hud" }],
    ["setSessionHudPinned", true],
    ["setSessionHudPinned", false],
  ]);
});

test("session:ack-completion returns {status:ok} when ack lands", async () => {
  const { ipcMain, calls } = createHarness({
    ackSessionCompletion: (sessionId) => {
      calls.push(["ackSessionCompletion", sessionId]);
      return true;
    },
  });
  const result = await ipcMain.invoke("session:ack-completion", "s1");
  assert.deepStrictEqual(result, { status: "ok" });
  assert.deepStrictEqual(calls, [["ackSessionCompletion", "s1"]]);
});

test("session:ack-completion returns noop when session missing or unflagged", async () => {
  const { ipcMain } = createHarness({
    ackSessionCompletion: () => false,
  });
  const result = await ipcMain.invoke("session:ack-completion", "s-missing");
  assert.deepStrictEqual(result, { status: "noop", reason: "not-pending-or-missing" });
});

test("session:ack-completion returns error when ackSessionCompletion throws", async () => {
  const { ipcMain } = createHarness({
    ackSessionCompletion: () => { throw new Error("boom"); },
  });
  const result = await ipcMain.invoke("session:ack-completion", "s1");
  assert.strictEqual(result.status, "error");
  assert.strictEqual(result.message, "boom");
});

test("session:ack-completion validates sessionId payload", async () => {
  const { ipcMain } = createHarness();
  for (const bad of [null, undefined, "", 42, { id: "s1" }]) {
    const result = await ipcMain.invoke("session:ack-completion", bad);
    assert.strictEqual(result.status, "error", `expected error for payload ${JSON.stringify(bad)}`);
  }
});

test("registerSessionIpc requires ackSessionCompletion dep", () => {
  assert.throws(
    () => registerSessionIpc({
      ipcMain: new FakeIpcMain(),
      getI18n: () => ({}),
      focusSession: () => {},
      setSessionHudPinned: () => {},
      // ackSessionCompletion intentionally absent
    }),
    /ackSessionCompletion/
  );
});
