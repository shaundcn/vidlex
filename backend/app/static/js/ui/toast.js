// 右下角提示（技术选型 12.4）：最多同时 3 条；成功 3 秒、失败 6 秒并带关闭按钮；鼠标停在上面时暂停倒计时。

import { h } from "../core/dom.js";
import { icon } from "../core/icons.js";

const MAX = 3;
const DURATION = { success: 3000, error: 6000 };
let region = null;

function container() {
  if (!region) {
    region = h("div", { class: "toasts", "aria-live": "polite", role: "status" });
    document.body.append(region);
  }
  return region;
}

export function toast(type, text) {
  const box = container();
  while (box.children.length >= MAX) box.firstElementChild.remove();
  const item = h("div", { class: `toast toast-${type}` },
    icon(type === "success" ? "check" : "circle-alert"),
    h("p", { class: "toast-text" }, text));
  if (type === "error") {
    item.append(h("button", { type: "button", class: "toast-close", "aria-label": "关闭提示", onclick: () => item.remove() },
      icon("x")));
  }
  box.append(item);
  let remaining = DURATION[type] ?? DURATION.success;
  let started = Date.now();
  let timer = setTimeout(() => item.remove(), remaining);
  item.addEventListener("mouseenter", () => {
    clearTimeout(timer);
    remaining -= Date.now() - started;
  });
  item.addEventListener("mouseleave", () => {
    started = Date.now();
    timer = setTimeout(() => item.remove(), Math.max(remaining, 1000));
  });
}
