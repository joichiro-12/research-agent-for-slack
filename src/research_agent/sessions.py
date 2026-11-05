"""Slack スレッドと Claude セッションの対応表（runtime/sessions.json）。

キーは "チャンネルID:スレッドts"。メンションされたスレッドは値 None で登録され、
初回の実行後に Claude のセッション ID が入る。登録済みのスレッドではメンションなしでも応じる。
"""

import json
import threading

from .config import SESSIONS_FILE

_lock = threading.Lock()


def thread_key(channel, thread_ts):
    return f"{channel}:{thread_ts}"


def load():
    if not SESSIONS_FILE.exists():
        return {}
    return json.loads(SESSIONS_FILE.read_text())


def is_registered(key):
    return key in load()


def get(key):
    return load().get(key)


def save(key, session_id):
    with _lock:
        sessions = load()
        sessions[key] = session_id
        _write(sessions)


def register(key):
    with _lock:
        sessions = load()
        if key not in sessions:
            sessions[key] = None
            _write(sessions)


def _write(sessions):
    SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    # ターンの途中でも書くので、書きかけを読んだり、途中で止まって壊したりしないよう、書き終えてから入れ替える
    temp = SESSIONS_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(sessions, indent=2))
    temp.replace(SESSIONS_FILE)
