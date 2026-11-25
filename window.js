const DAY = 86_400_000;
export const START = "2026-01-01";
const stamp = (date) => Date.parse(date + "T00:00:00Z");
const iso = (time) => new Date(time).toISOString().slice(0, 10);

export function displayWindow(records, chosenDate) {
  const lastDate = records.at(-1).date;
  const selected = records.findLast(row => row.date <= chosenDate)?.date ?? records[0].date;
  const latestStart = Math.max(stamp(START), stamp(lastDate) - 55 * DAY);
  const start = Math.min(latestStart, Math.max(stamp(START), stamp(selected) - 27 * DAY));
  const end = Math.min(stamp(lastDate), start + 55 * DAY);
  return { selected, start: iso(start), end: iso(end), rows: records.filter(row => stamp(row.date) >= start && stamp(row.date) <= end) };
}

export function fridayLabels(rows) {
  const labels = new Map();
  const first = stamp(rows[0].date);
  const last = stamp(rows.at(-1).date);
  for (let date = first; date <= last; date += DAY) {
    if (new Date(date).getUTCDay() !== 5) continue;
    const index = rows.findLastIndex(row => stamp(row.date) <= date);
    if (index >= 0) labels.set(index, iso(date));
  }
  return labels;
}
