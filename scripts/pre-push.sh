#!/bin/sh
# push の前に走らせるタスク。
# ~/.config/jj/config.toml の `jj push` エイリアスから呼ばれる。
# .github/workflows/pages.yml の test ジョブと同じテストを走らせる。
# 1 つでも失敗したら、その時点で止まり、push しない。
set -eu

cd "$(dirname "$0")/.."

echo "== pre-push: node --test webgui/test/*.test.mjs"
node --test webgui/test/*.test.mjs

echo "== pre-push: tools/test_map_protocol.py"
PYTHONDONTWRITEBYTECODE=1 python3 tools/test_map_protocol.py

echo "== pre-push: tools/test_strategy_sim.py"
PYTHONDONTWRITEBYTECODE=1 python3 tools/test_strategy_sim.py

echo "== pre-push: すべて OK"
