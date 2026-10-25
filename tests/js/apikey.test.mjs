import assert from "node:assert/strict";
import { test } from "node:test";

import { keyError } from "../../backend/app/static/js/core/apikey.js";

const MASKED = "ark-****1234";

test("打码值原样或改动后都不能保存（2026-10-24 打码的 Key 被编辑后保存）", () => {
  assert.match(keyError(MASKED, MASKED), /没有改动/);
  assert.match(keyError(`${MASKED}5`, MASKED), /不能包含 \*/);
});

test("没填不能保存；输入新 Key 可以保存", () => {
  assert.match(keyError("  ", MASKED), /请填写/);
  assert.equal(keyError("ark-new-key", MASKED), null);
  assert.equal(keyError("ark-new-key", null), null);
});
