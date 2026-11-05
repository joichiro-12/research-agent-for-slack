"""claude -p の実行と、前の bot が残した claude の後始末。"""

import json
import logging
import os
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .config import CLAUDE_BIN, CLAUDE_ENV, STOP_WAIT_SEC, TIMEOUT_SEC

log = logging.getLogger(__name__)

PROC_DIR = Path("/proc")


def run_claude(prompt, session_id, model, profile, on_event, env=None):
    """claude -p を stream-json で実行し、イベントを on_event に流しながら (返答, セッションID, エラーか) を返す。

    profile.work_dir で、profile.effort のエフォートで動かす。env は claude（とそのフック）に足す環境変数。
    """
    cmd = [
        CLAUDE_BIN, "-p",
        "--model", model,
        "--effort", profile.effort,
        "--output-format", "stream-json", "--verbose",
        "--settings", profile.settings,
    ]
    if session_id:
        cmd += ["--resume", session_id]
    log.info("run claude (profile=%s, model=%s, effort=%s, resume=%s): %s", profile.name, model, profile.effort, session_id, prompt[:80])

    timed_out = threading.Event()
    with (
        tempfile.TemporaryFile(mode="w+") as stderr,
        tempfile.NamedTemporaryFile(mode="w", suffix=".md") as system_prompt,
    ):
        # システムプロンプトは引数だと ps で他のユーザーに見えるので、本人だけが読める一時ファイルで渡す
        system_prompt.write(profile.prompt)
        system_prompt.flush()
        cmd += ["--append-system-prompt-file", system_prompt.name]
        proc = subprocess.Popen(
            cmd,
            cwd=profile.work_dir,
            env={**CLAUDE_ENV, **(env or {})},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            text=True,
        )
        # プロンプトは stdin で渡す（"-" で始まる文字列をフラグと誤認させないため）
        proc.stdin.write(prompt)
        proc.stdin.close()
        timer = threading.Timer(TIMEOUT_SEC, lambda: (timed_out.set(), proc.kill()))
        timer.start()
        result = None
        try:
            for line in proc.stdout:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                session_id = event.get("session_id") or session_id
                if event.get("type") == "result":
                    result = event
                else:
                    on_event(event)
            proc.wait()
        finally:
            timer.cancel()
        if timed_out.is_set():
            raise subprocess.TimeoutExpired(cmd, TIMEOUT_SEC)
        if result is None:
            stderr.seek(0)
            detail = (stderr.read() or "no output").strip()[-1500:]
            return f"claude が結果を返さずに終了しました (exit {proc.returncode}):\n```\n{detail}\n```", session_id, True
    return result.get("result") or "(返答なし)", session_id, bool(result.get("is_error"))


def stop_leftovers():
    """前の bot が動かしていた claude が残っていれば止める（同じセッションを 2 つの claude で動かさないため）。

    新しいターンを始める前に呼ぶ。/proc を読むので、Linux でだけ働く。確かめた動き（claude 2.1.296）：
    - tmux kill-session: claude は SIGHUP で実行中のコマンドを止めて、3 秒ほどで終わる。新しい bot はそれより早く起動する
    - bot だけが落ちる: claude は出力先がなくなっても、ターンの最後まで動き続ける。SIGTERM なら 3 秒ほどで終わる
    """
    if not (pids := _leftovers()):
        return
    log.warning("stopping claude left by the previous bot: %s", pids)
    _send(pids, signal.SIGTERM)
    deadline = time.monotonic() + STOP_WAIT_SEC
    while (pids := _leftovers()) and time.monotonic() < deadline:
        time.sleep(0.2)
    _send(pids, signal.SIGKILL)


def _leftovers():
    """bot が動かした claude のプロセス番号を返す。bot は claude に必ず AGENT_THREAD を渡すので、環境変数で見分ける。"""
    found = []
    for path in PROC_DIR.glob("[0-9]*"):
        try:
            if (path / "comm").read_text().strip() != Path(CLAUDE_BIN).name:
                continue
            if any(item.startswith(b"AGENT_THREAD=") for item in (path / "environ").read_bytes().split(b"\0")):
                found.append(int(path.name))
        except (OSError, ValueError):
            # 調べているあいだに終わったプロセスや、ほかのユーザーのプロセス
            continue
    return found


def _send(pids, sig):
    for pid in pids:
        try:
            os.kill(pid, sig)
        except OSError:
            pass
