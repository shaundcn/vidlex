#!/bin/sh
# 一条命令跑完全部检查（技术选型 13.14）。真实方舟测试会花钱，不在这里跑：uv run --env-file .env pytest -m real
set -eu
cd "$(dirname "$0")/.."

echo "== ruff"
uv run ruff check

echo "== pyright"
uv run pyright

echo "== 后端测试"
uv run pytest -q

echo "== 前端纯逻辑测试"
node --test 'tests/js/*.test.mjs'

echo "== JS 语法"
for f in backend/app/static/js/*/*.js; do node --check "$f"; done

echo "== 页面模块的导入能对上（只链接：页面一运行就要用 document，运行时的报错不算）"
for f in backend/app/static/js/pages/*.js; do
  node --input-type=module -e "import('./$f').catch((e) => { if (e instanceof SyntaxError) { console.error('$f:', e.message); process.exit(1); } })"
done

echo "全部通过"
