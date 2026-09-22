"use strict";

const path = require("path");

function requiredDependency(value, name) {
  if (!value) throw new Error(`registerPetInteractionIpc requires ${name}`);
  return value;
}

function registerPetInteractionIpc(options = {}) {
  const ipcMain = requiredDependency(options.ipcMain, "ipcMain");
  const showContextMenu = requiredDependency(options.showContextMenu, "showContextMenu");
  const moveWindowForDrag = requiredDependency(options.moveWindowForDrag, "moveWindowForDrag");
  const setIdlePaused = requiredDependency(options.setIdlePaused, "setIdlePaused");
  const isMiniTransitioning = requiredDependency(options.isMiniTransitioning, "isMiniTransitioning");
  const getCurrentState = requiredDependency(options.getCurrentState, "getCurrentState");
  const getCurrentSvg = requiredDependency(options.getCurrentSvg, "getCurrentSvg");
  const sendToRenderer = requiredDependency(options.sendToRenderer, "sendToRenderer");
  const setDragLocked = requiredDependency(options.setDragLocked, "setDragLocked");
  const setMouseOverPet = requiredDependency(options.setMouseOverPet, "setMouseOverPet");
  const beginDragSnapshot = requiredDependency(options.beginDragSnapshot, "beginDragSnapshot");
  const clearDragSnapshot = requiredDependency(options.clearDragSnapshot, "clearDragSnapshot");
  const syncHitWin = requiredDependency(options.syncHitWin, "syncHitWin");
  const isMiniMode = requiredDependency(options.isMiniMode, "isMiniMode");
  const checkMiniModeSnap = requiredDependency(options.checkMiniModeSnap, "checkMiniModeSnap");
  const hasPetWindow = requiredDependency(options.hasPetWindow, "hasPetWindow");
  const getPetWindowBounds = requiredDependency(options.getPetWindowBounds, "getPetWindowBounds");
  const getKeepSizeAcrossDisplays = requiredDependency(
    options.getKeepSizeAcrossDisplays,
    "getKeepSizeAcrossDisplays"
  );
  const getCurrentPixelSize = requiredDependency(options.getCurrentPixelSize, "getCurrentPixelSize");
  const computeDragEndBounds = requiredDependency(options.computeDragEndBounds, "computeDragEndBounds");
  const applyPetWindowBounds = requiredDependency(options.applyPetWindowBounds, "applyPetWindowBounds");
  const flushRuntimeStateToPrefs = requiredDependency(
    options.flushRuntimeStateToPrefs,
    "flushRuntimeStateToPrefs"
  );
  const reassertWinTopmost = requiredDependency(options.reassertWinTopmost, "reassertWinTopmost");
  const scheduleHwndRecovery = requiredDependency(options.scheduleHwndRecovery, "scheduleHwndRecovery");
  const repositionFloatingBubbles = requiredDependency(
    options.repositionFloatingBubbles,
    "repositionFloatingBubbles"
  );
  const exitMiniMode = requiredDependency(options.exitMiniMode, "exitMiniMode");
  const getDisableMiniMode = options.getDisableMiniMode || (() => false);
  const getFocusableLocalHudSessionIds = requiredDependency(
    options.getFocusableLocalHudSessionIds,
    "getFocusableLocalHudSessionIds"
  );
  const focusLog = requiredDependency(options.focusLog, "focusLog");
  const showDashboard = requiredDependency(options.showDashboard, "showDashboard");
  const focusSession = requiredDependency(options.focusSession, "focusSession");
  const revealSessionHud = requiredDependency(options.revealSessionHud, "revealSessionHud");
  const setLowPowerIdlePaused = requiredDependency(
    options.setLowPowerIdlePaused,
    "setLowPowerIdlePaused"
  );
  const statPath = requiredDependency(options.statPath, "statPath");
  const openTerminalAt = requiredDependency(options.openTerminalAt, "openTerminalAt");
  const dropLog = options.dropLog || (() => {});
  // 身体感受上报 (optional): forward touch reports to the gateway via the
  // chat module. Absent (noop) in tests / when the chat module is off.
  const reportInteraction = options.reportInteraction || null;
  // Where the pet sat when the current drag began — drag-end measures the
  // carry distance from this.
  let dragStart = null;
  const isMacPlatform = options.isMacPlatform != null
    ? !!options.isMacPlatform
    : process.platform === "darwin";
  const disposers = [];

  function on(channel, listener) {
    ipcMain.on(channel, listener);
    disposers.push(() => ipcMain.removeListener(channel, listener));
  }

  on("show-context-menu", showContextMenu);
  on("drag-move", () => moveWindowForDrag());

  on("pause-cursor-polling", () => {
    setIdlePaused(true);
  });
  on("resume-from-reaction", () => {
    setIdlePaused(false);
    if (isMiniTransitioning()) return;
    sendToRenderer("state-change", getCurrentState(), getCurrentSvg());
  });
  on("low-power-idle-paused", (_event, paused) => {
    setLowPowerIdlePaused(!!paused);
  });

  on("drag-lock", (_event, locked) => {
    setDragLocked(!!locked);
    if (locked) {
      setMouseOverPet(true);
      beginDragSnapshot();
      // 身体感受: remember where the pet sat so drag-end can report how
      // far the user carried it.
      dragStart = null;
      try {
        const b = getPetWindowBounds();
        dragStart = { x: b.x, y: b.y, at: Date.now() };
      } catch {}
    } else {
      clearDragSnapshot();
      syncHitWin();
    }
  });

  on("start-drag-reaction", (_event, direction) => {
    sendToRenderer("start-drag-reaction", direction === "left" || direction === "right" ? direction : null);
  });
  on("end-drag-reaction", () => sendToRenderer("end-drag-reaction"));
  // 身体感受: the hit renderer's click accumulator settled a burst
  // (1 = a tap, 2-3 = poking, 4+ = relentless). Fire-and-forget report.
  on("pet-interaction:click-burst", (_event, count) => {
    if (!reportInteraction) return;
    const n = Math.max(0, Math.min(20, Number(count) || 0));
    if (n <= 0) return;
    try {
      reportInteraction({ kind: "click", clicks: n });
    } catch {}
  });
  on("play-click-reaction", (_event, svg, duration) => {
    sendToRenderer("play-click-reaction", svg, duration);
  });

  on("drag-end", () => {
    // 身体感受: the user just carried the pet somewhere. Report the
    // gesture (carry distance + duration) before the bounds get clamped,
    // so the next chat knows it physically happened. Never throws.
    try {
      if (dragStart && reportInteraction) {
        const b = getPetWindowBounds();
        reportInteraction({
          kind: "drag",
          distance_px: Math.round(Math.hypot(b.x - dragStart.x, b.y - dragStart.y)),
          duration_ms: Math.max(0, Date.now() - dragStart.at),
        });
      }
    } catch {}
    dragStart = null;
    try {
      if (!isMiniMode() && !isMiniTransitioning()) {
        if (!getDisableMiniMode()) checkMiniModeSnap();
        if (isMiniMode() || isMiniTransitioning()) return;
        if (hasPetWindow()) {
          const virtualBounds = getPetWindowBounds();
          const size = getKeepSizeAcrossDisplays()
            ? { width: virtualBounds.width, height: virtualBounds.height }
            : getCurrentPixelSize();
          const clamped = computeDragEndBounds(virtualBounds, size);
          if (clamped) {
            applyPetWindowBounds(clamped);
            flushRuntimeStateToPrefs();
          }
          reassertWinTopmost();
          scheduleHwndRecovery();
          syncHitWin();
          repositionFloatingBubbles();
        }
      }
    } finally {
      setDragLocked(false);
      clearDragSnapshot();
    }
  });

  on("exit-mini-mode", () => {
    if (isMiniMode()) exitMiniMode();
  });

  on("pet-interaction:reveal-session-hud", () => {
    revealSessionHud();
  });

  // OS file drop from the hit window (#459, Windows/Linux only): first path
  // wins, files resolve to their parent directory, then open a plain terminal
  // there (no agent). The accept ping goes back to the SENDING window (hit
  // renderer plays its own reaction so its isReacting gate stays consistent).
  // macOS never registers the renderer-side listeners (screen-saver-level
  // windows are invisible to macOS drag-destination search); this guard is the
  // second layer so a stray IPC can't open terminals there either.
  on("pet-drop-paths", async (event, paths) => {
    try {
      if (isMacPlatform) {
        dropLog("drop ignored: OS file drop is disabled on macOS");
        return;
      }
      if (isMiniMode() || isMiniTransitioning()) return;
      if (!Array.isArray(paths)) return;
      const first = paths.find((p) => typeof p === "string" && p.length > 0);
      if (!first) return;
      let stats;
      try {
        stats = await statPath(first);
      } catch (_) {
        dropLog(`drop ignored: stat failed for ${first}`);
        return;
      }
      const dir = stats.isDirectory() ? first : path.dirname(first);
      const result = await openTerminalAt(dir);
      if (result && result.ok) {
        dropLog(`drop opened terminal=${result.terminal} dir=${dir}`);
        const sender = event && event.sender;
        if (sender && typeof sender.send === "function" && !(typeof sender.isDestroyed === "function" && sender.isDestroyed())) {
          sender.send("pet-drop-accepted");
        }
      } else {
        dropLog(`drop terminal launch failed: ${(result && result.message) || "unknown"}`);
      }
    } catch (err) {
      dropLog(`drop error: ${(err && err.message) || err}`);
    }
  });

  on("focus-terminal", () => {
    const focusableIds = getFocusableLocalHudSessionIds();
    focusLog(`focus request source=pet-body sid=- focusableCount=${focusableIds.length}`);
    if (focusableIds.length > 1) {
      focusLog(`focus result branch=none reason=multi-session-open-dashboard count=${focusableIds.length}`);
      showDashboard();
      return;
    }
    if (focusableIds.length === 1) {
      focusSession(focusableIds[0], { requestSource: "pet-body" });
      return;
    }
    focusLog("focus result branch=none reason=no-focusable-session source=pet-body");
  });

  return {
    dispose() {
      while (disposers.length) {
        const dispose = disposers.pop();
        dispose();
      }
    },
  };
}

module.exports = {
  registerPetInteractionIpc,
};
