// 任务页（立项第 4 节，技术选型 12.5）：顶栏显示队列状态 → 5 个数字卡片 → 任务列表（搜索框、暂停/继续按钮）。
// 每 3 秒取摘要，列表版本号变了才重新拉取完整列表；数据变了整块重画（state → render）。

import { api } from "../core/api.js";
import { $, h, replace } from "../core/dom.js";
import {
  ACTION_NAMES, CARD_STATUSES, QUEUE_STATES, STATUS_NAMES, STEP_NAMES, displayName, formatTime,
} from "../core/format.js";
import { icon } from "../core/icons.js";
import { poll } from "../core/poll.js";
import { COLUMNS, filterTasks, nextSort, sortTasks } from "../core/tasklist.js";
import { banner } from "../ui/banner.js";
import { button, setBusy } from "../ui/button.js";
import { empty, failure, loadingAfter } from "../ui/states.js";
import { tag } from "../ui/tag.js";
import { toast } from "../ui/toast.js";

const PAGE_SIZE = 200;
const HIDE_OLD_KEY = "vidlex.hideOld";

// 浏览器记住“隐藏 30 天前完成的任务”；读写失败（隐私模式等）时按关闭处理
function loadHideOld() {
  try {
    return localStorage.getItem(HIDE_OLD_KEY) === "1";
  } catch {
    return false;
  }
}

function saveHideOld(on) {
  try {
    localStorage.setItem(HIDE_OLD_KEY, on ? "1" : "0");
  } catch {
    // 记不住只影响下次打开页面
  }
}

const state = {
  summary: null,
  tasks: [],
  listVersion: null,
  loaded: false,
  filter: null, // 只显示某种状态
  query: "", // 搜索文件名（小写），同时匹配原文件名和新标题
  hideOld: loadHideOld(), // 隐藏过时任务：30 天前完成或忽略的
  sort: { key: "time", dir: "desc" }, // 刷新页面后恢复默认排序
  shown: PAGE_SIZE,
  busy: new Set(), // 正在执行的操作 "任务id:操作"，列表重画后仍保持转圈、不能再点
};

const main = $("#main");
const offline = banner($(".nav"));
const navStatusEl = h("p", { class: "nav-status", role: "status" });
$(".nav-logo").after(navStatusEl);
const cardsEl = h("div", { class: "cards", role: "group", "aria-label": "按状态筛选" });
// 搜索框只建一次，列表重画时不替换，输入法组字和焦点不会被打断
const searchEl = h("input", {
  class: "input", type: "search", placeholder: "搜索文件名", "aria-label": "搜索文件名",
  spellcheck: "false",
});
searchEl.addEventListener("input", () => {
  state.query = searchEl.value.trim().toLowerCase();
  state.shown = PAGE_SIZE;
  renderList();
});
const notesEl = h("div", { class: "list-notes" });
// “过时任务”和队列按钮同样大小；按下表示隐藏。和搜索框一样只建一次
const hideOldEl = button({
  text: "过时任务", size: "sm",
  attrs: { "data-focus": "hide-old", title: "隐藏 30 天前完成或忽略的任务" },
  onClick: () => setHideOld(!state.hideOld),
});
const queueCtlEl = h("div");
// 一行：标题、队列按钮、过时任务，搜索框在最右侧；筛选和隐藏提示在下一行，没有提示时不占位
const headEl = h("div", { class: "section-head" },
  h("div", { class: "list-head-row" },
    h("h2", { class: "title-md" }, "任务列表"), queueCtlEl, hideOldEl,
    h("div", { class: "search" }, icon("search", "icon search-icon"), searchEl)),
  notesEl);
showHideOld();
const listEl = h("div");

const cancelLoading = loadingAfter(main);
const poller = poll(refresh, {
  interval: 3000,
  maxInterval: 15000,
  onError(err) {
    if (state.loaded) {
      offline.show("暂时连不上 vidlex，正在重试"); // 已有数据：保留旧列表
    } else {
      cancelLoading();
      replace(main, failure({ text: err.message, retry: () => poller.now() }));
    }
  },
  onRecover: () => offline.hide(),
});

async function refresh() {
  const summary = await api("/api/summary");
  const listChanged = summary.list_version !== state.listVersion;
  const tasks = listChanged ? await api("/api/tasks") : null;
  const summaryChanged = JSON.stringify(summary) !== JSON.stringify(state.summary);
  state.summary = summary;
  if (listChanged) {
    state.tasks = tasks;
    state.listVersion = summary.list_version;
  }
  if (!state.loaded) {
    state.loaded = true;
    cancelLoading();
    replace(main, cardsEl, h("section", { class: "section-list" }, headEl, listEl));
  }
  if (summaryChanged || listChanged) render(listChanged);
}

// ---- 渲染 ----

function render(listChanged = true) {
  const focusKey = document.activeElement?.dataset?.focus; // 整块重画后把焦点还给同一个按钮
  renderQueue();
  renderCards();
  if (listChanged) renderList();
  if (focusKey) main.querySelector(`[data-focus="${CSS.escape(focusKey)}"]`)?.focus();
}

function renderQueue() {
  const { queue_state: queueState, error_message: errorMessage } = state.summary;
  const info = QUEUE_STATES[queueState];
  const dot = queueState === "running" ? "dot-running" : queueState === "error" ? "dot-error" : "";
  const text = queueState === "error" ? `${info.name}：${errorMessage}` : info.name;
  navStatusEl.className = `nav-status${queueState === "error" ? " text-error" : ""}`;
  navStatusEl.title = queueState === "error" ? errorMessage : info.desc; // 说明文字放在悬停提示里
  replace(navStatusEl,
    h("span", { class: `dot ${dot}`, "aria-hidden": "true" }), h("span", { class: "nav-status-text" }, text));
  replace(queueCtlEl,
    button({
      text: info.label,
      size: "sm",
      attrs: { "data-focus": "queue" },
      onClick: async () => {
        try {
          await api(`/api/queue/${info.action}`, { method: "POST" });
        } catch (err) {
          toast("error", err.message);
        }
        await poller.now();
      },
    }));
}

function renderCards() {
  replace(cardsEl, ...CARD_STATUSES.map((status) =>
    h("button", {
      type: "button",
      class: "card",
      "aria-pressed": String(state.filter === status),
      "data-focus": `card:${status}`,
      onclick: () => setFilter(state.filter === status ? null : status),
    },
    h("span", { class: "card-name" }, STATUS_NAMES[status]),
    h("span", { class: "card-value" }, state.summary.counts[status].toLocaleString("zh-CN")))));
}

function renderList() {
  const { rows: matched, hidden } = filterTasks(state.tasks, {
    filter: state.filter, query: state.query, hideOld: state.hideOld, now: Date.now(),
  });
  const rows = sortTasks(matched, state.sort);
  replace(notesEl,
    state.filter ? note(`仅显示：${STATUS_NAMES[state.filter]}`, "show-all", () => setFilter(null)) : null,
    hidden > 0
      ? note(`已隐藏 ${hidden.toLocaleString("zh-CN")} 个过时任务（30 天前完成或忽略）`, "show-old", () => setHideOld(false))
      : null);
  notesEl.hidden = !state.filter && hidden === 0;

  if (state.tasks.length === 0) {
    replace(listEl, empty({
      title: "还没有任务",
      text: "把 MP4、MOV 放进视频库的 input 文件夹，大约 6 分钟后会出现在这里",
    }));
    return;
  }
  if (rows.length === 0 && hidden > 0) {
    replace(listEl, empty({
      title: "过时任务都已隐藏",
      action: { label: "显示全部", onClick: () => setHideOld(false) },
    }));
    return;
  }
  if (rows.length === 0) {
    replace(listEl, state.query
      ? empty({ title: `没有文件名包含「${searchEl.value.trim()}」的任务`, action: { label: "清空搜索", onClick: clearSearch } })
      : empty({
        title: `没有「${STATUS_NAMES[state.filter]}」的任务`,
        action: { label: "显示全部", onClick: () => setFilter(null) },
      }));
    return;
  }
  const visible = rows.slice(0, state.shown);
  const rest = rows.length - visible.length;
  replace(listEl,
    h("div", { class: "table-wrap" },
      h("table", { class: "table" },
        h("caption", { class: "visually-hidden" }, "任务列表"),
        h("thead", {}, h("tr", {}, ...COLUMNS.map(headerCell), h("th", { scope: "col" }, "操作"))),
        h("tbody", {}, ...visible.map(taskRow)))),
    h("div", { class: "list-foot" }, rest > 0
      ? button({
        text: `显示更多（还有 ${rest.toLocaleString("zh-CN")} 条）`,
        attrs: { "data-focus": "more" },
        onClick: () => {
          state.shown += PAGE_SIZE;
          renderList();
        },
      })
      : `已显示全部 ${rows.length.toLocaleString("zh-CN")} 条`));
}

function headerCell({ key, label }) {
  const active = state.sort.key === key;
  const sortName = active ? (state.sort.dir === "asc" ? "ascending" : "descending") : "none";
  const arrow = active ? (state.sort.dir === "asc" ? "arrow-up" : "arrow-down") : "arrow-up-down";
  return h("th", { scope: "col", "aria-sort": sortName },
    h("button", { type: "button", class: "sort-btn", "data-focus": `sort:${key}`, onclick: () => setSort(key) },
      label, icon(arrow)));
}

function taskRow(task) {
  const name = task.status === "completed"
    ? [h("p", { class: "cell-title" }, displayName(task)), h("p", { class: "text-small" }, task.original_name)]
    : [task.original_name];
  const status = [tag(task.status)];
  if (task.status === "processing" && task.step) {
    status.push(h("span", { class: "text-secondary" }, `当前步骤：${STEP_NAMES[task.step] ?? task.step}`));
  }
  if (task.status === "failed" && task.fail_reason) {
    status.push(h("span", { class: "text-error" }, task.fail_reason));
  }
  return h("tr", {},
    h("td", { class: "cell-name" }, ...name),
    h("td", { class: "cell-time" }, formatTime(task.created_at)),
    h("td", { class: "cell-status" },
      h("div", { class: "status-line" }, ...status),
      ...[audioOnlyNote(task), task.notice].filter(Boolean).map((text) =>
        h("p", { class: "status-note text-secondary" }, text))),
    h("td", { class: "cell-actions" }, h("div", { class: "btn-group" },
      ...task.allowed_actions.map((action) => actionButton(task, action)),
      h("a", {
        class: "btn btn-outline btn-sm",
        href: `view.html?task=${task.id}`,
        target: "_blank",
        rel: "noopener noreferrer",
        "aria-label": `查看 ${displayName(task)}（在新标签页打开）`,
      }, "查看", icon("external-link")))));
}

// 超过 512MB 只传了音频（立项第 3 节）：处理中和已完成时显示；重新排队后要等下次开始处理才知道
function audioOnlyNote(task) {
  return task.analysis_mode === "audio" && ["processing", "completed"].includes(task.status)
    ? "超过 512MB，只用音频分析" : null;
}

function actionButton(task, action) {
  const key = `${task.id}:${action}`;
  // 已完成任务的“开始”是重新处理（立项 4.3）
  const text = task.status === "completed" && action === "start" ? "重新处理" : ACTION_NAMES[action];
  const el = button({ text, size: "sm", attrs: { "data-focus": key } });
  if (state.busy.has(key)) setBusy(el, true);
  el.addEventListener("click", () => act(task, action, key));
  return el;
}

// 被点的按钮转圈且不能再点；成功后立即刷新；失败时 Toast 显示原因，该行同时显示后端记下的操作提示
async function act(task, action, key) {
  if (state.busy.has(key)) return;
  state.busy.add(key);
  renderList();
  try {
    await api(`/api/tasks/${task.id}/${action}`, { method: "POST" });
  } catch (err) {
    toast("error", err.message);
  } finally {
    state.busy.delete(key);
    state.listVersion = null; // 无论成败都重新拉列表（失败时也要显示操作提示）
    await poller.now();
    renderList();
  }
}

// ---- 排序、筛选、搜索（规则在 core/tasklist.js） ----

function clearSearch() {
  searchEl.value = "";
  state.query = "";
  state.shown = PAGE_SIZE;
  renderList();
  searchEl.focus();
}

// “仅显示：失败 · 显示全部”这类一行提示
function note(text, focusKey, onShowAll) {
  return h("p", { class: "text-secondary" }, `${text} · `,
    h("button", { type: "button", class: "btn-text", "data-focus": focusKey, onclick: onShowAll }, "显示全部"));
}

// 按下时图标是“眼睛划掉”，读屏软件读作“过时任务，已按下”
function showHideOld() {
  hideOldEl.setAttribute("aria-pressed", String(state.hideOld));
  // 只换眼睛图标：点击时按钮前面还有转圈图标，它的 class 也含 icon
  hideOldEl.querySelector(".hide-old-icon")?.remove();
  hideOldEl.append(icon(state.hideOld ? "eye-off" : "eye", "icon hide-old-icon"));
}

function setHideOld(on) {
  state.hideOld = on;
  showHideOld();
  saveHideOld(on);
  state.shown = PAGE_SIZE;
  renderList();
}

function setFilter(status) {
  state.filter = status;
  state.shown = PAGE_SIZE; // 筛选或排序改变时回到前 200 行
  render();
}

function setSort(key) {
  state.sort = nextSort(state.sort, key);
  state.shown = PAGE_SIZE;
  render();
}
