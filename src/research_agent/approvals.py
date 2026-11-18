"""Slack での承認の状態（runtime/approvals.json）。

ガード（guard.py。Claude Code のフックとして別プロセスで動く）と bot の両方が読み書きするので、
ファイルロックをかけて扱う。スレッドごとに次の 2 つを持つ。
- pending: 承認待ちのコマンド。ガードが積み、bot がスレッドで承認を求める。
  その操作をせずに済ませるときは、Claude が agent-approval で取り下げる（取り下げたものは、bot がスレッドに出さない）
- granted: 承認済みで、まだ実行していないコマンド。1 回実行すると消え、GRANT_TTL_SEC を過ぎても消える
コマンドは全文の SHA-256 で照合する（承認したものと 1 文字でも違えば、承認し直しになる）。
フックから bot の設定（.env）を読まずに使えるよう、標準ライブラリだけで書く。
"""

import argparse
import fcntl
import hashlib
import json
import os
import secrets
import sys
import time
from contextlib import contextmanager
from pathlib import Path

# 既定は bot の runtime/（config.RUNTIME_DIR と同じ場所）。試すときは AGENT_RUNTIME で差し替える
RUNTIME_DIR = Path(os.environ.get("AGENT_RUNTIME") or Path(__file__).resolve().parents[2] / "runtime")
STATE_FILE = RUNTIME_DIR / "approvals.json"
LOCK_FILE = RUNTIME_DIR / "approvals.lock"
# ガードが止めたコマンドの記録。毎回承認しているものを許可に移すときの材料にする
LOG_FILE = RUNTIME_DIR / "guard.log"
GRANT_TTL_SEC = 3600


def _digest(command):
    return hashlib.sha256(command.encode()).hexdigest()


@contextmanager
def _state():
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOCK_FILE, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
        yield state
        # 空になったスレッドは消す
        state = {k: v for k, v in state.items() if v["pending"] or v["granted"]}
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def _entry(state, thread):
    entry = state.setdefault(thread, {"pending": [], "granted": []})
    now = time.time()
    entry["granted"] = [g for g in entry["granted"] if g["until"] > now]
    return entry


def request(thread, command, reason):
    """承認待ちに積み、承認 ID を返す（同じコマンドがすでに待っていれば、その ID）。"""
    digest = _digest(command)
    with _state() as state:
        entry = _entry(state, thread)
        for item in entry["pending"]:
            if item["hash"] == digest:
                return item["id"]
        item = {
            "id": secrets.token_hex(3),
            "hash": digest,
            "command": command,
            "reason": reason,
            "at": time.time(),
            "posted": False,
        }
        entry["pending"].append(item)
        return item["id"]


def consume(thread, command):
    """承認済みのコマンドなら、その承認を使い切って True を返す。"""
    digest = _digest(command)
    with _state() as state:
        entry = _entry(state, thread)
        for grant in entry["granted"]:
            if grant["hash"] == digest:
                entry["granted"].remove(grant)
                return True
        return False


def pending(thread):
    with _state() as state:
        return list(_entry(state, thread)["pending"])


def take_unposted(thread):
    """まだスレッドに出していない承認待ちを返し、出したことにする。"""
    with _state() as state:
        items = [item for item in _entry(state, thread)["pending"] if not item["posted"]]
        for item in items:
            item["posted"] = True
        return items


def _split(pending, ids):
    """承認待ちを (答えるもの, 残すもの) に分ける。ids がなければ全部に答える。"""
    if ids is None:
        return pending, []
    return [item for item in pending if item["id"] in ids], [item for item in pending if item["id"] not in ids]


def approve(thread, ids=None):
    """承認待ちを承認済みにして、承認したものを返す。ids を渡すと、その承認 ID のものだけ。"""
    with _state() as state:
        entry = _entry(state, thread)
        items, entry["pending"] = _split(entry["pending"], ids)
        until = time.time() + GRANT_TTL_SEC
        entry["granted"] += [{"hash": item["hash"], "until": until} for item in items]
        return items


def withdraw(thread, ids=None):
    """承認待ちを取り下げて、取り下げたものを返す（却下されたときや、別の指示が来たとき）。ids を渡すと、その承認 ID のものだけ。"""
    with _state() as state:
        entry = _entry(state, thread)
        items, entry["pending"] = _split(entry["pending"], ids)
        return items


def log(record):
    """ガードの判定を guard.log に 1 行 1 件の JSON で残す。"""
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps({"time": time.strftime("%Y-%m-%d %H:%M:%S"), **record}, ensure_ascii=False) + "\n")


def main():
    """agent-approval: 承認の依頼を Claude が取り下げる。承認はできない（答えるのは Slack の利用者だけ）。"""
    parser = argparse.ArgumentParser(prog="agent-approval", description="ガードが止めたコマンドの承認の依頼を取り下げる")
    parser.add_argument("--withdraw", nargs="+", required=True, metavar="ID", help="取り下げる承認 ID（ガードの返事にある）")
    args = parser.parse_args()
    thread = os.environ.get("AGENT_THREAD")
    if not thread:
        sys.exit("agent-approval は bot から動かした Claude の中でだけ使える（AGENT_THREAD がない）")
    items = withdraw(thread, args.withdraw)
    for item in items:
        log({"thread": thread, "verdict": "withdrawn", "id": item["id"], "command": item["command"][:2000]})
    if items:
        print(f"取り下げた: {' '.join(item['id'] for item in items)}。承認の案内はスレッドに出さない（出していたら、このターンの終わりに消す）")
    if missing := [i for i in args.withdraw if i not in {item["id"] for item in items}]:
        sys.exit(f"このスレッドの承認待ちにない: {' '.join(missing)}")


if __name__ == "__main__":
    main()
