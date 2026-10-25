// 标签 + 输入控件 + 说明 + 单位 + 错误文字（技术选型 12.4）。错误不只靠红框，还有文字和图标。
// addon：放在输入框同一行右侧的元素（例如“显示/隐藏”按钮）；help 可以是文字或元素（内容可后续更新）。

import { h, replace } from "../core/dom.js";
import { icon } from "../core/icons.js";

let seq = 0;

export function field({ label, control, help, unit, addon }) {
  const id = control.id || `f${++seq}`;
  control.id = id;
  const helpEl = help ? h("p", { class: "field-help", id: `${id}-help` }, help) : null;
  const errorEl = h("p", { class: "field-error", id: `${id}-error`, hidden: true });
  control.setAttribute("aria-describedby", [helpEl?.id, errorEl.id].filter(Boolean).join(" "));
  const el = h("div", { class: "field" },
    h("label", { class: "field-label", for: id }, label),
    unit || addon
      ? h("div", { class: "field-row" }, control, unit ? h("span", { class: "field-unit" }, unit) : null, addon)
      : control,
    helpEl, errorEl);
  return {
    el,
    control,
    setError(message) {
      control.setAttribute("aria-invalid", "true");
      replace(errorEl, icon("circle-alert"), message);
      errorEl.hidden = false;
    },
    clearError() {
      control.removeAttribute("aria-invalid");
      errorEl.hidden = true;
    },
  };
}
