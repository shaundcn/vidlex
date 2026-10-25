// 全站唯一调用 fetch 的地方（技术选型 12.2）：超时、JSON、错误统一处理。
// 修改类请求一律发 JSON，没有内容也发 {}；失败统一成 ApiError {status, message}。

const DEFAULT_TIMEOUT = 15000;

class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status; // 0 表示没连上（断网、超时）
  }
}

export async function api(path, { method = "GET", body, timeout = DEFAULT_TIMEOUT } = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  const options = { method, signal: controller.signal, headers: { Accept: "application/json" } };
  if (method !== "GET") {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body ?? {});
  }
  try {
    let response;
    try {
      response = await fetch(path, options);
    } catch (err) {
      throw new ApiError(0, err.name === "AbortError" ? "请求超时，请稍后再试" : "无法连接 vidlex");
    }
    if (response.ok) return response.status === 204 ? null : await response.json();
    let detail;
    try {
      detail = (await response.json()).detail;
    } catch {
      detail = null;
    }
    throw new ApiError(response.status, describe(detail, response.status));
  } finally {
    clearTimeout(timer);
  }
}

// detail 是文字直接用；是列表（422）拼成一句
function describe(detail, status) {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map((d) => d.msg).join("；") || `请求有误（${status}）`;
  return `请求失败（${status}）`;
}
