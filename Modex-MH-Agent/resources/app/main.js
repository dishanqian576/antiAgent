/**
 * MH Agent Desktop — Electron 主进程
 *
 * 职责：
 * 1. 启动内嵌 Python 后端（uvicorn）
 * 2. 等待后端 ready（轮询 /api/health）
 * 3. 创建 BrowserWindow 加载前端
 * 4. 托盘图标 + 关闭最小化到托盘
 * 5. 退出时杀掉 Python 子进程
 */

const { app, BrowserWindow, Tray, Menu, dialog } = require('electron');
const { spawn } = require('child_process');
const path = require('path');
const http = require('http');
const fs = require('fs');

// ── 路径 ──
const IS_DEV = !app.isPackaged;
const APP_ROOT = IS_DEV ? __dirname : path.join(process.resourcesPath, 'app');
const RUNTIME_DIR = IS_DEV
  ? path.join(__dirname, 'runtime')
  : path.join(path.dirname(process.resourcesPath), 'runtime');

const PYTHON_EXE = path.join(RUNTIME_DIR, 'python', 'python.exe');
const BACKEND_DIR = IS_DEV
  ? path.join(__dirname, '..', 'web', 'backend')
  : path.join(APP_ROOT, 'backend');

const PORT = 18088;
const HEALTH_URL = `http://127.0.0.1:${PORT}/api/health`;

let mainWindow = null;
let tray = null;
let pythonProcess = null;
let isQuitting = false;

// ── MiKTeX 自动安装 ──

function getMiKTeXDir() {
  const candidates = [
    path.join(process.env.LOCALAPPDATA || '', 'Programs', 'MiKTeX'),
    'C:\\Program Files\\MiKTeX',
  ];
  for (const p of candidates) {
    const xelatex = path.join(p, 'miktex', 'bin', 'x64', 'xelatex.exe');
    if (fs.existsSync(xelatex)) return p;
  }
  return null;
}

async function ensureMiKTeX() {
  // 检查 xelatex 是否可用（不只是 MiKTeX 目录存在）
  const { execSync } = require('child_process');
  
  // 先检查系统上有没有 xelatex
  let hasXelatex = false;
  try {
    execSync('where.exe xelatex', { stdio: 'ignore', timeout: 5000 });
    hasXelatex = true;
  } catch (e) {}
  
  if (!hasXelatex && getMiKTeXDir()) {
    // MiKTeX 装了但没有 xelatex，需要装 xetex 包
    const miktexDir = getMiKTeXDir();
    const miktexExe = path.join(miktexDir, 'miktex', 'bin', 'x64', 'miktex.exe');
    if (fs.existsSync(miktexExe)) {
      console.log('[MiKTeX] Installing xetex + Chinese packages...');
      const packages = ['xetex', 'ctex', 'xecjk', 'gbt7714', 'fontspec', 'booktabs', 'float', 'hyperref', 'amsmath', 'geometry', 'fancyhdr', 'caption', 'subcaption', 'multirow', 'listings', 'algorithm2e', 'pgfplots', 'xcolor', 'tcolorbox', 'biblatex', 'biber', 'natbib'];
      for (const pkg of packages) {
        try {
          execSync(`"${miktexExe}" packages install ${pkg}`, { stdio: 'ignore', timeout: 60000 });
        } catch (e) {} // 忽略已安装的包
      }
      console.log('[MiKTeX] Packages installed');
    }
    return;
  }
  
  if (hasXelatex) {
    console.log('[MiKTeX] xelatex already available');
    return;
  }

  // 没有 MiKTeX，用内嵌安装器安装
  const setupFile = path.join(RUNTIME_DIR, 'miktex-setup.exe');
  if (!fs.existsSync(setupFile)) {
    console.log('[MiKTeX] Installer not found at', setupFile);
    dialog.showMessageBox({
      type: 'warning',
      title: 'LaTeX 未安装',
      message: '未检测到 MiKTeX (LaTeX)，论文编译功能将不可用。\n请手动安装 MiKTeX: https://miktex.org/download',
    });
    return;
  }

  console.log('[MiKTeX] Installing from bundled installer (this may take several minutes)...');
  try {
    // MiKTeX 安装可能需要较长时间，给 15 分钟超时
    execSync(`"${setupFile}" --unattended --auto-install=yes --package-set=basic --paper-size=A4 --private`, {
      stdio: 'inherit',
      timeout: 900000,  // 15 分钟
    });
    console.log('[MiKTeX] Basic installation complete');
  } catch (e) {
    // 检查是否实际安装成功了（安装器可能返回非零退出码但实际装好了）
    if (getMiKTeXDir()) {
      console.log('[MiKTeX] Installation completed (installer returned non-zero but MiKTeX is present)');
    } else {
      console.error('[MiKTeX] Installation failed:', e.message);
      dialog.showMessageBox({
        type: 'warning',
        title: 'MiKTeX 安装失败',
        message: 'LaTeX 自动安装失败，论文编译功能可能不可用。\n请手动安装: https://miktex.org/download',
      });
      return;
    }
  }

  // 装完 basic 后，立刻装 xelatex 和中文包
  const newDir = getMiKTeXDir();
  if (newDir) {
    const miktexExe = path.join(newDir, 'miktex', 'bin', 'x64', 'miktex.exe');
    if (fs.existsSync(miktexExe)) {
      console.log('[MiKTeX] Installing xetex + Chinese packages...');
      const packages = ['xetex', 'ctex', 'xecjk', 'gbt7714', 'fontspec'];
      for (const pkg of packages) {
        try {
          execSync(`"${miktexExe}" packages install ${pkg}`, { stdio: 'ignore', timeout: 60000 });
        } catch (e) {}
      }
      // 启用自动安装缺失包
      const initexmf = path.join(newDir, 'miktex', 'bin', 'x64', 'initexmf.exe');
      try {
        execSync(`"${initexmf}" --set-config-value=[MPM]AutoInstall=1`, { stdio: 'ignore', timeout: 10000 });
      } catch (e) {}
      console.log('[MiKTeX] Full setup complete');
    }
  }
}

// ── Python 后端 ──

function startBackend() {
  // 查找可用的 Python
  let pythonPath;
  let pythonArgs;
  if (fs.existsSync(PYTHON_EXE)) {
    pythonPath = PYTHON_EXE;
    pythonArgs = ['-m', 'uvicorn', 'main:app', '--host', '127.0.0.1', '--port', String(PORT), '--log-level', 'info'];
  } else {
    // 开发模式：按优先级查找可用 Python
    const candidates = [
      'C:\\Windows\\py.exe',                    // Windows Launcher
      path.join(process.env.LOCALAPPDATA || '', 'Programs', 'Python', 'Python313', 'python.exe'),
      path.join(process.env.LOCALAPPDATA || '', 'Programs', 'Python', 'Python312', 'python.exe'),
      path.join(process.env.LOCALAPPDATA || '', 'Programs', 'Python', 'Python311', 'python.exe'),
      path.join(process.env.LOCALAPPDATA || '', 'Programs', 'Python', 'Python310', 'python.exe'),
      path.join(process.env.LOCALAPPDATA || '', 'Programs', 'Python', 'Python39', 'python.exe'),
    ];
    let found = false;
    for (const candidate of candidates) {
      if (candidate && fs.existsSync(candidate)) {
        if (candidate.endsWith('py.exe')) {
          pythonPath = candidate;
          pythonArgs = ['-3', '-m', 'uvicorn', 'main:app', '--host', '127.0.0.1', '--port', String(PORT), '--log-level', 'info'];
        } else {
          pythonPath = candidate;
          pythonArgs = ['-m', 'uvicorn', 'main:app', '--host', '127.0.0.1', '--port', String(PORT), '--log-level', 'info'];
        }
        found = true;
        break;
      }
    }
    if (!found) {
      pythonPath = 'python';
      pythonArgs = ['-m', 'uvicorn', 'main:app', '--host', '127.0.0.1', '--port', String(PORT), '--log-level', 'info'];
    }
  }

  const env = Object.assign({}, process.env, {
    MH_DESKTOP: '1',
    API_PORT: String(PORT),
    PYTHONDONTWRITEBYTECODE: '1',
    // 强制 UTF-8 编码（防止 Git Bash 写中文文件时乱码）
    LANG: 'en_US.UTF-8',
    LC_ALL: 'en_US.UTF-8',
    PYTHONIOENCODING: 'utf-8',
    PYTHONUTF8: '1',
  });

  // 把 runtime 工具链加入 PATH
  const extraPaths = [];
  const nodeDir = path.join(RUNTIME_DIR, 'node');
  if (fs.existsSync(nodeDir)) extraPaths.push(nodeDir);
  const texDir = path.join(RUNTIME_DIR, 'texlive', 'bin', 'windows');
  const texDirAlt = path.join(RUNTIME_DIR, 'texlive', 'miktex', 'bin', 'x64');
  if (fs.existsSync(texDir)) extraPaths.push(texDir);
  else if (fs.existsSync(texDirAlt)) extraPaths.push(texDirAlt);

  // Git Bash — Claude CLI 在 Windows 上必须有
  const gitBashPaths = [
    path.join(RUNTIME_DIR, 'git', 'bin', 'bash.exe'),
    'D:\\Git\\bin\\bash.exe',
    'C:\\Program Files\\Git\\bin\\bash.exe',
    'C:\\Program Files (x86)\\Git\\bin\\bash.exe',
  ];
  for (const bp of gitBashPaths) {
    if (fs.existsSync(bp)) {
      env.CLAUDE_CODE_GIT_BASH_PATH = bp;
      // 也把 git 的 cmd 和 bin 加入 PATH
      const gitBin = path.dirname(bp);
      extraPaths.push(gitBin);
      const gitCmd = path.join(path.dirname(gitBin), 'cmd');
      if (fs.existsSync(gitCmd)) extraPaths.push(gitCmd);
      console.log('[Backend] Git Bash:', bp);
      break;
    }
  }
  const pyDir = path.dirname(pythonPath);
  if (fs.existsSync(pyDir)) {
    extraPaths.push(pyDir);
    const scriptsDir = path.join(pyDir, 'Scripts');
    if (fs.existsSync(scriptsDir)) extraPaths.push(scriptsDir);
  }
  if (extraPaths.length) {
    env.PATH = extraPaths.join(';') + ';' + (env.PATH || '');
  }

  console.log('[Backend] Starting:', pythonPath, ...pythonArgs);
  console.log('[Backend] CWD:', BACKEND_DIR);

  pythonProcess = spawn(pythonPath, pythonArgs, {
    cwd: BACKEND_DIR,
    env,
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true,
  });

  pythonProcess.stdout.on('data', (data) => {
    process.stdout.write(`[Backend] ${data}`);
  });
  pythonProcess.stderr.on('data', (data) => {
    process.stderr.write(`[Backend] ${data}`);
  });
  pythonProcess.on('exit', (code) => {
    console.log(`[Backend] Process exited with code ${code}`);
    if (!isQuitting) {
      dialog.showErrorBox('后端异常退出', `Python 后端进程退出（code=${code}）。\n请检查日志或重启 Modex-MH-Agent。`);
    }
  });
}

function killBackend() {
  if (!pythonProcess) return;
  try {
    // Windows: taskkill /T 杀掉整个进程树
    const { execSync } = require('child_process');
    execSync(`taskkill /T /F /PID ${pythonProcess.pid}`, { stdio: 'ignore', shell: true });
  } catch (e) {
    try { pythonProcess.kill('SIGTERM'); } catch (_) {}
  }
  pythonProcess = null;
}

// ── 健康检查 ──

function waitForBackend(maxRetries = 60, interval = 500) {
  return new Promise((resolve, reject) => {
    let attempts = 0;
    const check = () => {
      attempts++;
      const req = http.get(HEALTH_URL, (res) => {
        if (res.statusCode === 200) {
          resolve();
        } else if (attempts < maxRetries) {
          setTimeout(check, interval);
        } else {
          reject(new Error(`Backend not ready after ${maxRetries} attempts`));
        }
      });
      req.on('error', () => {
        if (attempts < maxRetries) {
          setTimeout(check, interval);
        } else {
          reject(new Error(`Backend not ready after ${maxRetries} attempts`));
        }
      });
      req.setTimeout(2000, () => { req.destroy(); });
    };
    check();
  });
}

// ── 窗口 ──

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1400,
    height: 900,
    minWidth: 1000,
    minHeight: 700,
    title: 'Modex-MH-Agent',
    icon: path.join(__dirname, 'icon.ico'),
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
    show: false,
  });

  // 禁用缓存，确保每次加载最新的前端文件
  mainWindow.webContents.session.clearCache();

  mainWindow.loadURL(`http://127.0.0.1:${PORT}`);

  // 外部链接用系统浏览器打开
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith('http') && !url.includes('127.0.0.1')) {
      require('electron').shell.openExternal(url);
      return { action: 'deny' };
    }
    return { action: 'allow' };
  });
  mainWindow.webContents.on('will-navigate', (e, url) => {
    if (!url.includes('127.0.0.1')) {
      e.preventDefault();
      require('electron').shell.openExternal(url);
    }
  });

  mainWindow.once('ready-to-show', () => {
    mainWindow.show();
  });

  // 关闭时最小化到托盘
  mainWindow.on('close', (e) => {
    if (!isQuitting) {
      e.preventDefault();
      mainWindow.hide();
    }
  });
}

// ── 托盘 ──

function createTray() {
  const iconPath = path.join(__dirname, 'icon.ico');
  // 如果 icon 不存在，跳过托盘
  if (!fs.existsSync(iconPath)) {
    console.log('[Tray] icon.ico not found, skipping tray');
    return;
  }

  tray = new Tray(iconPath);
  tray.setToolTip('Modex-MH-Agent — AI 科研助手');

  const contextMenu = Menu.buildFromTemplate([
    {
      label: '显示主窗口',
      click: () => {
        if (mainWindow) {
          mainWindow.show();
          mainWindow.focus();
        }
      },
    },
    { type: 'separator' },
    {
      label: '退出',
      click: () => {
        isQuitting = true;
        app.quit();
      },
    },
  ]);

  tray.setContextMenu(contextMenu);
  tray.on('double-click', () => {
    if (mainWindow) {
      mainWindow.show();
      mainWindow.focus();
    }
  });
}

// ── 生命周期 ──

app.on('ready', async () => {
  createTray();

  // 首次启动：自动安装 MiKTeX（如果系统上没有）
  try {
    await ensureMiKTeX();
  } catch (e) {
    console.error('[MiKTeX] Setup error:', e.message);
    // 不阻塞启动，编译功能可能不可用但其他功能正常
  }

  startBackend();

  try {
    await waitForBackend();
    console.log('[App] Backend is ready');
    createWindow();
  } catch (err) {
    dialog.showErrorBox('启动失败', `后端启动超时：${err.message}\n请检查 Python 运行时是否完整。`);
    isQuitting = true;
    killBackend();
    app.quit();
  }
});

app.on('before-quit', () => {
  isQuitting = true;
  killBackend();
});

app.on('window-all-closed', () => {
  // macOS 上不退出（但本项目只针对 Windows）
  if (process.platform !== 'darwin') {
    // 不退出，保持托盘运行
  }
});

app.on('activate', () => {
  if (mainWindow) {
    mainWindow.show();
  }
});
