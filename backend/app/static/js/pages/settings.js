// 设置页（立项第 5 节，技术选型 12.5）：API Key、文件夹、提示词、休眠、任务日志。
// 打开时一次读取全部设置；前 4 个区块分别保存，“保存”是每个区块唯一的主按钮。

import { api } from "../core/api.js";
import { keyError } from "../core/apikey.js";
import { $, h, replace } from "../core/dom.js";
import { formatTime } from "../core/format.js";
import { icon } from "../core/icons.js";
import { poll } from "../core/poll.js";
import { banner } from "../ui/banner.js";
import { button } from "../ui/button.js";
import { field } from "../ui/field.js";
import { toggleSwitch } from "../ui/switch.js";
import { failure, loadingAfter } from "../ui/states.js";
import { toast } from "../ui/toast.js";

const PROMPTS = [
  ["title", "新标题"],
  ["main_content", "主要内容"],
  ["highlight", "视频亮点"],
  ["transcript", "转写"],
];
// 接口返回的文件夹名 → 页面上的说明（四个放在一行）
const FOLDER_LABELS = { input: "视频输入", output: "笔记输出", ignored: "忽略", data: "运行数据" };
const LOG_PAGE = 100;

const main = $("#main");
const offline = banner($(".nav"));

load();

async function load() {
  const cancelLoading = loadingAfter(main);
  try {
    const settings = await api("/api/settings");
    cancelLoading();
    replace(main,
      h("h1", { class: "visually-hidden" }, "设置"),
      h("div", { class: "settings-grid" },
        apiKeyPanel(settings), foldersPanel(settings), promptsPanel(settings), sleepPanel(settings), logsPanel()));
  } catch (err) {
    cancelLoading();
    replace(main, failure({ text: err.message, retry: load }));
  }
}

function panel(title, ...content) {
  return h("section", { class: "panel" }, h("h2", { class: "panel-title" }, title), ...content);
}

function actions(...items) {
  return h("div", { class: "panel-actions" }, ...items);
}

// ---- API Key ----

// 已保存的 Key 以“开头 4 位 + * + 末尾 4 位”显示在输入框里。点进去时清空、改成密码框输入新 Key，
// 什么都没输入就离开时恢复显示打码值——打码文字本身永远不会被编辑后保存
function apiKeyPanel(settings) {
  const input = h("input", {
    class: "input", type: "password", autocomplete: "off", spellcheck: "false", placeholder: "填写方舟 API Key",
    "data-lpignore": "true", "data-1p-ignore": "true", // 密码管理器也不要填
  });
  const reveal = h("button", { type: "button", class: "btn btn-outline", "aria-label": "显示 API Key", "aria-pressed": "false" },
    icon("eye"));
  reveal.addEventListener("click", () => {
    const show = input.type === "password";
    input.type = show ? "text" : "password";
    reveal.setAttribute("aria-label", show ? "隐藏 API Key" : "显示 API Key");
    reveal.setAttribute("aria-pressed", String(show));
    replace(reveal, icon(show ? "eye-off" : "eye"));
  });
  let masked = null;
  const showSaved = (value) => {
    masked = value;
    input.value = value ?? "";
    input.type = value ? "text" : "password"; // 打码后的值直接可见
    input.placeholder = "填写方舟 API Key";
    reveal.hidden = Boolean(value);
  };
  showSaved(settings.api_key_masked);
  input.addEventListener("focus", () => {
    if (!masked || input.value !== masked) return;
    input.value = "";
    input.type = "password";
    input.placeholder = "输入新的 API Key";
    reveal.hidden = false;
    reveal.setAttribute("aria-pressed", "false");
    reveal.setAttribute("aria-label", "显示 API Key");
    replace(reveal, icon("eye"));
  });
  input.addEventListener("blur", (event) => {
    if (masked && !input.value && event.relatedTarget !== reveal) showSaved(masked); // 没输入新 Key：恢复打码值
  });
  const key = field({ label: "API Key", control: input, addon: reveal });
  const result = h("div", { class: "result", role: "status", hidden: true });

  return panel("API Key", key.el,
    actions(
      button({
        text: "保存", variant: "primary",
        onClick: async () => {
          key.clearError();
          const error = keyError(input.value, masked);
          if (error) return key.setError(error);
          try {
            const out = await api("/api/settings/api-key", { method: "PUT", body: { key: input.value.trim() } });
            showSaved(out.masked);
            toast("success", "API Key 已保存");
          } catch (err) {
            key.setError(err.message);
          }
        },
      }),
      button({
        text: "测试",
        onClick: async () => {
          showResult(result, null, "正在测试……通常十几秒");
          try {
            const out = await api("/api/settings/test", { method: "POST", timeout: 120000 });
            showResult(result, out.ok, out.message);
          } catch (err) {
            showResult(result, false, err.message);
          }
        },
      })),
    result);
}

// 结果固定显示在区块里，不用 Toast：成功蓝色对勾，失败红字原因
function showResult(el, ok, text) {
  el.className = `result${ok === true ? " result-ok" : ok === false ? " result-fail" : ""}`;
  replace(el, ok === true ? icon("check") : ok === false ? icon("circle-alert") : icon("loader-circle", "icon spin"), text);
  el.hidden = false;
}

// ---- 文件夹（只读） ----

function foldersPanel(settings) {
  return panel("文件夹", h("div", { class: "folder-grid" }, ...settings.folders.map((f) =>
    h("div", { class: "folder-item" },
      h("p", { class: "folder-name" }, `${FOLDER_LABELS[f.name] ?? f.name} / ${f.name}`),
      f.ok ? h("p", { class: "text-secondary" }, "可读写") : h("p", { class: "text-error" }, f.reason ?? "无法读写")))));
}

// ---- 提示词 ----

function promptsPanel(settings) {
  let saved = { ...settings.prompts };
  const fields = Object.fromEntries(PROMPTS.map(([name, label]) => {
    const control = h("textarea", { class: "textarea", value: saved[name], maxlength: "2000" });
    const f = field({ label, control });
    control.addEventListener("blur", () => check(f));
    control.addEventListener("input", updateDirty);
    return [name, f];
  }));
  const unsaved = h("span", { class: "unsaved", hidden: true }, "未保存");
  const values = () => Object.fromEntries(PROMPTS.map(([name]) => [name, fields[name].control.value]));
  const isDirty = () => PROMPTS.some(([name]) => fields[name].control.value !== saved[name]);
  function updateDirty() {
    unsaved.hidden = !isDirty();
  }
  // 有改动时关闭页面，出现浏览器自带的提醒
  window.addEventListener("beforeunload", (event) => {
    if (!isDirty()) return;
    event.preventDefault();
    event.returnValue = ""; // 部分浏览器只认这个
  });

  return panel("提示词",
    h("div", { class: "prompt-grid" }, ...PROMPTS.map(([name]) => fields[name].el)),
    actions(
      button({
        text: "保存", variant: "primary",
        onClick: async () => {
          const ok = PROMPTS.map(([name]) => check(fields[name])).every(Boolean);
          if (!ok) return;
          try {
            const out = await api("/api/settings/prompts", { method: "PUT", body: values() });
            saved = { ...out };
            for (const [name] of PROMPTS) fields[name].control.value = out[name];
            updateDirty();
            toast("success", "提示词已保存");
          } catch (err) {
            toast("error", err.message);
          }
        },
      }),
      button({
        text: "恢复默认",
        onClick: () => {
          // 只把默认内容填进输入框，不保存
          for (const [name] of PROMPTS) {
            fields[name].control.value = settings.default_prompts[name];
            fields[name].clearError();
          }
          updateDirty();
        },
      }),
      unsaved));
}

function check(f) {
  if (!f.control.value.trim()) {
    f.setError("这一项不能为空，请写明 AI 在这一部分要写什么");
    return false;
  }
  f.clearError();
  return true;
}

// ---- 休眠 ----

function sleepPanel(settings) {
  const toggle = toggleSwitch({
    text: "空闲时自动休眠，降低扫描频率", checked: settings.sleep.enabled, onChange: () => syncDisabled(),
  });
  const minutes = numberField("休眠倒计时", "分钟", settings.sleep.idle_minutes, 5, 1440);
  const hours = numberField("休眠扫描间隔", "小时", settings.sleep.interval_hours, 1, 24);
  const syncDisabled = () => {
    const off = !toggle.checked();
    minutes.control.disabled = off; // 开关关闭时两个框不能填
    hours.control.disabled = off;
    if (off) {
      minutes.clearError();
      hours.clearError();
    }
  };
  syncDisabled();

  return panel("休眠",
    h("div", { class: "field" }, toggle.el),
    h("div", { class: "field-pair" }, minutes.el, hours.el),
    actions(button({
      text: "保存", variant: "primary",
      onClick: async () => {
        const enabled = toggle.checked();
        if (enabled && ![minutes.check(), hours.check()].every(Boolean)) return;
        try {
          await api("/api/settings/sleep", {
            method: "PUT",
            body: { enabled, idle_minutes: Number(minutes.control.value), interval_hours: Number(hours.control.value) },
          });
          toast("success", "休眠设置已保存");
        } catch (err) {
          toast("error", err.message);
        }
      },
    })));
}

// 只能填 min–max 的整数；离开输入框或点保存时检查，超出范围时在框下方提示允许的范围
function numberField(label, unit, value, min, max) {
  const control = h("input", {
    class: "input input-number", type: "number", inputmode: "numeric", step: "1", min: String(min), max: String(max),
    value: String(value),
  });
  const f = field({ label, control, unit });
  f.check = () => {
    const text = control.value.trim();
    const n = Number(text);
    if (!/^\d+$/.test(text) || n < min || n > max) {
      f.setError(`只能填 ${min}–${max} 的整数`);
      return false;
    }
    f.clearError();
    return true;
  };
  control.addEventListener("blur", () => !control.disabled && f.check());
  return f;
}

// ---- 任务日志 ----

function logsPanel() {
  const select = h("select", { class: "select" }, h("option", { value: "" }, "全部任务"));
  const filter = field({ label: "按任务筛选", control: select });
  const list = h("div", { class: "logs" });
  const foot = h("div", { class: "list-foot" });
  const logs = { items: [], hasMore: false, taskId: "", loaded: false };

  async function loadOptions() {
    const tasks = await api("/api/logs/tasks");
    const current = select.value;
    replace(select, h("option", { value: "" }, "全部任务"),
      ...tasks.map((t) => h("option", { value: String(t.task_id) }, `${t.task_name}（#${t.task_id}）`)));
    select.value = tasks.some((t) => String(t.task_id) === current) ? current : "";
  }

  function query(params) {
    const search = new URLSearchParams({ limit: String(LOG_PAGE), ...params });
    if (logs.taskId) search.set("task_id", logs.taskId);
    return api(`/api/logs?${search}`);
  }

  function render() {
    replace(list, ...logs.items.map((item) =>
      h("div", { class: `log${item.level === "error" ? " log-error" : ""}` },
        h("span", { class: "log-time" }, formatTime(item.ts, true)),
        h("span", { class: "log-task" }, item.task_name),
        h("span", { class: "log-message" }, item.message))));
    if (logs.items.length === 0) {
      replace(foot, "还没有日志");
    } else if (logs.hasMore) {
      replace(foot, button({ text: "加载更多", onClick: loadOlder }));
    } else {
      replace(foot, "没有更多了");
    }
  }

  async function reload() {
    const page = await query({});
    logs.items = page.items; // 最新的在上
    logs.hasMore = page.has_more;
    render();
  }

  async function loadOlder() {
    const oldest = logs.items.at(-1);
    try {
      const page = await query({ before_id: String(oldest.id) });
      logs.items.push(...page.items);
      logs.hasMore = page.has_more;
      render();
    } catch (err) {
      toast("error", err.message);
    }
  }

  // 每 5 秒只取比当前最新一条更新的日志，插到顶部
  async function fetchNewer() {
    if (!logs.loaded) {
      await Promise.all([reload(), loadOptions()]);
      logs.loaded = true;
      return;
    }
    let newest = logs.items[0]?.id ?? 0;
    const added = [];
    for (;;) {
      const page = await query({ after_id: String(newest) }); // 从旧到新
      added.push(...page.items);
      if (!page.has_more || page.items.length === 0) break;
      newest = page.items.at(-1).id;
    }
    if (added.length) {
      logs.items.unshift(...added.reverse());
      render();
      await loadOptions();
    }
  }

  select.addEventListener("change", async () => {
    logs.taskId = select.value;
    try {
      await reload();
    } catch (err) {
      toast("error", err.message);
    }
  });

  poll(fetchNewer, {
    interval: 5000,
    maxInterval: 15000,
    onError: () => offline.show("暂时连不上 vidlex，正在重试"),
    onRecover: () => offline.hide(),
  });

  return panel("任务日志", filter.el, list, foot);
}
