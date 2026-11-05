"""チャンネルの作業場（profile.work_dir）まわり：Slack に添付するファイルの置き場と、git での同期。

- outbox: Claude が <work_dir>/outbox/<スレッド>/ に置いたファイルを、返信のあとにスレッドへ添付し、sent/ に移す。
  Write を許していないプロファイルでも、そのスレッドの outbox にだけは書ける（bot.handle_request）
- git_sync: 依頼の前に pull し、終わったら変更を commit して push する（手元の PC など、ほかの環境と中身をそろえるため）。
  同じ作業場で同時に動いている依頼があるときは、pull を最初の 1 件、push を最後の 1 件に任せる
  （ほかの依頼が書きかけのファイルを巻き込まないため）
"""

import logging
import shutil
import subprocess
import threading
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path

from .config import GIT_AUTHOR

log = logging.getLogger(__name__)

OUTBOX = "outbox"
SENT = "sent"

_lock = threading.Lock()
_running = defaultdict(int)


def outbox_dir(work_dir, thread_ts):
    return Path(work_dir) / OUTBOX / thread_ts


def unsent(outbox):
    """outbox の直下にある、まだ送っていないファイル。"""
    if not outbox.is_dir():
        return []
    return sorted(p for p in outbox.iterdir() if p.is_file())


def mark_sent(path):
    sent = path.parent / SENT
    sent.mkdir(exist_ok=True)
    shutil.move(path, sent / path.name)


@contextmanager
def synced(profile, message):
    """依頼のあいだ、作業場を git でそろえる。同期に失敗したときの説明を、yield したリストに入れる。

    リストには with を抜けたあと（push のあと）に書き足されることがあるので、抜けてから読む。
    """
    errors = []
    if not profile.git_sync:
        yield errors
        return
    work_dir = profile.work_dir
    with _lock:
        first = _running[work_dir] == 0
        _running[work_dir] += 1
        if first:
            try:
                _pull(work_dir)
            except Exception as e:
                log.warning("workspace pull failed: %s", e)
                errors.append(f"作業場の pull に失敗しました: {e}")
    try:
        yield errors
    finally:
        with _lock:
            _running[work_dir] -= 1
            if _running[work_dir] == 0:
                try:
                    _push(work_dir, message)
                except Exception as e:
                    log.warning("workspace push failed: %s", e)
                    errors.append(f"作業場の commit・push に失敗しました: {e}")


def _pull(work_dir):
    try:
        _git(work_dir, "pull", "--rebase", "--autostash", "--quiet")
    except Exception:
        # 衝突したまま残さない
        subprocess.run(["git", "rebase", "--abort"], cwd=work_dir, capture_output=True)
        raise


def _push(work_dir, message):
    _git(work_dir, "add", "-A")
    if _git(work_dir, "status", "--porcelain").strip():
        _git(work_dir, "commit", "--quiet", "-m", message)
    if _git(work_dir, "rev-list", "@{upstream}..HEAD").strip():
        _pull(work_dir)
        _git(work_dir, "push", "--quiet")


def _git(work_dir, *args):
    name, email = GIT_AUTHOR
    result = subprocess.run(
        ["git", "-c", f"user.name={name}", "-c", f"user.email={email}", *args],
        cwd=work_dir, capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {args[0]}: {(result.stderr or result.stdout).strip()[-500:]}")
    return result.stdout
