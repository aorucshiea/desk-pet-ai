"use strict";

// Preload for the custom HTML context menu window (context-menu.html).
// The menu is data-driven: main sends item descriptors, the renderer
// draws them and reports its natural size; clicks come back by index.

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("contextMenuBridge", {
  onItems: (cb) => ipcRenderer.on("ctx-menu:items", (_e, items) => cb(items)),
  sendSize: (w, h) => ipcRenderer.send("ctx-menu:size", { width: w, height: h }),
  sendClick: (idx) => ipcRenderer.send("ctx-menu:click", { index: idx }),
  sendClose: () => ipcRenderer.send("ctx-menu:close"),
});
