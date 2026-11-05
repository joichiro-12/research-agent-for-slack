"""スレッドごとの状態（runtime/threads.json）。Claude のセッションの外で、bot がスレッドについて覚えておくこと。

1 つのスレッドで研究を進めると、ターン（claude -p の 1 回の実行）がいくつも続く。ターンをまたいで要るものをここに置く。
- plan: 段取りのメッセージ（plan.py）
- brief: スレッドの題と URL（bot.thread_brief）
- turns: 受けたのに、まだ終わっていない依頼（順番待ちも含む）。bot が途中で止まるとここに残り、次の起動で続きをさせる（bot.resume_thread）
"""

import json
import logging
import threading
from contextlib import contextmanager

from .config import THREADS_FILE

log = logging.getLogger(__name__)

_lock = threading.RLock()


def _load():
    if not THREADS_FILE.exists():
        return {}
    try:
        return json.loads(THREADS_FILE.read_text())
    except json.JSONDecodeError:
        log.warning("%s is not valid JSON; starting over", THREADS_FILE)
        return {}


def _write(state):
    THREADS_FILE.parent.mkdir(parents=True, exist_ok=True)
    # 途中で落ちても壊れないよう、書き終えてから入れ替える
    temp = THREADS_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    temp.replace(THREADS_FILE)


def get(key):
    with _lock:
        return _load().get(key, {})


def update(key, **fields):
    with _lock:
        state = _load()
        state.setdefault(key, {}).update(fields)
        _write(state)


@contextmanager
def turn(key, ts, **record):
    """依頼を受けてから終わるまで、記録を残す。ts はその依頼のメッセージ。

    例外で抜けても消す。bot のプロセスが死んだときだけ、記録が残る（シグナルで死ぬと finally は走らない）。
    """
    with _lock:
        update(key, turns={**get(key).get("turns", {}), ts: record})
    try:
        yield
    finally:
        end_turn(key, ts)


def end_turn(key, ts):
    """依頼の記録を消す（もう消えていれば何もしない）。"""
    with _lock:
        turns = get(key).get("turns", {})
        if ts in turns:
            del turns[ts]
            update(key, turns=turns)


def take_unfinished_turns():
    """記録が残っている依頼を {スレッド: {ts: 記録}} で取り出して消す。

    続きを動かす前に消すので、続きの途中で落ちるのを繰り返しても、同じ記録から何度も動くことはない。
    """
    with _lock:
        state = _load()
        unfinished = {key: thread.pop("turns") for key, thread in state.items() if thread.get("turns")}
        if unfinished:
            _write(state)
        return unfinished
