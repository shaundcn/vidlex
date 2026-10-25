// 全站唯一创建元素的地方（技术选型 12.2）：文字只用 textContent，不拼 HTML。
//   h("button", { class: "btn", type: "button", onclick: fn, "aria-pressed": "true" }, "文字", 子元素)
// 属性值为 null、undefined、false 时不设置；on 开头的是事件。

const PROPERTIES = new Set(["value", "checked", "disabled"]);

export function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props ?? {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key.startsWith("on") && typeof value === "function") {
      el.addEventListener(key.slice(2), value);
    } else if (key === "class") {
      el.className = value;
    } else if (PROPERTIES.has(key)) {
      el[key] = value; // textarea 的内容、复选框状态等只能通过属性设置
    } else {
      el.setAttribute(key, value === true ? "" : String(value));
    }
  }
  append(el, children);
  return el;
}

export function append(el, children) {
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

// 整块重画：清空后放入新内容
export function replace(el, ...children) {
  el.replaceChildren();
  return append(el, children);
}

export function $(selector, root = document) {
  return root.querySelector(selector);
}
