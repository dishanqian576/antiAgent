/**
 * Electron preload — 安全地暴露少量 API 给渲染进程
 */
const { contextBridge } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
  isDesktop: true,
  platform: process.platform,
});
