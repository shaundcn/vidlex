// 状态名称和顺序、步骤名称、名称显示规则、时间格式、拼音排序器——只在这里定义一次（技术选型 12.2）。

export const STATUS_ORDER = ["queued", "processing", "completed", "failed", "stopped", "ignored"];

export const STATUS_NAMES = {
  queued: "排队中",
  processing: "处理中",
  completed: "已完成",
  failed: "失败",
  stopped: "已停止",
  ignored: "已忽略",
};

// 数字卡片的 5 种状态（处理中不单独成卡片，立项 4.2）
export const CARD_STATUSES = ["queued", "completed", "failed", "stopped", "ignored"];

// 2026-10-24 恢复转码（只转音频，技术选型 11.5）
export const STEP_NAMES = {
  transcode: "转码",
  upload: "上传",
  analyze: "分析",
  render_md: "生成 md",
  commit: "保存结果",
};

export const ACTION_NAMES = { start: "开始", stop: "停止", ignore: "忽略" };

export const QUEUE_STATES = {
  running: { name: "运行中", desc: "按顺序处理排队中的任务", action: "pause", label: "暂停任务" },
  paused: { name: "已暂停", desc: "不再开始新任务；正在处理的任务会做完", action: "resume", label: "继续任务" },
  sleeping: { name: "休眠中", desc: "没有新视频，已降低扫描频率；发现新视频自动回到运行中", action: "wake", label: "开始任务" },
  error: { name: "出错", desc: "", action: "resume", label: "继续任务" },
};

// 完成前显示原文件名；完成后显示新标题（只有已完成才有 title）
export function displayName(task) {
  return task.title ?? task.original_name;
}

const minuteFormat = new Intl.DateTimeFormat("zh-CN", {
  year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
});
const secondFormat = new Intl.DateTimeFormat("zh-CN", {
  year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit",
  hour12: false,
});

// 浏览器所在时区；列表到分钟（2026-10-22 17:45），日志到秒
export function formatTime(ms, withSeconds = false) {
  const parts = Object.fromEntries(
    (withSeconds ? secondFormat : minuteFormat).formatToParts(new Date(ms)).map((p) => [p.type, p.value]),
  );
  const time = `${parts.hour}:${parts.minute}${withSeconds ? `:${parts.second}` : ""}`;
  return `${parts.year}-${parts.month}-${parts.day} ${time}`;
}

// 名称按中文拼音排序
export const collator = new Intl.Collator("zh-CN", { numeric: true, sensitivity: "base" });
