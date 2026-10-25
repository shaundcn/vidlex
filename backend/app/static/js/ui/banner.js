// 顶栏下方的整页提示条（技术选型 12.4），例如“暂时连不上 vidlex，正在重试”。

import { h, replace } from "../core/dom.js";
import { icon } from "../core/icons.js";

export function banner(anchor) {
  const inner = h("div", { class: "banner-inner" });
  const el = h("div", { class: "banner", role: "alert", hidden: true }, inner);
  anchor.after(el);
  return {
    show(text, iconName = "wifi-off") {
      replace(inner, icon(iconName), text);
      el.hidden = false;
    },
    hide() {
      el.hidden = true;
    },
  };
}
