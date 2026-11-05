"""処理中のスレッドに出すステータス表示（シマー）。

Claude の stream-json イベントを短い日本語に変換し、assistant.threads.setStatus で表示する。
"""

import logging
import threading
import time
from pathlib import Path

from .config import APP_NAME, STATUS_HEARTBEAT_SEC, STATUS_MAX_LEN

log = logging.getLogger(__name__)


def shorten(text, limit=40):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def describe_tool(name, tool_input):
    """ツール呼び出しをステータス用の短い日本語にする。"""
    path = Path(tool_input.get("file_path") or tool_input.get("notebook_path") or "").name
    if name == "Bash":
        return f"実行中: {shorten(tool_input.get('description') or tool_input.get('command', ''))}"
    if name == "Read":
        return f"{path} を読んでいます..."
    if name in ("Edit", "MultiEdit", "Write", "NotebookEdit"):
        return f"{path} を編集しています..."
    if name == "Grep":
        return f"「{shorten(tool_input.get('pattern', ''), 30)}」を検索しています..."
    if name == "Glob":
        return f"ファイルを探しています: {shorten(tool_input.get('pattern', ''), 30)}"
    if name == "WebSearch":
        return f"Web を検索しています: {shorten(tool_input.get('query', ''))}"
    if name == "WebFetch":
        return "Web ページを読んでいます..."
    if name in ("Task", "Agent"):
        return f"サブエージェントに依頼中: {shorten(tool_input.get('description', ''))}"
    if name in ("TodoWrite", "TaskCreate", "TaskUpdate", "TaskList", "TaskGet"):
        return "作業の段取りを整理しています..."
    if name == "ToolSearch":
        return "使うツールを準備しています..."
    if name.startswith("mcp__"):
        server = name.split("__")[1]
        return f"{server.removeprefix('claude_ai_').replace('_', ' ')} を使っています..."
    return f"{name} を使っています..."


def status_from_event(event):
    """claude の stream-json イベントから、ステータスに出す文言を作る（なければ None）。"""
    if event.get("type") == "system" and event.get("subtype") == "thinking_tokens":
        return "考え中..."
    if event.get("type") != "assistant":
        return None
    content = event.get("message", {}).get("content", [])
    tools = [c for c in content if c.get("type") == "tool_use"]
    if tools:
        text = describe_tool(tools[0].get("name", ""), tools[0].get("input") or {})
        return text + (f"（ほか {len(tools) - 1} 件）" if len(tools) > 1 else "")
    if any(c.get("type") == "text" for c in content):
        return "返答をまとめています..."
    if any(c.get("type") == "thinking" for c in content):
        return "考え中..."
    return None


class StatusReporter:
    """スレッドの「<アプリ名> (モデル/エフォート) 〇〇中...」表示（シマー）を更新する。

    setStatus が使えない会話では何もしない。
    """

    def __init__(self, client, channel, thread_ts, model_label, effort):
        self.client = client
        self.channel = channel
        self.thread_ts = thread_ts
        # status にはアプリ名を Slack が前に付けるので、その後ろにモデルとエフォートを出す
        self.prefix = f"({model_label}/{effort}) "
        # loading_messages にはアプリ名が付かないので、自分で付ける
        self.loading_messages = [
            f"{APP_NAME} {self.prefix}{text}"
            for text in ("依頼内容を読んでいます...", "Claude を起動しています...", "進め方を考えています...")
        ]
        self.enabled = True
        self._text = None
        self._step_started = time.monotonic()
        self._lock = threading.Lock()
        # stop() の後にハートビートがステータスを出し直さないよう、送信と停止を直列化する
        self._send_lock = threading.Lock()
        self._stop = threading.Event()
        self._heartbeat = threading.Thread(target=self._heartbeat_loop, daemon=True)

    def start(self):
        self._text = "考え中..."
        self._send(self._text, loading_messages=self.loading_messages)
        if self.enabled:
            self._heartbeat.start()

    def on_event(self, event):
        """claude の stream-json イベントを受け取り、必要ならステータスを更新する。"""
        if text := status_from_event(event):
            self.update(text)

    def update(self, text):
        with self._lock:
            if not self.enabled or text == self._text:
                return
            self._text = text
            self._step_started = time.monotonic()
        log.info("status: %s", text)
        self._send(text)

    def refresh(self):
        """途中でスレッドに投稿すると消えるステータスを、出し直す。"""
        with self._lock:
            text = self._text
        if text:
            self._send(text)

    def stop(self):
        # 返信を投稿すると Slack 側でステータスは自動的に消える
        with self._send_lock:
            self._stop.set()

    def clear(self):
        self.stop()
        self._send("")

    def _heartbeat_loop(self):
        while not self._stop.wait(STATUS_HEARTBEAT_SEC):
            with self._lock:
                text, elapsed = self._text, int(time.monotonic() - self._step_started)
            if elapsed >= 60:
                text = f"{text}（{elapsed // 60} 分経過）"
            self._send(text)

    def _send(self, text, loading_messages=None):
        with self._send_lock:
            if not self.enabled or (self._stop.is_set() and text):
                return
            try:
                self.client.assistant_threads_setStatus(
                    channel_id=self.channel,
                    thread_ts=self.thread_ts,
                    status=shorten(self.prefix + text, STATUS_MAX_LEN) if text else "",
                    loading_messages=loading_messages,
                )
            except Exception as e:
                log.warning("setStatus failed, disabling status updates: %s", e)
                self.enabled = False
