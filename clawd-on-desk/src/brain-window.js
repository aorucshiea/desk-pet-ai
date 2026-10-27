"use strict";

// "大脑流" window — a standalone, read-only view of the pet's internal
// steps (rounds, decisions, thoughts, tool calls, ...). It is deliberately
// independent from the speech bubble: opening/closing it never touches the
// bubble window or its visibility.
//
// The window is created lazily on first open and destroyed when closed.
// The renderer talks to the gateway over SSE; the main process only hands
// out the base URL + token through a narrow IPC bridge.

const { BrowserWindow, ipcMain, screen } = require("electron");
const path = require("path");

const WINDOW_WIDTH = 620;
const WINDOW_HEIGHT = 780;
const MIN_WIDTH = 420;
const MIN_HEIGHT = 480;
const EDGE_MARGIN = 24;
const FALLBACK_WORK_AREA = { x: 0, y: 0, width: 1280, height: 800 };

function isUsableRect(rect) {
  return !!rect
    && Number.isFinite(rect.x)
    && Number.isFinite(rect.y)
    && Number.isFinite(rect.width)
    && Number.isFinite(rect.height)
    && rect.width > 0
    && rect.height > 0;
}

function rectsOverlap(a, b) {
  return !!a && !!b
    && a.x < b.x + b.width
    && a.x + a.width > b.x
    && a.y < b.y + b.height
    && a.y + a.height > b.y;
}

function clamp(value, min, max) {
  if (max < min) return min;
  return Math.max(min, Math.min(value, max));
}

function createBrainWindow(options = {}) {
  const getUrl = typeof options.getUrl === "function" ? options.getUrl : () => "";
  const getToken = typeof options.getToken === "function" ? options.getToken : () => "";
  const getPetBounds = typeof options.getPetBounds === "function" ? options.getPetBounds : () => null;

  let brainWindow = null;
  let handlersRegistered = false;

  function safeGetUrl() {
    try {
      const value = getUrl();
      return typeof value === "string" ? value : "";
    } catch {
      return "";
    }
  }

  function safeGetToken() {
    try {
      const value = getToken();
      return typeof value === "string" ? value : "";
    } catch {
      return "";
    }
  }

  function getPrimaryWorkArea() {
    try {
      const display = screen && typeof screen.getPrimaryDisplay === "function"
        ? screen.getPrimaryDisplay()
        : null;
      if (display && isUsableRect(display.workArea)) return display.workArea;
    } catch {}
    return FALLBACK_WORK_AREA;
  }

  function computeBounds() {
    const workArea = getPrimaryWorkArea();
    const width = Math.max(320, Math.min(WINDOW_WIDTH, workArea.width - 16));
    const height = Math.max(360, Math.min(WINDOW_HEIGHT, workArea.height - 16));

    // Near the right edge, vertically centered by default. If that lands on
    // top of the pet, slide left of it (or above it as a last resort).
    let x = workArea.x + workArea.width - width - EDGE_MARGIN;
    let y = workArea.y + Math.max(24, Math.round((workArea.height - height) / 2));

    let petBounds = null;
    try {
      const raw = getPetBounds();
      if (isUsableRect(raw)) petBounds = raw;
    } catch {}

    if (petBounds && rectsOverlap({ x, y, width, height }, petBounds)) {
      const leftOfPet = petBounds.x - width - 16;
      const abovePet = petBounds.y - height - 16;
      if (leftOfPet >= workArea.x + 8) {
        x = leftOfPet;
      } else if (abovePet >= workArea.y + 8) {
        y = abovePet;
      }
    }

    x = clamp(x, workArea.x + 8, workArea.x + workArea.width - width - 8);
    y = clamp(y, workArea.y + 8, workArea.y + workArea.height - height - 8);
    return { x: Math.round(x), y: Math.round(y), width: Math.round(width), height: Math.round(height) };
  }

  function registerIpcHandlers() {
    if (handlersRegistered) return;
    handlersRegistered = true;
    try {
      if (!ipcMain || typeof ipcMain.handle !== "function") return;
      if (typeof ipcMain.removeHandler === "function") {
        ipcMain.removeHandler("brain:info");
        ipcMain.removeHandler("brain:close");
      }
      ipcMain.handle("brain:info", () => {
        try {
          return { url: safeGetUrl(), token: safeGetToken() };
        } catch {
          return { url: "", token: "" };
        }
      });
      ipcMain.handle("brain:close", () => {
        close();
        return { ok: true };
      });
    } catch (err) {
      try { console.warn("[brain] failed to register IPC handlers:", err && err.message); } catch {}
    }
  }

  function createWindow() {
    const bounds = computeBounds();
    const win = new BrowserWindow({
      x: bounds.x,
      y: bounds.y,
      width: bounds.width,
      height: bounds.height,
      minWidth: MIN_WIDTH,
      minHeight: MIN_HEIGHT,
      title: "大脑流",
      backgroundColor: "#0b0f14",
      autoHideMenuBar: true,
      resizable: true,
      show: false,
      webPreferences: {
        preload: path.join(__dirname, "preload-brain.js"),
        contextIsolation: true,
        nodeIntegration: false,
      },
    });

    win.once("ready-to-show", () => {
      try {
        if (!win.isDestroyed()) win.show();
      } catch (err) {
        try { console.warn("[brain] ready-to-show failed:", err && err.message); } catch {}
      }
    });

    win.on("closed", () => {
      if (brainWindow === win) brainWindow = null;
    });

    try {
      const loadPromise = win.loadFile(path.join(__dirname, "brain.html"));
      if (loadPromise && typeof loadPromise.catch === "function") {
        loadPromise.catch((err) => {
          try { console.warn("[brain] failed to load brain.html:", err && err.message); } catch {}
        });
      }
    } catch (err) {
      try { console.warn("[brain] failed to load brain.html:", err && err.message); } catch {}
    }

    return win;
  }

  function open() {
    try {
      registerIpcHandlers();
      if (brainWindow && !brainWindow.isDestroyed()) {
        if (brainWindow.isMinimized()) brainWindow.restore();
        brainWindow.show();
        brainWindow.focus();
        return brainWindow;
      }
      brainWindow = createWindow();
      return brainWindow;
    } catch (err) {
      brainWindow = null;
      try { console.warn("[brain] failed to open window:", err && err.message); } catch {}
      return null;
    }
  }

  function close() {
    const win = brainWindow;
    brainWindow = null;
    if (!win) return;
    try {
      if (!win.isDestroyed()) win.close();
    } catch (err) {
      try { console.warn("[brain] failed to close window:", err && err.message); } catch {}
    }
  }

  function isOpen() {
    try {
      return !!brainWindow && !brainWindow.isDestroyed();
    } catch {
      return false;
    }
  }

  registerIpcHandlers();

  return { open, close, isOpen };
}

module.exports = createBrainWindow;
module.exports.createBrainWindow = createBrainWindow;
