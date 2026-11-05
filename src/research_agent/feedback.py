"""fb で受けたフィードバックを、profiles/ の修正として 1 本の PR に積む。

流れ: fb 付きの依頼 → 作業用 worktree（runtime/feedback/worktree）で Claude が profiles/ を編集
→ 差分をスレッドに出す → ok で commit・push して PR に追加 / やめる で破棄 / それ以外の返信は修正の依頼。
PR は 1 本にまとめ（1 commit = 1 フィードバック）、マージされるまで次のフィードバックも同じブランチに積む。
確認待ちにできるフィードバックは同時に 1 件だけ。
修正案は、スレッドのセッションとは別の新しいセッションで作る（スレッドのやりとりはテキストで渡す）。
スレッドのセッションを引き継ぐと、以前「記憶に保存した」流れに引っぱられて profiles/ を直さないため。
fb はルールや手順を変えるためのもので、好みは通常の会話で Claude Code の自動メモリが覚える。
Claude に許すのは worktree の profiles/ の編集だけ（自動メモリやチャンネルごとのツールは使わせない）で、
commit・push・PR の作成は bot が行う。
稼働中の作業ツリーには、マージされた PR を origin/main の fast-forward で取り込む（sync）。
"""

import base64
import json
import logging
import re
import subprocess
import threading
import time
import tomllib
import urllib.error
import urllib.request
from datetime import datetime

from .config import (
    CLAUDE_ENV,
    FEEDBACK_DIR,
    GIT_AUTHOR,
    GITHUB_REPO,
    GITHUB_TOKEN,
    ROOT_DIR,
    SYNC_INTERVAL_SEC,
)
from .profiles import Profile, base_settings

log = logging.getLogger(__name__)

WORKTREE = FEEDBACK_DIR / "worktree"
STATE_FILE = FEEDBACK_DIR / "state.json"
REPO_URL = f"https://github.com/{GITHUB_REPO}.git"
# Claude に編集させるディレクトリ（worktree からの相対パス）
EDITABLE = "profiles"
# 取り込んだ変更にこれらが含まれていたら、bot の再起動が必要
RESTART_PATHS = ("src/", "scripts/", "pyproject.toml", "uv.lock")
# worktree・状態ファイル・稼働中の作業ツリーの git 操作を直列化する
_lock = threading.Lock()

PROMPT = """# フィードバック反映モード

利用者から、あなたのルールや手順を変えてほしいというフィードバックが届いた。
bot の設定のリポジトリ（作業コピー: {root}）の profiles/ を編集して、次から確実にそう動くようにする。
変更は PR として利用者が確認する。

- 編集してよいのは {root}/profiles/ の下だけ。commit や push は bot が行うので、git は使わない
- 口調や話し方の変更も、persona.md を書き換えて行う。ペルソナにある「好みを覚える」は通常の会話の話で、このモードでは profiles/ を書き換えることを指す
- 自動メモリ（~/.claude/ の下）には書かない（書けない）。スレッドで「記憶に保存した」と返していても profiles/ には入っていないので、ここで profiles/ に書く
- profiles/ の編集のほかの作業はしない。必要なら、修正案とは別に通常の依頼として頼んでもらうよう伝える
- このチャンネルのプロファイルは「{profile}」。置き場所は次のとおり
  - 全チャンネル共通の口調・振る舞い: profiles/persona.md
  - このチャンネル固有の手順: profiles/{profile}/prompt.md
  - このチャンネルで使うツールの許可・禁止: profiles/{profile}/permissions.json（{{"allow": [...], "deny": [...]}}）
  - このチャンネルのガード（あれば）: profiles/{profile}/guard.toml。Bash のコマンドを正規表現で調べ、deny は止め、approve は利用者の承認を待たせる
- 判断・口調・手順のように、ゆるく守ればよいことはプロンプトに書く。絶対に起きてはいけないことは permissions.json か guard.toml で止める
- 追記を重ねず、既存の記述に統合して書き換える。重複や矛盾を残さず、短く保つ
- src/ や config/ の変更が必要なときは編集せず、そう伝える
- 依頼に添えたスレッドのやりとりから、何がまずかったかを確かめてから直す

返答は次の形で、短く書く。差分は bot が別に貼るので、返答には載せない。

要約: <変更の 1 行要約。コミットメッセージになる>
- なぜ起きたか
- どう直したか
"""


def enabled():
    return bool(GITHUB_TOKEN and GITHUB_REPO)


def pending():
    """確認待ちのフィードバック（{thread, permalink, requests, summary}）。なければ None。"""
    return _load().get("pending")


def start(thread, request, permalink):
    """新しいフィードバックを受け付けて worktree を用意する。確認待ちのものがあれば受け付けず、それを返す。"""
    with _lock:
        state = _load()
        if state.get("pending"):
            return state["pending"]
        _prepare(state)
        state["pending"] = {"thread": thread, "permalink": permalink, "requests": [request], "summary": "", "session": None}
        _save(state)
    return None


def session():
    """修正案を作っている Claude のセッション ID（修正の依頼のときに再開する）。"""
    return _load()["pending"].get("session")


def set_session(session_id):
    with _lock:
        state = _load()
        state["pending"]["session"] = session_id
        _save(state)


def add_request(text):
    """確認待ちのフィードバックに、修正の依頼を書き足す（コミットメッセージに残す）。"""
    with _lock:
        state = _load()
        state["pending"]["requests"].append(text)
        _save(state)


def profile(base):
    """フィードバック反映モードのプロファイル。base の指示に、worktree の profiles/ を編集する権限を組み合わせる。

    権限はチャンネルの追加分を使わず、共通の設定から始める。
    """
    settings = base_settings()
    permissions = settings.setdefault("permissions", {})
    # 自動メモリに書かれると profiles/ に差分が出ず、PR で確認できなくなる。
    # 自動メモリを切っても、メモリのパスを知っていれば Write で直接書けてしまうので、~/.claude/ への編集も禁止する
    settings["autoMemoryEnabled"] = False
    permissions.setdefault("deny", []).append("Edit(~/.claude/**)")
    target = WORKTREE / EDITABLE
    # "//" で始まるパスは絶対パス。Edit のルールは Write など、ファイルを編集するツールすべてに効く
    permissions.setdefault("allow", []).append(f"Edit(/{target}/**)")
    prompt = base.prompt + "\n\n" + PROMPT.format(root=WORKTREE, profile=base.name)
    return Profile(f"{base.name}+feedback", prompt, json.dumps(settings, ensure_ascii=False), effort=base.effort)


def proposal(reply):
    """Claude の返答から要約を取り、worktree の変更をステージして (差分, 読めない JSON・TOML ファイル) を返す。"""
    with _lock:
        state = _load()
        p = state["pending"]
        if m := re.search(r"^\s*要約[:：]\s*(.+)$", reply, re.MULTILINE):
            p["summary"] = m.group(1).strip()
        _save(state)
        _git(WORKTREE, "add", "-A", "--", EDITABLE)
        # profiles/ の外は Claude に編集させていないが、念のため元に戻す
        _git(WORKTREE, "checkout", "--", ".")
        _git(WORKTREE, "clean", "-fdq")
        broken = []
        for name in _git(WORKTREE, "diff", "--cached", "--name-only").splitlines():
            path = WORKTREE / name
            if not path.exists():
                continue
            try:
                if name.endswith(".json"):
                    json.loads(path.read_text())
                elif name.endswith(".toml"):
                    # guard.toml が読めないと、ガードがそのチャンネルの Bash をすべて止める
                    tomllib.loads(path.read_text())
            except (json.JSONDecodeError, tomllib.TOMLDecodeError):
                broken.append(name)
        return _git(WORKTREE, "diff", "--cached"), broken


def approve():
    """確認待ちの修正を commit・push して PR に積む（PR がなければ作る）。{number, url, summary, permalink} を返す。"""
    with _lock:
        state = _load()
        p = state["pending"]
        summary = p["summary"] or p["requests"][0].splitlines()[0][:60]
        _git(WORKTREE, "add", "-A", "--", EDITABLE)
        # 前回の push が失敗して commit だけ残っている場合は、commit し直さない
        if not _git_ok(WORKTREE, "diff", "--cached", "--quiet"):
            quoted = "\n".join(f"> {line}" for r in p["requests"] for line in r.splitlines())
            _git(WORKTREE, "commit", "-q", "-m", f"{summary}\n\nフィードバック:\n{quoted}\n\nSlack: {p['permalink']}")
        _git(WORKTREE, "push", "-q", REPO_URL, f"HEAD:refs/heads/{state['branch']}")
        state["items"].append({"summary": summary, "permalink": p["permalink"]})
        state["pending"] = None
        _save(state)
        lines = "\n".join(f"- {i['summary']}（[スレッド]({i['permalink']})）" for i in state["items"])
        body = f"Slack の fb で受けたフィードバックの反映。1 commit = 1 フィードバック。\n\n{lines}\n"
        if state.get("pr"):
            _api("PATCH", f"/pulls/{state['pr']}", {"body": body})
        else:
            pr = _api("POST", "/pulls", {"title": state["title"], "head": state["branch"], "base": "main", "body": body})
            state.update(pr=pr["number"], pr_url=pr["html_url"])
            _save(state)
        return {"number": state["pr"], "url": state["pr_url"], "summary": summary, "permalink": p["permalink"]}


def discard():
    """確認待ちの修正を捨てる。"""
    with _lock:
        state = _load()
        _git(WORKTREE, "reset", "-q", "--hard")
        _git(WORKTREE, "clean", "-fdq")
        state["pending"] = None
        _save(state)


def start_sync(on_update):
    """マージされた PR を稼働中の作業ツリーに取り込むスレッドを起動する。取り込んだら on_update(報告文) を呼ぶ。"""

    def loop():
        while True:
            time.sleep(SYNC_INTERVAL_SEC)
            try:
                if text := sync():
                    on_update(text)
            except Exception:
                log.exception("sync failed")

    threading.Thread(target=loop, daemon=True).start()


def sync():
    """origin/main が先に進んでいれば fast-forward で取り込み、報告文を返す（何もしなければ None）。"""
    with _lock:
        _fetch("main")
        if _git(ROOT_DIR, "symbolic-ref", "--short", "HEAD") != "main":
            return None
        head, remote = _git(ROOT_DIR, "rev-parse", "HEAD"), _git(ROOT_DIR, "rev-parse", "origin/main")
        # 手元からサーバーに先に push した直後などは HEAD の方が新しいので、何もしない
        if head == remote or not _git_ok(ROOT_DIR, "merge-base", "--is-ancestor", head, remote):
            return None
        changed = _git(ROOT_DIR, "diff", "--name-only", head, remote).splitlines()
        _git(ROOT_DIR, "merge", "-q", "--ff-only", remote)
    log.info("synced %s..%s: %s", head[:7], remote[:7], changed)
    text = f"main の更新を取り込みました（{head[:7]} → {remote[:7]}）。次の依頼から反映されます。"
    if any(name.startswith(RESTART_PATHS) for name in changed):
        text += "\nコードの変更を含むので、bot の再起動が必要です。"
    return text


def _prepare(state):
    """worktree を、積んでいる途中のブランチ（なければ origin/main から切った新しいブランチ）の先頭にそろえる。"""
    _fetch("main")
    if not WORKTREE.exists():
        _git(ROOT_DIR, "worktree", "prune")
        _git(ROOT_DIR, "worktree", "add", "-q", "--detach", str(WORKTREE), "origin/main")
    if state.get("items") and (not state.get("pr") or _api("GET", f"/pulls/{state['pr']}")["state"] == "open"):
        # GitHub 上でブランチが直されていても追いつけるよう、毎回取り直す
        _fetch(state["branch"])
        base = f"origin/{state['branch']}"
    else:
        now = datetime.now()
        state.update(branch=f"feedback/{now:%Y%m%d-%H%M%S}", title=f"フィードバック反映（{now:%m/%d}〜）", pr=None, pr_url=None, items=[])
        base = "origin/main"
    _git(WORKTREE, "checkout", "-q", "-f", "-B", state["branch"], base)
    _git(WORKTREE, "clean", "-fdq")
    # 積んでいる PR のブランチが main から遅れていると、そのあと main に入ったファイル（新しいプロファイルなど）が
    # 作業コピーに無く、Claude が同じファイルを新しく作って PR が main と衝突する。先に main を取り込んでおく
    # （取り込んだ commit は、次の ok で PR のブランチに push される）
    if base != "origin/main" and not _git_ok(WORKTREE, "merge", "-q", "--no-edit", "origin/main"):
        _git_ok(WORKTREE, "merge", "--abort")
        raise RuntimeError(f"PR のブランチ {state['branch']} に main を取り込めませんでした（衝突）。PR を先にマージするか閉じてください")


def _fetch(branch):
    _git(ROOT_DIR, "fetch", "-q", REPO_URL, f"+refs/heads/{branch}:refs/remotes/origin/{branch}")


def _load():
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}


def _save(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def _git_env():
    basic = base64.b64encode(f"x-access-token:{GITHUB_TOKEN}".encode()).decode()
    name, email = GIT_AUTHOR
    return {
        **CLAUDE_ENV,
        "GIT_TERMINAL_PROMPT": "0",
        # トークンはコマンドライン引数に載せない（同じサーバーの他のユーザーから ps で見えるため）
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": name,
        "GIT_COMMITTER_EMAIL": email,
    }


def _run_git(cwd, args):
    return subprocess.run(["git", *args], cwd=cwd, env=_git_env(), capture_output=True, text=True, timeout=120)


def _git(cwd, *args):
    result = _run_git(cwd, args)
    if result.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {result.stderr.strip()[-500:]}")
    return result.stdout.strip()


def _git_ok(cwd, *args):
    return _run_git(cwd, args).returncode == 0


def _api(method, path, body=None):
    request = urllib.request.Request(
        f"https://api.github.com/repos/{GITHUB_REPO}{path}",
        data=None if body is None else json.dumps(body).encode(),
        method=method,
        headers={
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"GitHub API {method} {path} failed: {e.code} {e.read().decode()[:300]}") from None
