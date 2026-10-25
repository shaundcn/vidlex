// 状态标签（技术选型 12.3、12.4）：只显示，不可点击。

import { h } from "../core/dom.js";
import { STATUS_NAMES } from "../core/format.js";

export function tag(status) {
  return h("span", { class: `tag tag-${status}` }, STATUS_NAMES[status] ?? status);
}
