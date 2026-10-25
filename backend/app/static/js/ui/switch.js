// 开关（技术选型 12.4）：role="switch"，文字放在旁边，点文字也能切换。
//   const s = toggleSwitch({ text: "隐藏 30 天前完成的任务", checked: false, onChange: (on) => {...} })
//   s.el 放进页面；s.checked() 读状态；s.set(false) 从外部改（不触发 onChange）

import { h } from "../core/dom.js";

export function toggleSwitch({ text, checked = false, onChange }) {
  const control = h("button", { type: "button", class: "switch", role: "switch", "aria-checked": String(checked) });
  const set = (on) => control.setAttribute("aria-checked", String(on));
  const isOn = () => control.getAttribute("aria-checked") === "true";
  control.addEventListener("click", () => {
    set(!isOn());
    onChange?.(isOn());
  });
  // label 包住按钮：按钮的名字就是这段文字，点文字等于点按钮
  return { el: h("label", { class: "switch-field" }, control, h("span", {}, text)), control, checked: isOn, set };
}
