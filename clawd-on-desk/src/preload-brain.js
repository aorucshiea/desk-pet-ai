"use strict";

const { contextBridge, ipcRenderer } = require("electron");

// Narrow bridge for the "大脑流" window. The renderer only needs the
// gateway location and a way to close itself — no other main-process APIs.
contextBridge.exposeInMainWorld("petBrain", {
  info: () => ipcRenderer.invoke("brain:info"),
  close: () => ipcRenderer.invoke("brain:close"),
});
