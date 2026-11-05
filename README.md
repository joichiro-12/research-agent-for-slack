# research-agent-for-slack

## 概要

Slack のメンションを、サーバー上の Claude Code（`claude -p`）に渡して、結果を同じスレッドに返す bot。
1 スレッド = 1 つの Claude セッションで、続けて話すと文脈を引き継ぐ。Socket Mode で動くので、サーバーに公開 URL はいらない。

## 呼び出し方

| 送り方 | モデル |
|---|---|
| `@research-agent 依頼` | Sonnet |
| `@research-agent opus 依頼` | Opus |
| 一度メンションしたスレッドで、メンションなしの返信 | Sonnet |

メンションに `--max` のように付けると、エフォート（low・medium・high・xhigh・max）も変えられる。
画像などの添付ファイルも Claude に渡る。
`@research-agent fb 変えてほしいルール` と送ると、bot の設定（`profiles/`）の修正案を PR にする。

## 権限

共通の設定は `config/claude-settings.json`、チャンネルごとの追加分は `profiles/<名前>/permissions.json` で管理する。

- 許可: ファイルの読み取り、`ls` などの読み取り系コマンド、WebSearch / WebFetch
- 禁止: SSH 鍵、各種認証情報、`.env` の読み取り
- `config/.env` の値は Claude に渡さない
- 指示できるのは `ALLOWED_USER_IDS` に書いたユーザーだけ
- `guard.toml` のあるプロファイルでは、Bash のコマンドを実行前に調べ、規則に当たるものは止めるか、スレッドでの承認を待つ

`profiles/research` は Bash とファイルの編集を許す。ガードは操作の誤りを防ぐもので、サンドボックスではない。`ALLOWED_USER_IDS` には信頼できる人だけを入れる。

## 初回セットアップ

前提: サーバーに Claude Code 2.1.295 以降（ログイン済み）、[uv](https://docs.astral.sh/uv/)、tmux。Office の添付も読ませるなら LibreOffice。
Claude Code がこれより古いと、ガードのフックが落ちたときにコマンドが止まらない。

1. [api.slack.com/apps](https://api.slack.com/apps) で **From a manifest** を選び、`config/slack-manifest.yaml` を貼ってアプリを作る。App-Level Token（`connections:write`）と Bot Token を発行する
2. サーバーに clone して、トークンと自分の Slack member ID を書く

   ```bash
   git clone https://github.com/joichiro-12/research-agent-for-slack.git && cd research-agent-for-slack
   cp config/.env.example config/.env && chmod 600 config/.env
   ```

3. 起動する

   ```bash
   tmux new-session -d -s research-agent scripts/run.sh
   ```

   ログは `runtime/bot.log` に出る。

4. 使うチャンネルで `/invite @research-agent` し、`config/channels.toml` でチャンネルとプロファイルを対応づける。`work_dir` のディレクトリは先に作っておく
5. `fb` を使うなら、この clone を自分の private リポジトリに push し、そのリポジトリを `GITHUB_REPO` に、トークンを `GITHUB_TOKEN` に書く。commit と PR に Slack の依頼の文とリンクが入るので、public のリポジトリには使わない

---

Slack と Claude は、それぞれの権利者の商標です。このプロジェクトは個人のもので、Slack と Anthropic の公式のものではありません。
