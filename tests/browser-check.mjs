// 浏览器端验收：真实渲染八张 Canvas 图，检查紫色标记、休市日映射、提示框和 320px 布局，
// 并保存桌面与窄屏截图。使用 Chrome/Edge 的 DevTools 协议，无第三方依赖。
// 运行前先启动本地服务器：python scripts/serve.py --port 8080
// 运行：node tests/browser-check.mjs
import { spawn } from "node:child_process";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";
import { displayWindow } from "../public/window.js";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.dirname(here);
const artifacts = path.join(here, "artifacts");
const dashboard = process.env.DASHBOARD_URL ?? "http://127.0.0.1:8080/";
const port = Number(process.env.CDP_PORT ?? 9333);
const records = JSON.parse(await readFile(path.join(root, "public/api/history.json"), "utf8")).records;

const candidates = [
  process.env.CHROME_PATH,
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
].filter(Boolean);
const chromePath = candidates.find((file) => existsSync(file));
if (!chromePath) throw new Error("未找到 Chrome 或 Edge，可设置 CHROME_PATH");

const profile = await mkdtemp(path.join(tmpdir(), "etf-cdp-"));
await mkdir(artifacts, { recursive: true });
const browser = spawn(chromePath, [
  "--headless=new", `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`,
  "--no-first-run", "--no-default-browser-check", "--disable-gpu", "--disable-extensions",
  "--hide-scrollbars", "--force-device-scale-factor=1", "--no-proxy-server", "--window-size=1280,2200",
  "about:blank",
], { stdio: "ignore", windowsHide: true });

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function getJSON(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`HTTP ${response.status}: ${url}`);
  return response.json();
}
async function waitFor(fn, timeout = 30000, interval = 250) {
  const deadline = Date.now() + timeout;
  let last;
  while (Date.now() < deadline) {
    try { const value = await fn(); if (value) return value; } catch (error) { last = error; }
    await sleep(interval);
  }
  throw new Error(`等待超时：${last?.message ?? "条件未满足"}`);
}

let ws;
let sessionId;
let nextId = 1;
const pending = new Map();
const events = [];
function send(method, params = {}, useSession = true) {
  const id = nextId++;
  const message = { id, method, params };
  if (useSession) message.sessionId = sessionId;
  ws.send(JSON.stringify(message));
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
    setTimeout(() => { if (pending.delete(id)) reject(new Error(`CDP 超时：${method}`)); }, 60000);
  });
}
async function evaluate(expression) {
  const result = await send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
  if (result.exceptionDetails) throw new Error(`页面脚本错误：${result.exceptionDetails.text} ${result.exceptionDetails.exception?.description ?? ""}`);
  return result.result.value;
}

const cleanup = async () => {
  try { ws?.close(); } catch {}
  if (browser.pid) {
    if (process.platform === "win32") spawn("taskkill", ["/pid", String(browser.pid), "/T", "/F"], { stdio: "ignore" });
    else browser.kill("SIGKILL");
  }
  await sleep(500);
  await rm(profile, { recursive: true, force: true }).catch(() => {});
};
process.on("exit", () => { if (browser.pid && process.platform !== "win32") browser.kill("SIGKILL"); });

try {
  const version = await waitFor(() => getJSON(`http://127.0.0.1:${port}/json/version`), 20000);
  ws = new WebSocket(version.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = () => reject(new Error("DevTools 连接失败")); });
  ws.onmessage = (event) => {
    const message = JSON.parse(event.data);
    if (message.id && pending.has(message.id)) {
      const { resolve, reject } = pending.get(message.id);
      pending.delete(message.id);
      if (message.error) reject(new Error(`${message.error.message} (${message.error.code})`));
      else resolve(message.result);
    } else if (message.method) events.push(message);
  };

  const { targetId } = await send("Target.createTarget", { url: "about:blank" }, false);
  ({ sessionId } = await send("Target.attachToTarget", { targetId, flatten: true }, false));
  await send("Page.enable");
  await send("Runtime.enable");
  await send("Emulation.setDeviceMetricsOverride", { width: 1280, height: 1000, deviceScaleFactor: 1, mobile: false });
  await send("Page.navigate", { url: dashboard });

  const ready = await waitFor(async () => {
    const state = await evaluate(`(() => ({
      readyState: document.readyState,
      charts: document.querySelectorAll("canvas").length,
      asOf: document.getElementById("as-of")?.textContent,
      range: document.getElementById("range")?.textContent,
      published: document.getElementById("published")?.textContent,
    }))()`);
    return state.readyState === "complete" && state.charts === 8 && state.asOf === "2026-11-24" ? state : null;
  }, 40000);
  assert.equal(ready.charts, 8, "应有 8 张图");
  assert.equal(ready.asOf, "2026-11-24", "数据截至日应为 2026-11-24");
  assert.match(ready.published, /更新于 2026-\d{2}-\d{2} \d{2}:\d{2}（北京时间）/, "应显示发布时间");

  const latest = records.at(-1).date;
  const beijingToday = new Intl.DateTimeFormat("sv-SE", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date());
  const expectedRange = (chosen) => {
    const window = displayWindow(records, chosen);
    const mapped = chosen === window.selected ? "" : ` · 对应交易日 ${window.selected}`;
    return `${window.rows[0].date} 至 ${window.rows.at(-1).date} · ${window.rows.length}个交易日${mapped}`;
  };
  assert.equal(ready.range, expectedRange(latest), "默认窗口文字应与交接文档口径一致");

  const structure = await evaluate(`(() => ({
    titles: [...document.querySelectorAll(".chart h2")].map((node) => node.textContent),
    pickerMin: document.getElementById("date").min,
    pickerMax: document.getElementById("date").max,
    pickerValue: document.getElementById("date").value,
    columns: getComputedStyle(document.getElementById("charts")).gridTemplateColumns.trim().split(/\\s+/).length,
    overflow: document.documentElement.scrollWidth - window.innerWidth,
    canvasLabels: [...document.querySelectorAll("canvas")].map((node) => node.getAttribute("aria-label")),
  }))()`);
  assert.deepEqual(structure.titles, ["沪深300", "上证50", "创业板指", "科创50", "中证A500", "中证500", "中证1000", "中证2000"], "八张图标题顺序");
  assert.equal(structure.pickerMin, "2026-01-01", "可选起始日");
  assert.equal(structure.pickerMax, beijingToday > latest ? beijingToday : latest, "可选截止日应为北京时间当天");
  assert.equal(structure.pickerValue, latest, "默认选中最新交易日");
  assert.equal(structure.columns, 2, "桌面应为两列");
  assert.ok(structure.overflow <= 0, `桌面不应横向溢出：${structure.overflow}px`);
  assert.ok(structure.canvasLabels.every((label) => label.includes("查询日期")), "每张图应有可读的 aria-label");

  const purple = async () => evaluate(`(() => {
    const canvas = document.querySelector("canvas");
    const data = canvas.getContext("2d").getImageData(0, 0, canvas.width, canvas.height).data;
    let count = 0;
    const columns = new Map();
    for (let y = 0; y < canvas.height; y += 1) {
      for (let x = 0; x < canvas.width; x += 1) {
        const i = (y * canvas.width + x) * 4;
        if (Math.abs(data[i] - 107) < 22 && Math.abs(data[i + 1] - 51) < 22 && Math.abs(data[i + 2] - 158) < 22 && data[i + 3] > 200) {
          count += 1;
          columns.set(x, (columns.get(x) ?? 0) + 1);
        }
      }
    }
    return { count, tallest: Math.max(0, ...columns.values()), height: canvas.height, width: canvas.width };
  })()`);
  const latestPurple = await purple();
  assert.ok(latestPurple.count > 40, `查询日应有紫色空心圆和紫色日期：${latestPurple.count} 像素`);
  assert.ok(latestPurple.tallest < latestPurple.height * 0.4, `图内不应有查询日竖线：最高紫色列 ${latestPurple.tallest}/${latestPurple.height}`);

  const tooltip = await evaluate(`(() => {
    const canvas = document.querySelector("canvas");
    const box = canvas.getBoundingClientRect();
    canvas.dispatchEvent(new PointerEvent("pointermove", { clientX: box.left + box.width * 0.4, clientY: box.top + box.height * 0.5, bubbles: true }));
    const node = document.querySelector(".chart .tooltip");
    return { hidden: node.hidden, text: node.textContent };
  })()`);
  assert.equal(tooltip.hidden, false, "悬停应显示提示框");
  assert.match(tooltip.text, /^\d{4}-\d{2}-\d{2}\n日度变动 [+-]?\d+\.\d{2} 亿元\nETF总规模 \d[\d,]*\.\d{2} 亿元$/, "提示框应含实际交易日期和两项数值");

  const holiday = "2026-10-03";
  const holidayState = await evaluate(`(() => {
    const picker = document.getElementById("date");
    picker.value = "${holiday}";
    picker.dispatchEvent(new Event("change"));
    return {
      range: document.getElementById("range").textContent,
      value: picker.value,
      tooltipHidden: document.querySelector(".chart .tooltip").hidden,
      labels: [...document.querySelectorAll("canvas")].map((node) => node.getAttribute("aria-label")),
    };
  })()`);
  assert.equal(holidayState.value, holiday, "日期控件应保留输入值");
  assert.equal(holidayState.range, expectedRange(holiday), "休市日应映射到前一交易日并说明");
  assert.ok(holidayState.range.includes("对应交易日 2026-09-30"), "国庆休市应映射到 2026-09-30");
  assert.ok(holidayState.labels.every((label) => label.includes(holiday) && label.includes("2026-09-30")), "八张图应同步切换到休市日映射后的交易日");
  const holidayPurple = await purple();
  assert.ok(holidayPurple.count > 40, "休市日查询仍应有紫色标记");
  const holidayShot = await screenshot("dashboard-holiday-2026-10-03.png");

  const first = await evaluate(`(() => {
    const picker = document.getElementById("date");
    picker.value = "2026-01-01";
    picker.dispatchEvent(new Event("change"));
    return { range: document.getElementById("range").textContent };
  })()`);
  assert.equal(first.range, expectedRange("2026-01-01"), "2026-01-01 应映射到首个交易日");

  const reset = await evaluate(`(() => {
    document.getElementById("latest").click();
    return {
      range: document.getElementById("range").textContent,
      value: document.getElementById("date").value,
      disabled: document.getElementById("latest").disabled,
    };
  })()`);
  assert.equal(reset.value, latest, "回到最新应恢复最新交易日");
  assert.equal(reset.range, expectedRange(latest), "回到最新后窗口文字");
  assert.equal(reset.disabled, true, "已在最新时按钮应禁用");

  const desktopShot = await screenshot("dashboard-desktop-1280.png");

  await send("Emulation.setDeviceMetricsOverride", { width: 320, height: 900, deviceScaleFactor: 2, mobile: true });
  await sleep(600);
  const mobile = await evaluate(`(() => ({
    columns: getComputedStyle(document.getElementById("charts")).gridTemplateColumns.trim().split(/\\s+/).length,
    overflow: document.documentElement.scrollWidth - window.innerWidth,
    canvases: document.querySelectorAll("canvas").length,
    width: document.querySelector("canvas").clientWidth,
    range: document.getElementById("range").textContent,
  }))()`);
  assert.equal(mobile.columns, 1, "窄屏应为单列");
  assert.ok(mobile.overflow <= 0, `320px 不应横向溢出：${mobile.overflow}px`);
  assert.equal(mobile.canvases, 8, "窄屏仍应有 8 张图");
  assert.ok(mobile.width > 200, `窄屏画布应有可用宽度：${mobile.width}px`);
  assert.equal(mobile.range, expectedRange(latest), "窄屏窗口文字保持一致");
  const mobileShot = await screenshot("dashboard-mobile-320.png");

  console.log(JSON.stringify({
    ok: true,
    asOf: ready.asOf,
    published: ready.published,
    range: ready.range,
    purplePixels: { latest: latestPurple.count, holiday: holidayPurple.count, tallestColumn: latestPurple.tallest },
    tooltip: tooltip.text.replaceAll("\n", " | "),
    desktop: desktopShot,
    holiday: holidayShot,
    mobile: mobileShot,
    screenshots: [desktopShot, holidayShot, mobileShot],
  }, null, 2));
} finally {
  await cleanup();
}

async function screenshot(name) {
  const metrics = await send("Page.getLayoutMetrics");
  const width = Math.ceil(metrics.cssContentSize.width);
  const height = Math.ceil(metrics.cssContentSize.height);
  const shot = await send("Page.captureScreenshot", {
    format: "png", captureBeyondViewport: true, fromSurface: true,
    clip: { x: 0, y: 0, width, height, scale: 1 },
  });
  const file = path.join(artifacts, name);
  await writeFile(file, Buffer.from(shot.data, "base64"));
  return file;
}
