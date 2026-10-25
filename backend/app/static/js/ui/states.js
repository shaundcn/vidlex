// 加载中、无数据、出错三种页面状态，全站统一用这里生成（技术选型第 5 节、12.4）。

import { h, replace } from "../core/dom.js";
import { button } from "./button.js";

function loading(text = "加载中") {
  return h("div", { class: "state", role: "status", "aria-busy": "true" }, h("p", { class: "state-title" }, text));
}

export function empty({ title, text, action }) {
  return h("div", { class: "state" },
    h("p", { class: "state-title" }, title),
    text ? h("p", { class: "state-text" }, text) : null,
    action ? button({ text: action.label, onClick: action.onClick }) : null);
}

export function failure({ title = "读取失败", text, retry }) {
  return h("div", { class: "state state-error", role: "alert" },
    h("p", { class: "state-title" }, title),
    text ? h("p", { class: "state-text" }, text) : null,
    retry ? button({ text: "重试", onClick: retry }) : null);
}

// 读取超过 300 毫秒还没返回才显示“加载中”，避免一闪而过（12.5）
export function loadingAfter(container, ms = 300) {
  const timer = setTimeout(() => replace(container, loading()), ms);
  return () => clearTimeout(timer);
}
