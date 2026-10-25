import assert from "node:assert/strict";
import { test } from "node:test";

import { HIDE_AFTER_MS, filterTasks, nextSort, sortTasks } from "../../backend/app/static/js/core/tasklist.js";

const task = (id, fields = {}) => ({
  id, status: "queued", original_name: `${id}.mp4`, title: null, created_at: id * 1000, ...fields,
});
const ids = (tasks) => tasks.map((t) => t.id);

test("名称按拼音排序，已完成的按新标题", () => {
  const tasks = [
    task(1, { original_name: "张三.mp4" }),
    task(2, { status: "completed", title: "阿里", original_name: "z.mp4" }),
    task(3, { original_name: "白.mp4" }),
  ];
  assert.deepEqual(ids(sortTasks(tasks, { key: "name", dir: "asc" })), [2, 3, 1]);
  assert.deepEqual(ids(sortTasks(tasks, { key: "name", dir: "desc" })), [1, 3, 2]);
});

test("状态按固定顺序；同状态按时间倒序，时间也相同按编号倒序", () => {
  const tasks = [
    task(1, { status: "failed", created_at: 3000 }),
    task(2, { status: "queued" }),
    task(3, { status: "failed" }),
    task(4, { status: "processing" }),
    task(5, { status: "queued" }),
  ];
  assert.deepEqual(ids(sortTasks(tasks, { key: "status", dir: "asc" })), [5, 2, 4, 3, 1]);
});

test("时间倒序：后加入的在前", () => {
  assert.deepEqual(ids(sortTasks([task(1), task(3), task(2)], { key: "time", dir: "desc" })), [3, 2, 1]);
});

test("第一次点名称、状态是正序，第一次点时间是倒序；再点同一列反向", () => {
  assert.deepEqual(nextSort({ key: "time", dir: "desc" }, "name"), { key: "name", dir: "asc" });
  assert.deepEqual(nextSort({ key: "name", dir: "asc" }, "name"), { key: "name", dir: "desc" });
  assert.deepEqual(nextSort({ key: "name", dir: "desc" }, "time"), { key: "time", dir: "desc" });
  assert.deepEqual(nextSort({ key: "time", dir: "desc" }, "time"), { key: "time", dir: "asc" });
  assert.deepEqual(nextSort({ key: "time", dir: "desc" }, "status"), { key: "status", dir: "asc" });
});

test("搜索不分大小写，同时匹配原文件名和新标题；和状态筛选同时生效", () => {
  const tasks = [
    task(1, { original_name: "Vlog 01.MP4" }),
    task(2, { status: "completed", title: "旅行 VLOG", original_name: "a.mp4" }),
    task(3),
  ];
  const shown = (opts) => ids(filterTasks(tasks, opts).rows);
  assert.deepEqual(shown({ filter: null, query: "vlog" }), [1, 2]);
  assert.deepEqual(shown({ filter: "completed", query: "vlog" }), [2]);
  assert.deepEqual(shown({ filter: "queued", query: "" }), [1, 3]);
});

test("隐藏 30 天前完成或忽略的任务；失败、已停止、排队中、处理中从不隐藏", () => {
  const now = 100 * 24 * 3600 * 1000;
  const old = now - HIDE_AFTER_MS - 1;
  const tasks = [
    task(1, { status: "completed", updated_at: old }),
    task(2, { status: "ignored", updated_at: old }),
    task(3, { status: "completed", updated_at: now - HIDE_AFTER_MS }), // 正好 30 天：不隐藏
    ...["failed", "stopped", "queued", "processing"].map((status, i) => task(4 + i, { status, updated_at: old })),
  ];
  const { rows, hidden } = filterTasks(tasks, { filter: null, query: "", hideOld: true, now });
  assert.deepEqual(ids(rows), [3, 4, 5, 6, 7]);
  assert.equal(hidden, 2);
  assert.equal(filterTasks(tasks, { filter: null, query: "", hideOld: false, now }).hidden, 0);
  assert.equal(filterTasks(tasks, { filter: "completed", query: "", hideOld: true, now }).hidden, 1);
});

test("有搜索词时不隐藏", () => {
  const tasks = [task(1, { status: "completed", updated_at: 0, original_name: "旧视频.mp4" })];
  const out = filterTasks(tasks, { filter: null, query: "旧", hideOld: true, now: HIDE_AFTER_MS * 2 });
  assert.deepEqual([ids(out.rows), out.hidden], [[1], 0]);
});
