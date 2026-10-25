// 任务列表的排序、筛选、搜索（立项 4.3）：不碰页面的纯函数，测试见 tests/js/tasklist.test.mjs。

import { STATUS_ORDER, collator, displayName } from "./format.js";

// 第一次点“名称”“状态”是正序，第一次点“时间”是倒序
export const COLUMNS = [
  { key: "name", label: "名称", firstDir: "asc" },
  { key: "time", label: "时间", firstDir: "desc" },
  { key: "status", label: "状态", firstDir: "asc" },
];

// 再点同一列反向；点另一列从该列的默认方向开始
export function nextSort(sort, key) {
  return sort.key === key
    ? { key, dir: sort.dir === "asc" ? "desc" : "asc" }
    : { key, dir: COLUMNS.find((c) => c.key === key).firstDir };
}

export const HIDE_AFTER_MS = 30 * 24 * 60 * 60 * 1000;

// 只显示某种状态；搜索词（已转小写）同时匹配原文件名和新标题。
// hideOld：不显示 30 天前完成或忽略的任务（立项 4.3），有搜索词时不隐藏。返回 { rows, hidden }
export function filterTasks(tasks, { filter, query, hideOld = false, now = 0 }) {
  const rows = tasks.filter((t) => (!filter || t.status === filter) && (!query || matches(t, query)));
  if (!hideOld || query) return { rows, hidden: 0 };
  const kept = rows.filter((t) => !isOld(t, now));
  return { rows: kept, hidden: rows.length - kept.length };
}

// 失败、已停止、排队中、处理中的任务要用户处理或正在处理，从不隐藏
function isOld(task, now) {
  return (task.status === "completed" || task.status === "ignored") && now - task.updated_at > HIDE_AFTER_MS;
}

function matches(task, query) {
  return task.original_name.toLowerCase().includes(query) || (task.title ?? "").toLowerCase().includes(query);
}

// 名称按拼音、状态按固定顺序、时间按加入时间；值相同时按时间倒序
export function sortTasks(tasks, { key, dir }) {
  const sign = dir === "asc" ? 1 : -1;
  const primary = {
    name: (a, b) => collator.compare(displayName(a), displayName(b)),
    status: (a, b) => STATUS_ORDER.indexOf(a.status) - STATUS_ORDER.indexOf(b.status),
    time: (a, b) => a.created_at - b.created_at,
  }[key];
  return [...tasks].sort((a, b) => sign * primary(a, b) || b.created_at - a.created_at || b.id - a.id);
}
