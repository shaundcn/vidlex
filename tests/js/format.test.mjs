import assert from "node:assert/strict";
import { test } from "node:test";

// 时间按浏览器所在时区显示；先固定时区再加载模块（模块加载时创建格式器）
process.env.TZ = "Asia/Shanghai";
const { formatTime } = await import("../../backend/app/static/js/core/format.js");

test("列表到分钟、日志到秒，零点显示 00", () => {
  assert.equal(formatTime(Date.UTC(2026, 9, 22, 9, 45)), "2026-10-22 17:45");
  assert.equal(formatTime(Date.UTC(2026, 9, 21, 16, 5, 9)), "2026-10-22 00:05");
  assert.equal(formatTime(Date.UTC(2026, 9, 21, 16, 5, 9), true), "2026-10-22 00:05:09");
});
