// 前端逻辑与数据一致性验收（Node.js 22+，无第三方依赖）。
// 运行：node tests/verify_frontend.mjs
import { readFile, readdir } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";
import assert from "node:assert/strict";
import { displayWindow, fridayLabels } from "../public/window.js";

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const readJSON = async (file) => JSON.parse(await readFile(path.join(root, file), "utf8"));
const published = await readJSON("public/api/history.json");
const internal = await readJSON("data/current/history.json");
const seed = await readJSON("data/history.json");
const records = published.records;
const iso = (date) => new Date(date + "T00:00:00Z");
const day = 86_400_000;
let checks = 0;
const ok = (condition, message) => { assert.ok(condition, message); checks += 1; };
const eq = (actual, expected, message) => { assert.deepEqual(actual, expected, message); checks += 1; };

// ---- 1. 数据完整性与口径
eq(published.indices, seed.indices, "对外 JSON 的八指数顺序应与已验收快照一致");
ok(records.length >= 514, `记录数应不少于 514，实际 ${records.length}`);
eq(records[0].date, "2024-10-15", "历史起点应为 2024-10-15（八个指数均有 ETF 份额的最早交易日）");
ok(records.at(-1).date <= new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Shanghai" }).format(new Date()), "最新数据日不应晚于北京时间当天");
ok(!("last_members" in published), "对外 JSON 不应泄露 last_members 内部字段");
eq(published.meta.record_count, records.length, "meta.record_count 应与记录数一致");
eq(published.meta.through, records.at(-1).date, "meta.through 应等于最新数据日");
eq(internal.records, records, "内部快照与对外 JSON 的 records 应完全一致");
ok(Object.keys(internal.last_members).length === 8, "内部快照应保留 8 个指数的 last_members");
for (let i = 0; i < records.length; i += 1) {
  const row = records[i];
  ok(/^\d{4}-\d{2}-\d{2}$/.test(row.date), `${row.date} 日期格式`);
  ok(iso(row.date).getUTCDay() % 6 !== 0, `${row.date} 不应是周末`);
  ok(row.values.length === 8, `${row.date} 应有 8 组指标`);
  row.values.forEach(([change, size], index) => {
    ok(Number.isFinite(change) && Number.isFinite(size) && size > 0, `${row.date} 第${index + 1}组指标应为有限正数`);
    if (i > 0) {
      const previousSize = records[i - 1].values[index][1];
      ok(Math.abs(change - (size - previousSize)) < 1e-6, `${row.date} 第${index + 1}组变动应等于相邻规模差`);
    }
  });
  if (i > 0) ok(row.date > records[i - 1].date, `${row.date} 日期递增`);
}

// ---- 2. 扩展后的查询样例（窗口边界 + 图内首末数据日 + 数据点数）
const cases = [
  { input: "2024-10-14", selected: "2024-10-15", start: "2024-10-15", end: "2024-12-09", first: "2024-10-15", last: "2024-12-09", rows: 40 },
  { input: "2025-01-02", selected: "2025-01-02", start: "2024-12-06", end: "2025-01-30", first: "2024-12-06", last: "2025-01-27", rows: 36 },
  { input: "2026-01-01", selected: "2025-12-31", start: "2025-12-04", end: "2026-01-28", first: "2025-12-04", last: "2026-01-28", rows: 38 },
  { input: "2026-07-24", selected: "2026-07-24", start: "2026-06-27", end: "2026-08-21", first: "2026-06-29", last: "2026-08-21", rows: 40 },
];
for (const item of cases) {
  const window = displayWindow(records, item.input);
  eq(window.selected, item.selected, `${item.input} 应映射到 ${item.selected}`);
  eq(window.start, item.start, `${item.input} 窗口起点`);
  eq(window.end, item.end, `${item.input} 窗口终点`);
  eq(window.rows[0].date, item.first, `${item.input} 图内首个数据日`);
  eq(window.rows.at(-1).date, item.last, `${item.input} 图内末个数据日`);
  eq(window.rows.length, item.rows, `${item.input} 交易日数量`);
}

// ---- 3. 休市日、周末、边界映射
const latestDate = records.at(-1).date;
const nextDay = new Date(`${latestDate}T00:00:00Z`);
nextDay.setUTCDate(nextDay.getUTCDate() + 1);
const afterLatest = nextDay.toISOString().slice(0, 10);
const mappings = [
  ["2026-10-03", "2026-09-30"], // 国庆假期内的周六
  ["2026-10-06", "2026-09-30"], // 国庆假期内的周二（休市）
  ["2026-11-21", "2026-11-20"], // 周六
  ["2026-11-22", "2026-11-20"], // 周日
  ["2024-10-14", "2024-10-15"], // 数据起点之前
  [afterLatest, latestDate], // 尚无数据的最新日期
];
for (const [input, expected] of mappings) {
  const window = displayWindow(records, input);
  eq(window.selected, expected, `${input} 应映射到 ${expected}`);
  ok(window.start >= "2024-10-15", `${input} 窗口不应早于 2024-10-15`);
  const first = iso(window.start), last = iso(window.end);
  ok(last - first <= 55 * day, `${input} 窗口应不超过 56 个自然日`);
  ok(window.rows.every((row) => row.date >= window.start && row.date <= window.end), `${input} 窗口内记录应在区间内`);
  ok(window.rows.some((row) => row.date === window.selected), `${input} 查询日应落在窗口内`);
}

// ---- 4. 所有数据日都能得到包含自身的窗口，且靠近最新边界时向左侧补足
for (const row of records) {
  const window = displayWindow(records, row.date);
  ok(window.selected === row.date, `${row.date} 选中自身`);
  ok(window.rows.at(-1).date <= window.end, `${row.date} 窗口最后一笔不应晚于窗口终点`);
  ok(window.rows[0].date >= window.start, `${row.date} 窗口第一笔不应早于窗口起点`);
  if (window.end >= records.at(-1).date) ok(window.rows.at(-1).date === records.at(-1).date, `${row.date} 触到最新边界时应包含最新交易日`);
  ok(window.rows.length <= 41, `${row.date} 56 个自然日内的交易日不应超过 41 个`);
}
const latestWindow = displayWindow(records, records.at(-1).date);
ok(latestWindow.rows.length > 20, "最新窗口应向左补足到约 56 个自然日");

// ---- 5. 周五刻度：完整日期、周五休市锚定到前一交易日、重叠只留一个
let fridayCount = 0;
for (const sample of ["2026-01-05", "2026-07-24", "2026-11-24"]) {
  const window = displayWindow(records, sample);
  const labels = fridayLabels(window.rows);
  let previousIndex = -1;
  for (const [index, label] of [...labels.entries()].sort((a, b) => a[0] - b[0])) {
    ok(iso(label).getUTCDay() === 5, `${label} 应是周五`);
    ok(index > previousIndex, `${label} 刻度位置应递增`);
    ok(index >= 0 && index < window.rows.length, `${label} 刻度应落在窗口内`);
    ok(window.rows[index].date <= label, `${label} 应锚定到不晚于该周五的交易日`);
    ok(/^\d{4}-\d{2}-\d{2}$/.test(label), `${label} 应为完整日期`);
    previousIndex = index;
    fridayCount += 1;
  }
  ok(labels.size >= 5, `${sample} 窗口内应至少有 5 个周五刻度`);
}
ok(fridayCount > 0, "周五刻度应被绘制");

// 国庆整周休市：相邻两个周五映射到同一交易日时只保留一个刻度
const holiday = displayWindow(records, "2026-10-08");
const holidayLabels = fridayLabels(holiday.rows);
ok(new Set(holidayLabels.keys()).size === holidayLabels.size, "同一位置不应出现两个周五刻度");

// ---- 6. 页面只依赖同源资源，无海外 CDN / 第三方脚本
const publicFiles = await readdir(path.join(root, "public"));
const sourceFiles = publicFiles.filter((name) => /\.(html|css|js)$/.test(name));
for (const file of sourceFiles) {
  const text = await readFile(path.join(root, "public", file), "utf8");
  const hosts = [...text.matchAll(/https?:\/\/([^/"'\s)]+)/g)].map((match) => match[1]);
  const allowed = /^(?:[a-z0-9-]+\.)*(sse\.com\.cn|szse\.cn|eastmoney\.com|csindex\.com\.cn|cnindex\.com\.cn)$/;
  ok(hosts.every((host) => allowed.test(host)), `${file} 不应引用第三方资源：${hosts.filter((host) => !allowed.test(host)).join(", ")}`);
  ok(!/<script[^>]+src="(?!\.\/)/.test(text), `${file} 脚本应使用相对路径`);
}

// ---- 7. 页面底部的数据来源与风险提示
const html = await readFile(path.join(root, "public/index.html"), "utf8");
ok(html.includes("数据来源") && html.includes("风险提示"), "页面应包含数据来源和风险提示两个说明");
ok(html.includes("仅为客观数据展示，仅供内部使用，不构成投资建议"), "风险提示应包含完整表述");
ok(html.includes("上海证券交易所") && html.includes("深圳证券交易所") && html.includes("东方财富"), "数据来源应列出实际数据源");

console.log(`前端逻辑与数据验收通过：${checks} 项检查，${records.length} 个交易日，${records[0].date} 至 ${records.at(-1).date}`);
