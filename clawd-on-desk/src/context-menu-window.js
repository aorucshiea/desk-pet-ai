"use strict";

// Custom HTML context menu (现代做法：无边框透明子窗口替换原生 Menu.popup).
// Native menus can't be styled on Windows and clash with the pet's dark
// glass aesthetic. This module owns one shared frameless transparent
// window that renders item data sent from main; clicks come back by
// index and are dispatched to the caller's closures after the window
// hides (so actions never steal focus into the menu).

const path = require("path");
const { BrowserWindow, ipcMain, screen } = require("electron");

const MENU_WIDTH = 252;
const MENU_HTML = path.join(__dirname, "context-menu.html");
const MENU_PRELOAD = path.join(__dirname, "context-menu-preload.js");

let menuWindow = null;
let clickHandlers = [];
let pendingItems = null;
// Remote-desktop sessions (RustDesk/RDP) often refuse programmatic
// focus — show() never acquires focus and Electron fires blur
// immediately, which would hide the menu the instant it opens.
// Only a menu that WAS focused may close on blur; the unfocused case
// (remote sessions) closes via Escape / item click / idle timeout.
let menuEverFocused = false;

function ensureMenuWindow() {
  if (menuWindow && !menuWindow.isDestroyed()) return menuWindow;
  menuWindow = new BrowserWindow({
    width: MENU_WIDTH,
    height: 80,
    frame: false,
    transparent: true,
    resizable: false,
    movable: false,
    minimizable: false,
    maximizable: false,
    skipTaskbar: true,
    alwaysOnTop: true,
    show: false,
    hasShadow: false,
    focusable: true,
    fullscreenable: false,
    webPreferences: {
      preload: MENU_PRELOAD,
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  menuWindow.setMenuBarVisibility(false);
  menuWindow.loadFile(MENU_HTML);
  menuWindow.on("focus", () => { menuEverFocused = true; });
  menuWindow.on("blur", () => {
    if (menuEverFocused) hideContextMenu();
  });
  menuWindow.on("closed", () => { menuWindow = null; });
  // Diagnostics: an invisible menu window is indistinguishable from
  // "right-click does nothing" — surface renderer failures loudly.
  menuWindow.webContents.on("console-message", (_e, level, message) => {
    if (level >= 2) console.error("[context-menu]", message);
  });
  menuWindow.webContents.on("did-fail-load", (_e, code, desc) => {
    console.error("[context-menu] load failed:", code, desc);
  });

  ipcMain.on("ctx-menu:size", (_e, { width, height } = {}) => {
    if (!menuWindow || menuWindow.isDestroyed() || !pendingItems) return;
    const w = Math.max(200, Math.min(400, Math.round(width || MENU_WIDTH)));
    const h = Math.max(40, Math.round(height || 80));
    placeAndShow(w, h);
  });
  ipcMain.on("ctx-menu:click", (_e, { index } = {}) => {
    const handler = clickHandlers[Number(index)];
    hideContextMenu();
    // Dispatch after hiding so the action runs without the menu window
    // hovering over whatever it opens.
    if (typeof handler === "function") setTimeout(() => { try { handler(); } catch {} }, 30);
  });
  ipcMain.on("ctx-menu:close", () => hideContextMenu());
  return menuWindow;
}

function placeAndShow(width, height) {
  if (!menuWindow || menuWindow.isDestroyed()) return;
  const cursor = screen.getCursorScreenPoint();
  const display = screen.getDisplayNearestPoint(cursor);
  const wa = display.workArea;
  let x = cursor.x + 4;
  let y = cursor.y + 4;
  if (x + width > wa.x + wa.width - 8) x = wa.x + wa.width - width - 8;
  if (y + height > wa.y + wa.height - 8) y = cursor.y - height - 6; // flip above
  if (y < wa.y + 4) y = wa.y + 4;
  if (x < wa.x + 4) x = wa.x + 4;
  menuWindow.setBounds({ x: Math.round(x), y: Math.round(y), width, height });
  menuWindow.show();
  menuWindow.focus();
}

function hideContextMenu() {
  pendingItems = null;
  menuEverFocused = false;
  if (menuWindow && !menuWindow.isDestroyed() && menuWindow.isVisible()) menuWindow.hide();
}

const MENU_IDLE_TIMEOUT_MS = 20000;

/**
 * Show a styled context menu.
 * @param {Array} items - flat descriptor list:
 *   { type: "separator" } | { label, icon?, checked?, danger?, disabled?, click? }
 * @param {{x?: number, y?: number}} [pos] - screen point (defaults to cursor)
 */
async function showContextMenu(items, pos = {}) {
  if (!Array.isArray(items) || !items.length) return;
  clickHandlers = items.map((it) => (it && typeof it.click === "function" ? it.click : null));
  pendingItems = items.map((it) => {
    if (!it || it.type === "separator") return { type: "separator" };
    return {
      label: String(it.label || ""),
      icon: it.icon || "",
      checked: !!it.checked,
      danger: !!it.danger,
      disabled: !!it.disabled,
    };
  });

  const win = ensureMenuWindow();
  const send = () => {
    if (!win || win.isDestroyed()) return;
    win.webContents.send("ctx-menu:items", pendingItems);
  };
  if (win.webContents.isLoading()) {
    win.webContents.once("did-finish-load", send);
  } else {
    send();
  }
  // Safety net: if the renderer never reports its size, the window stays
  // a transparent nothing that looks exactly like "right-click does
  // nothing" — reject so the caller can fall back to the native menu.
  return new Promise((resolve, reject) => {
    let sizeArrived = false;
    let settled = false;
    const finish = (err) => {
      if (settled) return;
      settled = true;
      ipcMain.removeListener("ctx-menu:size", onSize);
      clearTimeout(sizeTimer);
      clearTimeout(idleTimer);
      if (err) reject(err);
      else resolve();
    };
    const onSize = (_e, { width, height } = {}) => {
      if (sizeArrived) return;
      sizeArrived = true;
      placeAndShow(
        Math.max(200, Math.min(400, Math.round(width || MENU_WIDTH))),
        Math.max(40, Math.round(height || 80)),
      );
      finish();
    };
    ipcMain.on("ctx-menu:size", onSize);
    const sizeTimer = setTimeout(() => {
      if (!sizeArrived) {
        hideContextMenu();
        finish(new Error("context menu renderer reported no size within 1.5s"));
      }
    }, 1500);
    // Remote-session fallback: if focus never arrived, blur will never
    // fire — close after an idle timeout instead of lingering forever.
    const idleTimer = setTimeout(() => {
      if (!menuEverFocused) {
        hideContextMenu();
        finish(new Error("context menu idle timeout (never focused — remote session?)"));
      }
    }, MENU_IDLE_TIMEOUT_MS);
    // If the renderer already reported a size for the PREVIOUS show, place
    // immediately; the fresh size event re-places us right after.
    placeAndShow(MENU_WIDTH, 80);
  });
}

module.exports = { showContextMenu, hideContextMenu };
