# research-agent-for-slack

Slack のメンションを Claude Code（`claude -p`）に渡し、結果をスレッドに返す bot。Python 3.12 + slack-bolt（Socket Mode）を uv で動かす。

## Architecture

```
research-agent-for-slack/
├── src/research_agent/  # bot 本体（入口は bot.py、claude -p の実行は claude.py）
├── profiles/            # 指示と権限（persona.md + <プロファイル>/）。fb の PR が書き換える
├── config/              # channels.toml（チャンネル ↔ プロファイル・作業場・モデル）、claude-settings.json（共通の権限）
├── scripts/run.sh       # bot を常駐させる
├── tests/               # unittest
└── runtime/             # 状態・ログ・作業場（git 管理外）
```

## Commands

- `uv run python -m unittest discover -s tests`: テストを実行する
- `tmux kill-session -t research-agent; tmux new-session -d -s research-agent scripts/run.sh`: bot を再起動する
- `tail -n 50 runtime/bot.log`: ログを見る（ガードは `runtime/guard.log`）

## Critical Rules

- `config/.env` の値は読まない・表示しない
- **README は短く保つ**: 概要 / 呼び出し方 / 権限 / 初回セットアップの 4 節だけ。機能を足しても README には書かず、仕組みの説明は各モジュールの docstring に書く
- この CLAUDE.md は bot に読ませていない（`config.py` の `CLAUDE_MD_EXCLUDES`）。場所を変えたらそこも直す
- `src/`・`scripts/`・`pyproject.toml`・`uv.lock`・`config/.env` を変えたら再起動する。`profiles/` とほかの `config/` は依頼ごとに読み直すので不要
- 再起動すると実行中のターンは止まる。次の起動でスレッドに知らせて 1 回だけ続けさせる（`bot.resume_thread`）が、途中のコマンドはやり直しになる
- 作業場（`work_dir`）を動かすと `--resume` が切れる。動かすなら `~/.claude/projects/` の古いパスのディレクトリを新しいパス名でコピーする
- ガードのあるプロファイルの `work_dir` は、このリポジトリの外に置く（リポジトリの中は Edit できない）
- 研究の文脈（テーマの CLAUDE.md やノート）はこのリポジトリに入れず、各チャンネルの作業場のリポジトリに置く
- 好みは bot の自動メモリ、ルールや手順は `fb` で `profiles/` に。自動メモリは作業場ごとに分かれるので、全チャンネルに効かせる書き方は `persona.md` に書く

## For Claude

- コメント・docstring・README は日本語、コミットメッセージは英語の命令形（例: `Show the model and effort in the thread status`）
- Slack に出す文には絵文字もリアクションも付けない。使うのは段取りの印（⬜ ⏳ ✅）と承認待ちの 🔐 だけで、失敗も文で伝える
