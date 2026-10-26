// Lucide 图标（lucide-static 1.53.0，ISC 许可，全文见 LICENSES/lucide-ISC.txt）。
// 只收录用到的图标；用 createElementNS 逐个生成 SVG 元素，不拼 HTML 字符串（技术选型 12.2）。

const SVG_NS = "http://www.w3.org/2000/svg";

const ICONS = {
  "loader-circle": [["path", {"d": "M21 12a9 9 0 1 1-6.219-8.56"}]],
  "download": [["path", {"d": "M12 15V3"}], ["path", {"d": "M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"}], ["path", {"d": "m7 10 5 5 5-5"}]],
  "external-link": [["path", {"d": "M15 3h6v6"}], ["path", {"d": "M10 14 21 3"}], ["path", {"d": "M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"}]],
  "arrow-up": [["path", {"d": "m5 12 7-7 7 7"}], ["path", {"d": "M12 19V5"}]],
  "arrow-down": [["path", {"d": "M12 5v14"}], ["path", {"d": "m19 12-7 7-7-7"}]],
  "arrow-up-down": [["path", {"d": "m21 16-4 4-4-4"}], ["path", {"d": "M17 20V4"}], ["path", {"d": "m3 8 4-4 4 4"}], ["path", {"d": "M7 4v16"}]],
  "search": [["path", {"d": "m21 21-4.34-4.34"}], ["circle", {"cx": "11", "cy": "11", "r": "8"}]],
  "check": [["path", {"d": "M20 6 9 17l-5-5"}]],
  "x": [["path", {"d": "M18 6 6 18"}], ["path", {"d": "m6 6 12 12"}]],
  "circle-alert": [["circle", {"cx": "12", "cy": "12", "r": "10"}], ["line", {"x1": "12", "x2": "12", "y1": "8", "y2": "12"}], ["line", {"x1": "12", "x2": "12.01", "y1": "16", "y2": "16"}]],
  "eye": [["path", {"d": "M2.062 12.348a1 1 0 0 1 0-.696 10.75 10.75 0 0 1 19.876 0 1 1 0 0 1 0 .696 10.75 10.75 0 0 1-19.876 0"}], ["circle", {"cx": "12", "cy": "12", "r": "3"}]],
  "eye-off": [["path", {"d": "M10.733 5.076a10.744 10.744 0 0 1 11.205 6.575 1 1 0 0 1 0 .696 10.747 10.747 0 0 1-1.444 2.49"}], ["path", {"d": "M14.084 14.158a3 3 0 0 1-4.242-4.242"}], ["path", {"d": "M17.479 17.499a10.75 10.75 0 0 1-15.417-5.151 1 1 0 0 1 0-.696 10.75 10.75 0 0 1 4.446-5.143"}], ["path", {"d": "m2 2 20 20"}]],
  "wifi-off": [["path", {"d": "M12 20h.01"}], ["path", {"d": "M8.5 16.429a5 5 0 0 1 7 0"}], ["path", {"d": "M5 12.859a10 10 0 0 1 5.17-2.69"}], ["path", {"d": "M19 12.859a10 10 0 0 0-2.007-1.523"}], ["path", {"d": "M2 8.82a15 15 0 0 1 4.177-2.643"}], ["path", {"d": "M22 8.82a15 15 0 0 0-11.288-3.764"}], ["path", {"d": "m2 2 20 20"}]],
};

export function icon(name, className = "icon") {
  const svg = document.createElementNS(SVG_NS, "svg");
  for (const [k, v] of Object.entries({
    viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": "2",
    "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true", class: className,
  })) svg.setAttribute(k, v);
  for (const [tag, attrs] of ICONS[name]) {
    const el = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
    svg.append(el);
  }
  return svg;
}
