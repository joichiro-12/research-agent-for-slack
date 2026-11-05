#!/usr/bin/env bash
# bot を常駐させる（tmux などの中で実行）。落ちたら 10 秒後に再起動する。
cd "$(dirname "$0")/.."
# bot と claude が作るファイルは本人だけが読めるようにする（共用サーバーでほかのユーザーに見せないため）
umask 077
export PATH="$HOME/.local/bin:$PATH"
mkdir -p runtime
while true; do
  uv run research-agent 2>&1 | tee -a runtime/bot.log
  echo "$(date '+%F %T') bot exited, restarting in 10s" | tee -a runtime/bot.log
  sleep 10
done
