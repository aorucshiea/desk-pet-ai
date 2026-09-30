"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const createFloatingWindowRuntime = require("../src/floating-window-runtime");

const SRC_DIR = path.join(__dirname, "..", "src");

function makeWindow(label, calls, destroyed = false) {
  return {
    isDestroyed: () => destroyed,
    showInactive: () => calls.push(["show", label]),
    hide: () => calls.push(["hide", label]),
  };
}

describe("floating-window-runtime", () => {
  it("keeps floating bubble coordination out of main", () => {
    const mainSource = fs.readFileSync(path.join(SRC_DIR, "main.js"), "utf8");

    assert.match(mainSource, /createFloatingWindowRuntime/);
    assert.ok(!mainSource.includes("if (pendingPermissions.length) repositionBubbles();"));
  });

  it("repositions permission bubbles only when pending entries exist", () => {
    const calls = [];
    const pending = [];
    const runtime = createFloatingWindowRuntime({
      getPendingPermissions: () => pending,
      repositionPermissionBubbles: () => calls.push("permission"),
    });

    runtime.repositionFloatingBubbles();
    pending.push({ bubble: {} });
    runtime.repositionFloatingBubbles();

    assert.deepStrictEqual(calls, ["permission"]);
  });

  it("keeps anchored surface ordering as HUD first, then permission bubbles", () => {
    const calls = [];
    const runtime = createFloatingWindowRuntime({
      getPendingPermissions: () => [{ bubble: {} }],
      repositionSessionHud: () => calls.push("hud"),
      repositionPermissionBubbles: () => calls.push("permission"),
    });

    runtime.repositionAnchoredSurfaces();

    assert.deepStrictEqual(calls, ["hud", "permission"]);
  });

  it("syncs Session HUD visibility before repositioning dependent bubbles", () => {
    const calls = [];
    const runtime = createFloatingWindowRuntime({
      getPendingPermissions: () => [{ bubble: {} }],
      syncSessionHudVisibility: () => calls.push("syncHud"),
      repositionPermissionBubbles: () => calls.push("permission"),
    });

    runtime.syncSessionHudVisibilityAndBubbles();

    assert.deepStrictEqual(calls, ["syncHud", "permission"]);
  });

  it("restores live permission bubbles when the pet is shown", () => {
    const calls = [];
    const live = makeWindow("live", calls);
    const destroyed = makeWindow("destroyed", calls, true);
    const runtime = createFloatingWindowRuntime({
      getPendingPermissions: () => [{ bubble: live }, { bubble: destroyed }, { bubble: null }],
      keepOutOfTaskbar: (win) => calls.push(["taskbar", win === live ? "live" : "other"]),
    });

    runtime.showFloatingSurfacesForPet();

    assert.deepStrictEqual(calls, [
      ["show", "live"],
      ["taskbar", "live"],
    ]);
  });

  it("hides live permission bubbles when the pet is hidden", () => {
    const calls = [];
    const live = makeWindow("live", calls);
    const destroyed = makeWindow("destroyed", calls, true);
    const runtime = createFloatingWindowRuntime({
      getPendingPermissions: () => [{ bubble: live }, { bubble: destroyed }, { bubble: null }],
    });

    runtime.hideFloatingSurfacesForPet();

    assert.deepStrictEqual(calls, [
      ["hide", "live"],
    ]);
  });
});
