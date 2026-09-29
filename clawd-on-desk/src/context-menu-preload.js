"use strict";

// Preload for the custom HTML context menu window (context-menu.html).
// The menu is data-driven: main sends item descriptors, the renderer
// draws them and reports its natural size; clicks come back by index.

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("contextMenuBridge", {
  onItems: (cb) => ipcRenderer.on("ctx-menu:items", (_e, items) => cb(items)),
  // anchor: "corner" (default, list menus) puts the top-left at the cursor;
  // "center" (the radial ring) puts the ring's hub on the cursor.
  sendSize: (w, h, anchor) => ipcRenderer.send("ctx-menu:size", { width: w, height: h, anchor }),
  sendClick: (idx) => ipcRenderer.send("ctx-menu:click", { index: idx }),
  sendClose: () => ipcRenderer.send("ctx-menu:close"),
});
