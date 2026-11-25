import { displayWindow, fridayLabels } from "./window.js";

const picker = document.querySelector("#date");
const reset = document.querySelector("#latest");
const charts = document.querySelector("#charts");
const status = document.querySelector("#range");
const asOf = document.querySelector("#as-of");
const published = document.querySelector("#published");
const axisNumber = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 });
const metric = new Intl.NumberFormat("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const selectedColor = "#6b339e";
let records = [];
let selectedRange;
let views = [];

function tickScale(values, includeZero) {
  const min = includeZero ? Math.min(0, ...values) : Math.min(...values);
  const max = includeZero ? Math.max(0, ...values) : Math.max(...values);
  const span = Math.max(max - min, includeZero ? 1 : Math.max(1, max * .018));
  const unit = 10 ** Math.floor(Math.log10(span / 5));
  const nice = [1, 2, 2.5, 5, 10].map(n => n * unit).find(n => n >= span / 5);
  const step = includeZero ? nice : Math.max(1, Math.ceil(nice));
  const low = includeZero ? Math.floor(min / step) * step : Math.max(0, Math.floor((min - span * .04) / step) * step);
  const high = Math.max(low + step, Math.ceil((max + span * .04) / step) * step);
  return { low, high, step };
}

function paint(view, chartIndex) {
  const { rows, selected, selectedIndex } = selectedRange;
  const { canvas } = view;
  const width = canvas.clientWidth, height = canvas.clientHeight;
  if (width < 120 || !rows.length) return;
  const px = Math.min(devicePixelRatio || 1, 2);
  canvas.width = Math.round(width * px);
  canvas.height = Math.round(height * px);
  const ctx = canvas.getContext("2d");
  ctx.scale(px, px);
  const left = width < 410 ? 39 : 50;
  const right = width - (width < 410 ? 51 : 61);
  const dates = [...fridayLabels(rows)].filter(([, date]) => date !== selected);
  dates.push([selectedIndex, selected]);
  dates.sort(([first], [second]) => first - second);
  const dateFont = "14px system-ui, sans-serif";
  const selectedFont = `600 ${dateFont}`;
  const longestDate = Math.ceil(Math.max(...dates.map(([, date]) => {
    ctx.font = date === selected ? selectedFont : dateFont;
    return ctx.measureText(date).width;
  })));
  const dateOffset = 10;
  const top = 15, bottom = height - longestDate - dateOffset - 7;
  const step = (right - left) / rows.length;
  const x = i => left + (i + .5) * step;
  view.geometry = { left, right, top, bottom, step };
  const changes = rows.map(row => row.values[chartIndex][0]);
  const sizes = rows.map(row => row.values[chartIndex][1]);
  const l = tickScale(changes, true), r = tickScale(sizes, false);
  const y = (v, scale) => bottom - (v - scale.low) * (bottom - top) / (scale.high - scale.low);
  const baseline = y(0, l);
  const selectedX = x(selectedIndex);
  ctx.strokeStyle = "#445464";
  ctx.lineWidth = .85;
  ctx.beginPath();
  ctx.moveTo(left, top); ctx.lineTo(left, bottom);
  ctx.moveTo(right, top); ctx.lineTo(right, bottom);
  ctx.moveTo(left, baseline); ctx.lineTo(right, baseline);
  ctx.stroke();
  ctx.font = `${width < 410 ? 11 : 12}px system-ui, sans-serif`;
  ctx.fillStyle = "#253447";
  ctx.textBaseline = "middle";
  for (const [axis, isLeft] of [[l, true], [r, false]]) {
    ctx.textAlign = isLeft ? "right" : "left";
    for (let i = 0; i <= Math.round((axis.high - axis.low) / axis.step); i++) {
      const value = axis.low + i * axis.step;
      ctx.fillText(axisNumber.format(Math.abs(value) < 1e-8 ? 0 : value), isLeft ? left - 7 : right + 7, y(value, axis));
    }
  }
  const barWidth = Math.max(2, Math.min(14, step * .57));
  ctx.fillStyle = "#db292c";
  changes.forEach((value, i) => {
    const end = y(value, l);
    ctx.fillRect(x(i) - barWidth / 2, Math.min(baseline, end), barWidth, Math.abs(baseline - end));
  });
  ctx.strokeStyle = "#0861aa";
  ctx.lineWidth = 2.2;
  ctx.lineJoin = "round";
  ctx.beginPath();
  sizes.forEach((value, i) => i === 0 ? ctx.moveTo(x(i), y(value, r)) : ctx.lineTo(x(i), y(value, r)));
  ctx.stroke();
  ctx.beginPath();
  ctx.arc(selectedX, y(sizes[selectedIndex], r), 3.5, 0, 2 * Math.PI);
  ctx.fillStyle = "#fff";
  ctx.fill();
  ctx.strokeStyle = selectedColor;
  ctx.lineWidth = 1.8;
  ctx.stroke();

  ctx.lineWidth = 1;
  const labelSpacing = 20;
  const labelInset = 10;
  let labelX = labelInset - labelSpacing;
  for (const [position, [i, date]] of dates.entries()) {
    const tickX = x(i);
    const rightLimit = width - labelInset - (dates.length - position - 1) * labelSpacing;
    labelX = Math.min(rightLimit, Math.max(tickX, labelX + labelSpacing));
    const highlighted = date === selected;
    ctx.strokeStyle = highlighted ? selectedColor : "#8391a1";
    ctx.beginPath();
    ctx.moveTo(tickX, bottom);
    ctx.lineTo(labelX, bottom + dateOffset - 3);
    ctx.stroke();
    ctx.save();
    ctx.translate(labelX, bottom + dateOffset);
    ctx.rotate(-Math.PI / 2);
    ctx.textAlign = "right";
    ctx.font = highlighted ? selectedFont : dateFont;
    if (highlighted) {
      const length = ctx.measureText(date).width;
      ctx.fillStyle = "#f1e7fa";
      ctx.beginPath();
      ctx.roundRect(-length - 4, -9, length + 8, 18, 4);
      ctx.fill();
    }
    ctx.fillStyle = highlighted ? selectedColor : "#253447";
    ctx.fillText(date, 0, 0);
    ctx.restore();
  }
}

function selectDate(chosen) {
  selectedRange = displayWindow(records, chosen);
  const { selected, rows } = selectedRange;
  const selectedIndex = rows.findIndex(row => row.date === selected);
  selectedRange.selectedIndex = selectedIndex;
  const mapped = chosen === selected ? "" : ` · 对应交易日 ${selected}`;
  const description = `${rows[0].date} 至 ${rows.at(-1).date} · ${rows.length}个交易日${mapped}`;
  status.textContent = description;
  reset.disabled = chosen === records.at(-1).date;
  views.forEach((view, i) => {
    view.tooltip.hidden = true;
    view.focusIndex = selectedIndex;
    view.canvas.setAttribute("aria-label", `${view.index.name}，查询日期 ${chosen}，${description}，可用方向键查看数据点`);
    paint(view, i);
  });
}

function showValue(view, chartIndex, rowIndex, leftHint) {
  const row = selectedRange.rows[rowIndex];
  const [change, size] = row.values[chartIndex];
  const text = `${row.date}\n日度变动 ${change > 0 ? "+" : ""}${metric.format(change)} 亿元\nETF总规模 ${metric.format(size)} 亿元`;
  view.tooltip.textContent = text;
  view.tooltip.hidden = false;
  const room = view.canvas.clientWidth - view.tooltip.offsetWidth;
  view.tooltip.style.left = `${Math.max(2, Math.min(room - 3, leftHint + 11))}px`;
  view.tooltip.style.top = "3px";
  view.canvas.setAttribute("aria-label", `${view.index.name}，${text.replaceAll("\n", "，")}`);
}

function chartView(index, chartIndex) {
  const card = document.createElement("article");
  card.className = "chart";
  const title = document.createElement("h2");
  title.textContent = index.name;
  const plot = document.createElement("div");
  plot.className = "plot";
  const canvas = document.createElement("canvas");
  canvas.tabIndex = 0;
  const tooltip = document.createElement("div");
  tooltip.className = "tooltip";
  tooltip.hidden = true;
  plot.append(canvas, tooltip);
  card.append(title, plot);
  charts.append(card);
  const view = { index, canvas, tooltip, focusIndex: 0 };
  const pointer = (event) => {
    const geometry = view.geometry;
    const x = event.clientX - canvas.getBoundingClientRect().left;
    if (!geometry || x < geometry.left || x >= geometry.right) { tooltip.hidden = true; return; }
    const rowIndex = Math.min(selectedRange.rows.length - 1, Math.floor((x - geometry.left) / geometry.step));
    view.focusIndex = rowIndex;
    showValue(view, chartIndex, rowIndex, x);
  };
  canvas.addEventListener("pointermove", pointer);
  canvas.addEventListener("pointerdown", pointer);
  canvas.addEventListener("pointerleave", () => { tooltip.hidden = true; });
  canvas.addEventListener("blur", () => { tooltip.hidden = true; });
  canvas.addEventListener("keydown", (event) => {
    if (event.key === "Escape") { tooltip.hidden = true; return; }
    if (!view.geometry || !["ArrowLeft", "ArrowRight"].includes(event.key)) return;
    event.preventDefault();
    view.focusIndex = Math.max(0, Math.min(selectedRange.rows.length - 1, view.focusIndex + (event.key === "ArrowLeft" ? -1 : 1)));
    showValue(view, chartIndex, view.focusIndex, view.geometry.left + (view.focusIndex + .5) * view.geometry.step);
  });
  new ResizeObserver(() => { if (selectedRange) paint(view, chartIndex); }).observe(plot);
  return view;
}

// 同源只读数据：优先 /api/history（服务器映射到定时更新的 JSON），
// 静态托管未配置该映射时回退到同目录的 api/history.json。
const SOURCES = ["api/history", `api/history.json?v=${Date.now()}`];

async function readHistory() {
  const failures = [];
  for (const source of SOURCES) {
    try {
      const response = await fetch(new URL(source, document.baseURI), { cache: "no-store", credentials: "omit" });
      if (!response.ok) { failures.push(`${source}: HTTP ${response.status}`); continue; }
      const data = await response.json();
      if (!Array.isArray(data?.indices) || data.indices.length !== 8 || !Array.isArray(data?.records) || !data.records.length) {
        failures.push(`${source}: 数据结构不符`); continue;
      }
      return data;
    } catch (error) {
      failures.push(`${source}: ${error.message}`);
    }
  }
  throw new Error(`数据读取失败（${failures.join("；")}）`);
}

async function load() {
  const data = await readHistory();
  if (data.meta?.published_at) published.textContent = `更新于 ${data.meta.published_at}（北京时间）`;
  records = data.records;
  const latest = records.at(-1).date;
  const today = new Intl.DateTimeFormat("sv-SE", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date());
  picker.max = today < latest ? latest : today;
  asOf.textContent = latest;
  picker.disabled = false;
  picker.value = latest;
  views = data.indices.map(chartView);
  selectDate(latest);
  picker.addEventListener("change", () => { if (picker.value && picker.validity.valid) selectDate(picker.value); });
  reset.addEventListener("click", () => { picker.value = latest; selectDate(latest); });
}

load().catch((error) => {
  console.error(error);
  asOf.textContent = "暂不可用";
  status.textContent = "暂时无法读取历史数据，请稍后刷新页面。";
});
