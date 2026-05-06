#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");
const os = require("os");
const cp = require("child_process");

const ROOT = path.resolve(__dirname, "..");
const HOME = os.homedir();
const CODEX_HOME = process.env.CODEX_HOME || path.join(HOME, ".codex");
const INSTALL_ROOT = path.join(CODEX_HOME, "saki-codex-pet-overlay");
const OVERLAY_SCRIPT = path.join(INSTALL_ROOT, "overlay", "quota_overlay.py");
const SETTINGS_PATH = path.join(CODEX_HOME, "quota-overlay-settings.json");
const GLOBAL_STATE_PATH = path.join(CODEX_HOME, ".codex-global-state.json");

function fail(message) {
  console.error(`saki-codex-pet: ${message}`);
  process.exit(1);
}

function ensureWindows() {
  if (process.platform !== "win32") {
    fail("this overlay currently supports Windows only.");
  }
}

function copyDir(src, dest) {
  if (typeof fs.cpSync === "function") {
    fs.cpSync(src, dest, { recursive: true, force: true });
    return;
  }

  fs.mkdirSync(dest, { recursive: true });
  for (const entry of fs.readdirSync(src, { withFileTypes: true })) {
    const srcPath = path.join(src, entry.name);
    const destPath = path.join(dest, entry.name);
    if (entry.isDirectory()) {
      copyDir(srcPath, destPath);
    } else if (entry.isFile()) {
      fs.copyFileSync(srcPath, destPath);
    }
  }
}

function readJson(file, fallback) {
  try {
    return JSON.parse(fs.readFileSync(file, "utf8"));
  } catch {
    return fallback;
  }
}

function writeJson(file, value) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, `${JSON.stringify(value, null, 2)}\n`, "utf8");
}

function powershell(script) {
  return cp.spawnSync(
    "powershell.exe",
    ["-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
    { stdio: "inherit", windowsHide: true }
  );
}

function quotePowerShell(value) {
  return `'${String(value).replace(/'/g, "''")}'`;
}

function ensurePython() {
  const result = powershell(`
    $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if (-not $cmd) { $cmd = Get-Command python.exe -ErrorAction SilentlyContinue }
    if (-not $cmd) { exit 1 }
  `);
  if (result.status !== 0) {
    fail("Python was not found. Install Python 3 first, then run install again.");
  }
}

function installFiles() {
  copyDir(path.join(ROOT, "overlay"), path.join(INSTALL_ROOT, "overlay"));
  copyDir(path.join(ROOT, "pets", "saki"), path.join(CODEX_HOME, "pets", "saki"));

  fs.writeFileSync(
    path.join(INSTALL_ROOT, "run-saki-overlay.cmd"),
    `@echo off\r\npythonw "%~dp0overlay\\quota_overlay.py"\r\n`,
    "utf8"
  );
}

function selectSakiPet() {
  const state = readJson(GLOBAL_STATE_PATH, {});
  const persisted = state["electron-persisted-atom-state"] || {};
  persisted["selected-avatar-id"] = "custom:saki";
  state["electron-persisted-atom-state"] = persisted;
  writeJson(GLOBAL_STATE_PATH, state);
}

function prepareFirstRunSettings() {
  const settings = readJson(SETTINGS_PATH, {});
  if (!Object.prototype.hasOwnProperty.call(settings, "has_seen_initial_settings")) {
    settings.has_seen_initial_settings = false;
    writeJson(SETTINGS_PATH, settings);
  }
}

function stopOverlay() {
  powershell(`
    $target = ${quotePowerShell(OVERLAY_SCRIPT)}
    Get-CimInstance Win32_Process |
      Where-Object { $_.Name -in @('python.exe','pythonw.exe') -and $_.CommandLine -like "*$target*" } |
      ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
  `);
}

function createShortcuts() {
  const installRoot = quotePowerShell(INSTALL_ROOT);
  const script = quotePowerShell(OVERLAY_SCRIPT);
  const shortcutName = "Saki Codex Pet Overlay.lnk";
  const ps = `
    $ErrorActionPreference = 'Stop'
    $root = ${installRoot}
    $script = ${script}
    $pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
    if (-not $pythonw) { $pythonw = (Get-Command python.exe -ErrorAction Stop).Source }
    $shell = New-Object -ComObject WScript.Shell
    $desktop = [Environment]::GetFolderPath('Desktop')
    $startup = [Environment]::GetFolderPath('Startup')
    foreach ($dir in @($desktop, $startup)) {
      $shortcut = $shell.CreateShortcut((Join-Path $dir ${quotePowerShell(shortcutName)}))
      $shortcut.TargetPath = $pythonw
      $shortcut.Arguments = '"' + $script + '"'
      $shortcut.WorkingDirectory = $root
      $shortcut.WindowStyle = 7
      $shortcut.Description = 'Saki Codex pet quota overlay'
      $shortcut.Save()
    }
  `;
  const result = powershell(ps);
  if (result.status !== 0) {
    fail("failed to create shortcuts.");
  }
}

function removeShortcuts() {
  powershell(`
    $desktop = [Environment]::GetFolderPath('Desktop')
    $startup = [Environment]::GetFolderPath('Startup')
    foreach ($dir in @($desktop, $startup)) {
      Remove-Item -LiteralPath (Join-Path $dir 'Saki Codex Pet Overlay.lnk') -Force -ErrorAction SilentlyContinue
    }
  `);
}

function startOverlay() {
  const result = powershell(`
    $script = ${quotePowerShell(OVERLAY_SCRIPT)}
    $root = ${quotePowerShell(path.dirname(OVERLAY_SCRIPT))}
    $pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
    if (-not $pythonw) { $pythonw = (Get-Command python.exe -ErrorAction Stop).Source }
    Start-Process -FilePath $pythonw -ArgumentList ('"' + $script + '"') -WorkingDirectory $root -WindowStyle Hidden
  `);
  if (result.status !== 0) {
    fail("failed to start overlay.");
  }
}

function install() {
  ensureWindows();
  ensurePython();
  stopOverlay();
  installFiles();
  selectSakiPet();
  prepareFirstRunSettings();
  createShortcuts();
  startOverlay();
  console.log("Saki Codex Pet Overlay installed and started.");
  console.log(`Installed to: ${INSTALL_ROOT}`);
}

function uninstall() {
  ensureWindows();
  stopOverlay();
  removeShortcuts();
  console.log("Saki Codex Pet Overlay stopped and shortcuts removed.");
  console.log(`Installed files remain at: ${INSTALL_ROOT}`);
}

const command = (process.argv[2] || "install").toLowerCase();
if (command === "install") install();
else if (command === "start") startOverlay();
else if (command === "stop") stopOverlay();
else if (command === "uninstall") uninstall();
else {
  console.log("Usage: saki-codex-pet [install|start|stop|uninstall]");
  process.exit(command === "help" || command === "--help" || command === "-h" ? 0 : 1);
}
