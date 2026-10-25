// 主按钮、描边按钮（技术选型 12.4）：点击后自动转圈、防连点，操作结束后恢复。
//   button({ text: "保存", variant: "primary", onClick: async () => {...} })

import { h } from "../core/dom.js";
import { icon } from "../core/icons.js";

export function button({ text, variant = "outline", size = "md", label, onClick, attrs = {} }) {
  const el = h("button", {
    type: "button",
    class: `btn btn-${variant}${size === "sm" ? " btn-sm" : ""}`,
    "aria-label": label,
    ...attrs,
  }, h("span", { class: "btn-label" }, text));
  if (onClick) el.addEventListener("click", () => run(el, onClick));
  return el;
}

// 执行期间显示转圈、不能再点；连点只会触发一次
export async function run(el, action) {
  if (el.getAttribute("aria-busy") === "true") return;
  setBusy(el, true);
  try {
    await action();
  } finally {
    setBusy(el, false);
  }
}

// 转圈图标放在左内边距里（见 base.css），按钮宽度不变。列表重画时用它把“执行中”带到新按钮上
export function setBusy(el, busy) {
  el.querySelector(".btn-spinner")?.remove();
  if (busy) {
    el.setAttribute("aria-busy", "true");
    el.prepend(icon("loader-circle", "icon spin btn-spinner"));
  } else {
    el.removeAttribute("aria-busy");
  }
}
