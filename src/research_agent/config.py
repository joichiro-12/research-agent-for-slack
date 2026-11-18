"""設定値。秘密情報は config/.env、Claude の権限は config/claude-settings.json に置く。"""

import os
import re
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

ROOT_DIR = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT_DIR / "config"
PROFILES_DIR = ROOT_DIR / "profiles"
# 全チャンネル共通のペルソナ（口調・振る舞い）
PERSONA_FILE = PROFILES_DIR / "persona.md"
CHANNELS_FILE = CONFIG_DIR / "channels.toml"
# git 管理外。スレッドとセッションの対応表、ログ、添付ファイル、Claude の作業場所を置く
RUNTIME_DIR = ROOT_DIR / "runtime"
ATTACHMENTS_DIR = RUNTIME_DIR / "attachments"
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
ENV_FILE = CONFIG_DIR / ".env"
load_dotenv(ENV_FILE)

SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN = os.environ["SLACK_APP_TOKEN"]
ALLOWED_USER_IDS = {u.strip() for u in os.environ["ALLOWED_USER_IDS"].split(",") if u.strip()}

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
# 相対パスはリポジトリからの位置として読む（Edit の規則には絶対パスが要る）
WORK_DIR = ROOT_DIR / Path(os.environ.get("CLAUDE_WORK_DIR", "runtime/workspace")).expanduser()
# 許可するツールと、読ませない認証情報の一覧（claude --settings で渡す）
SETTINGS_FILE = Path(os.environ.get("CLAUDE_SETTINGS", CONFIG_DIR / "claude-settings.json")).expanduser()
# リポジトリの CLAUDE.md は bot を開発する Claude Code 向け。作業場（runtime/workspace）はリポジトリの中にあり、
# Claude Code は親ディレクトリの CLAUDE.md も読むので、bot として動かす claude には読ませない（claudeMdExcludes で渡す）
CLAUDE_MD_EXCLUDES = [str(ROOT_DIR / "CLAUDE.md"), str(ROOT_DIR / ".claude" / "CLAUDE.md")]
# Slack トークンなど .env の中身は claude に引き継がない（echo $SLACK_BOT_TOKEN で読めてしまうため）
CLAUDE_ENV = {k: v for k, v in os.environ.items() if k not in dotenv_values(ENV_FILE)}
# Claude が Bash から使う bot のコマンド（agent-approval）を PATH に入れる
CLAUDE_ENV["PATH"] = f"{ROOT_DIR / 'bin'}:{CLAUDE_ENV.get('PATH', '')}"
# claude.ai のコネクタが接続し終わるまで待ってから始める。
# 待たないと、セッションを再開したときにコネクタのツールが「切断された」と扱われて使えなくなる
CLAUDE_ENV["MCP_CONNECTION_NONBLOCKING"] = "0"
TIMEOUT_SEC = int(os.environ.get("CLAUDE_TIMEOUT_SEC", "1800"))
SESSIONS_FILE = RUNTIME_DIR / "sessions.json"
# スレッドごとの状態（段取り、題、終わっていない依頼）。threadstate.py
THREADS_FILE = RUNTIME_DIR / "threads.json"
# 時間切れになったら、同じセッションでこの回数だけ続きをさせる（作業はセッションに残っている）
TIMEOUT_CONTINUES = 1
TIMEOUT_NOTICE = "{minutes} 分たっても終わらなかったから、いったん区切って続けるね"
TIMEOUT_PROMPT = (
    "【時間切れ】前の実行は {minutes} 分で打ち切られた。途中で打ち切られたコマンドの結果は分からないものとして、"
    "ファイルの状況を確かめてから、元の依頼の続きを進めること。"
)
TIMEOUT_GIVE_UP = "{minutes} 分の上限を {times} 回使っても終わらなかったから、ここで止めたよ"
# bot の再起動で止まったターンは、次の起動で同じセッションで 1 回だけ続けさせる（bot.resume_thread）。
# 依頼が Claude に届く前に止まったかもしれないので、元の依頼も渡す
RESTART_NOTICE = "再起動で作業が途中で止まったから、続きをやるね"
RESTART_PROMPT = (
    "【再起動】{now} に bot が再起動して、次の依頼の処理が途中で止まった（始まる前だったかもしれない）。"
    "途中で止まったコマンドの結果は分からないものとして、ファイルの状況を確かめてから、続きを進めること。\n\n"
    "【元の依頼】\n{prompt}"
)
RESTART_GIVE_UP = "続きをやっている途中でまた再起動が入ったから、ここで止めたよ。続けるなら声をかけてね"
# 前の bot が残した claude に SIGTERM を送ってから、止まるのを待つ時間（claude.stop_leftovers）
STOP_WAIT_SEC = 10

# fb で受けたフィードバックを、profiles/ を直す PR にする（どちらかが空なら使わない）
# GITHUB_TOKEN は fine-grained PAT（対象リポジトリの Contents と Pull requests を Read and write）
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")  # owner/name
# 修正案を作る作業用の worktree と、積んでいる PR・確認待ちの状態を置く
FEEDBACK_DIR = RUNTIME_DIR / "feedback"
GIT_AUTHOR = ("research-agent", "research-agent@localhost")
# マージされた PR を取り込むため、origin/main を確認する間隔
SYNC_INTERVAL_SEC = 300
DIFF_MAX_LEN = 3000
# フィードバックの文脈として Claude に渡すスレッドのやりとりの上限（文字数、新しい方を残す）
THREAD_HISTORY_MAX_LEN = 8000
# 先頭の "fb" でフィードバック反映モードにする（"FB:" "fb：" なども可）
FEEDBACK_PATTERN = re.compile(r"^fb(?![A-Za-z0-9])[\s:：]*", re.IGNORECASE)
# 確認待ちのフィードバックへの返事（NFKC で全角をそろえてから照合する）。どちらでもなければ修正の依頼として扱う
OK_PATTERN = re.compile(r"^(ok|okay|おk|おけ|おっけー|オッケー|お願い(します)?|おねがい(します)?)[\s!。.]*$", re.IGNORECASE)
CANCEL_PATTERN = re.compile(r"^(やめる|やめて|やめます|キャンセル|中止|cancel)[\s!。.]*$", re.IGNORECASE)
# ガードが止めた操作への返事（同じく NFKC でそろえてから照合する）。どちらでもなければ、承認待ちを取り下げて通常の依頼として扱う
APPROVE_PATTERN = re.compile(
    r"^(承認|承認します|ok|okay|おk|おけ|おっけー|オッケー|はい|yes|実行して(ください)?|お願い(します)?|おねがい(します)?)[\s!。.]*$",
    re.IGNORECASE,
)
REJECT_PATTERN = re.compile(r"^(却下|やめる|やめて|やめます|キャンセル|中止|いいえ|no|cancel)[\s!。.]*$", re.IGNORECASE)
# 承認の依頼に付けるボタン（bot.approval_buttons）
APPROVE_ACTION = "approval_approve"
REJECT_ACTION = "approval_reject"
# Bolt がリスナーを動かすスレッドの数。既定の 5 本では、メンションの処理がターンのあいだ 1 本ずつ使うので、
# 埋まるとボタンに 3 秒以内に返せなくなる。ターンは claude を待っているだけなので、多くても負荷にならない
LISTENER_WORKERS = 32

# Claude が作業場の outbox/<スレッド>/ に置いたファイルを、返信のあとにスレッドへ添付する（1 回の依頼で最大 MAX_UPLOADS 件）
MAX_UPLOADS = 10
OUTBOX_HINT = (
    "Slack のスレッドに添付したいファイル（図・表・文書）は {dir} に置くこと。"
    "返信のあとに bot が添付する（1 件 20 MB まで）。"
)

# モデルごとの claude --model の値と表示名
MODELS = {
    "sonnet": {"label": "Sonnet"},
    "opus": {"label": "Opus"},
}
DEFAULT_MODEL = "sonnet"
# claude --effort に渡せる値。stream-json には出ないので、ステータスには渡した値を出す
EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_EFFORT = "medium"
# 英単語として "opus" を含むか（"opusで" のように日本語が続いても拾う）
OPUS_PATTERN = re.compile(r"(?<![A-Za-z])opus(?![A-Za-z])", re.IGNORECASE)
# 単語として置いた "--max" などでエフォートを選ぶ（"--maxで" も拾う）。iPhone は -- を — に変えるので、ダッシュも受ける
EFFORT_PATTERN = re.compile(rf"(?<!\S)(?:--|－－|[—–])({'|'.join(EFFORTS)})(?![A-Za-z0-9_-])", re.IGNORECASE)
# スレッド内の返信として扱う message イベントの subtype（編集・削除・bot 投稿などは無視する）
THREAD_MESSAGE_SUBTYPES = {None, "thread_broadcast", "file_share"}
SLACK_CHUNK = 3500

# ステータスは Slack 側で 2 分間返信がないと消えるので、それより短い間隔で再送する
STATUS_HEARTBEAT_SEC = 90
STATUS_MAX_LEN = 80
# Slack は status の前にアプリ名を付けるが、loading_messages はそのまま出すので、そちらには bot が付ける
# （表示名は users:read がないと取れないので、ここに書く。Slack アプリの名前を変えたら、ここも直す）
APP_NAME = "research-agent"
# Bash の description がそのままステータスに出るので、日本語で短く書かせる
STATUS_HINT = (
    "あなたの進捗は Slack のステータス表示に流れる。"
    "Bash ツールの description は日本語で 20 字程度の簡潔な説明にすること。"
)
# Claude の段取り（TaskCreate・TaskUpdate）を、スレッドにメッセージとして出す（plan.py）。
# いまのモデルでは CLI がこのツールを外しているので、依頼を処理するときだけ環境変数で戻し、使いどころを指示する
PLAN_ENV = {"CLAUDE_CODE_ENABLE_TODO_TOOLS": "1"}
PLAN_HINT = (
    "手順が 3 つ以上ある依頼では、作業を始める前に TaskCreate で手順を登録し、"
    "各手順の開始時と完了時に TaskUpdate で状態を更新すること。手順の一覧は段取りとして Slack のスレッドに表示される。"
    "subject は日本語で 20 字程度の簡潔な手順名にすること。すぐに答えられる質問では使わないこと。"
    "段取りはターンをまたいでスレッドに残る。承認を待ってターンを終えるときは、残りの手順を消さずに残し、"
    "次のターンで TaskUpdate で続きを更新すること。やらなくなった手順は deleted にすること。"
)
# 段取りの各手順に付ける絵文字
PLAN_MARKS = {"pending": "⬜", "in_progress": "⏳", "completed": "✅"}
# ターンを終えて承認を待っている手順の印
PLAN_APPROVAL_MARK = "🔐"
# 段取りに並べる手順の上限。超えたら、終わった手順を古い方から畳む
PLAN_SHOWN_MAX = 10
# 段取りに添える、いま待っていること（plan.py）
WAIT_APPROVAL = "いまは承認を待ってるよ"
WAIT_REPLY = "いまは返事を待ってるよ"
# 見出しの進み具合のバー（終わった手順・残りの手順）。手順 1 つで 1 マス、多いときは PLAN_BAR_MAX マスに縮める
PLAN_BAR = ("▰", "▱")
PLAN_BAR_MAX = 10

# スレッドの題と URL を、システムプロンプトに足す（bot.thread_brief）。
# システムプロンプトが変わると Claude のキャッシュが切れるので、ターンごとに変わるものはここに入れない
THREAD_BRIEF = (
    "## このスレッド\n\n"
    "題はスレッドの最初の投稿で、このスレッドで何をしているかを表す。\n"
    "- 題：{title}\n"
    "- URL：{url}\n"
    "- スレッドの番号：{thread_ts}"
)
