// API Key 输入框的判断（立项第 5 节）：不碰页面的纯函数，测试见 tests/js/apikey.test.mjs。

// 保存前检查，返回错误提示，没问题时返回 null。打码值（开头 4 位 + * + 末尾 4 位）不能原样或改动后保存
export function keyError(value, masked) {
  if (!value.trim()) return "请填写 API Key";
  if (masked && value === masked) return "没有改动；要更换请点进输入框输入新的 API Key";
  if (value.includes("*")) return "API Key 不能包含 *，请完整输入新的 Key";
  return null;
}
