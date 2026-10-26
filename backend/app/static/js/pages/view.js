// 查看页 view.html?task=ID（立项 4.4，技术选型 12.5）：在新标签页打开，只看一个任务，打开时读取一次。
// 已完成：上面视频、下面 md；未完成：只显示视频。视频右下角是下载按钮。全站唯一使用 innerHTML 的地方：
// 写入后端渲染好的 md 正文（后端已关闭原始 HTML、拦截危险链接）。

import { api } from "../core/api.js";
import { $, h, replace } from "../core/dom.js";
import { displayName } from "../core/format.js";
import { icon } from "../core/icons.js";
import { empty, failure, loadingAfter } from "../ui/states.js";
import { tag } from "../ui/tag.js";

const main = $("#main");
const taskId = new URLSearchParams(location.search).get("task");

load();

async function load() {
  if (!/^\d+$/.test(taskId ?? "")) {
    replace(main, empty({ title: "任务不存在" }));
    return;
  }
  const cancelLoading = loadingAfter(main);
  try {
    const view = await api(`/api/tasks/${taskId}/view`);
    cancelLoading();
    render(view);
  } catch (err) {
    cancelLoading();
    if (err.status === 404) {
      document.title = "任务不存在 · vidlex";
      replace(main, empty({ title: "任务不存在", text: "它可能已经因为视频被删除或替换而从列表消失。" }));
    } else {
      replace(main, failure({ text: err.message, retry: load }));
    }
  }
}

function render(view) {
  const name = displayName(view);
  document.title = `${name} · vidlex`;
  const header = h("header", {},
    h("h1", { class: "title-lg" }, name),
    view.title ? h("p", { class: "text-small" }, view.original_name) : null,
    view.status !== "completed" ? h("p", { class: "status-note" }, tag(view.status)) : null);
  if (view.status !== "completed") {
    replace(main, header, h("div", { class: "view-single" }, videoArea(view)));
    return;
  }
  replace(main, header, h("div", { class: "view-layout" }, videoArea(view), mdPanel(view)));
}

// 视频和它右下角的下载按钮（2026-10-26 用户要求）：视频在时“下载视频”，已完成且 md 在时“下载 md”。
// 浏览器播放不了时也显示，可以下载到本机播放
function videoArea(view) {
  const links = [
    view.video_exists ? downloadLink(`/api/tasks/${view.id}/video?download=1`, "下载视频") : null,
    view.status === "completed" && view.md_exists ? downloadLink(`/api/tasks/${view.id}/md`, "下载 md") : null,
  ].filter(Boolean);
  return h("div", {}, player(view), links.length ? h("div", { class: "view-actions" }, ...links) : null);
}

// 原样下载文件；文件名由后端的 Content-Disposition 给出
function downloadLink(href, text) {
  return h("a", { class: "btn btn-outline btn-sm", href, download: true }, icon("download"), text);
}

function player(view) {
  const box = h("div", { class: "media" });
  if (!view.video_exists) {
    box.append(message("视频文件不存在"));
    return box;
  }
  const video = h("video", { controls: true, preload: "metadata", src: `/api/tasks/${view.id}/video` });
  // 浏览器播放不了：出现播放错误，或读完视频信息但画面宽度为 0（只有声音、画面黑屏），例如部分浏览器里的 HEVC
  const unplayable = () => replace(box, message("浏览器无法播放此视频"));
  video.addEventListener("error", unplayable);
  video.addEventListener("loadedmetadata", () => {
    if (video.videoWidth === 0) unplayable();
  });
  box.append(video);
  return box;
}

function message(text) {
  return h("p", { class: "media-msg" }, text);
}

function mdPanel(view) {
  if (!view.md_exists) return empty({ title: "md 文件不存在" });
  const meta = view.front_matter.length
    ? h("table", { class: "meta-table" },
      h("caption", { class: "visually-hidden" }, "信息块"),
      h("tbody", {}, ...view.front_matter.map(({ key, value }) =>
        h("tr", {}, h("th", { scope: "row" }, key), h("td", {}, value)))))
    : null;
  const body = h("article", { class: "md" });
  body.innerHTML = view.md_html;
  for (const link of body.querySelectorAll("a[href]")) {
    link.target = "_blank"; // 正文里的链接在新标签页打开
    link.rel = "noopener noreferrer";
  }
  return h("div", {}, meta, body);
}
