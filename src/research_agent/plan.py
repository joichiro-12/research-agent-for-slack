"""Claude の段取り（TaskCreate・TaskUpdate）と、いま待っていることを、スレッドのメッセージとして出す。

Claude のタスクはセッションの中で続くので、段取りも 1 回の実行ではなくスレッドに持つ（threadstate.py の plan）。
- 手順が全部終わるまでは、ターンをまたいでも同じ段取りを更新する。新しい手順も同じ段取りに足す
- 全部終わったあとに手順が登録されたら、新しいメッセージで次の段取りを始める
- 前のターンから表示が変わったら、メッセージをスレッドの末尾に出し直す（古い位置を書き換えても目に入らないので、前のメッセージは消す）
- ターンを終えるときは、何を待っているかを添える（承認、なくて手順が残っていれば返事）
"""

import logging
import re

from . import threadstate
from .config import (
    PLAN_APPROVAL_MARK,
    PLAN_BAR,
    PLAN_BAR_MAX,
    PLAN_MARKS,
    PLAN_SHOWN_MAX,
    WAIT_APPROVAL,
    WAIT_REPLY,
)

log = logging.getLogger(__name__)

# TaskCreate の結果（"Task #1 created successfully: ..."）から、手順の ID を取る
CREATED = re.compile(r"Task #(\S+) created")


class PlanReporter:
    def __init__(self, client, channel, thread_ts, key=None, on_post=None):
        self.client = client
        self.channel = channel
        self.thread_ts = thread_ts
        # スレッドのキー。None なら状態を残さない（フィードバックは別のセッションで動くので、スレッドの段取りと混ぜない）
        self.key = key
        # 段取りを投稿したあとに呼ぶ（投稿で消えるステータスを出し直す）
        self.on_post = on_post
        saved = threadstate.get(key).get("plan", {}) if key else {}
        # 手順の ID → {"subject": 手順名, "status": pending・in_progress・completed}（登録順）
        self.tasks = saved.get("tasks", {})
        self._session = saved.get("session")
        self._ts = saved.get("ts")
        self._text = saved.get("text")
        # 前のターンの終わりの表示。ターンの終わりにこれと違っていれば、末尾に出し直す
        # （ターンの外の書き換え（refresh）では変えない）
        self._settled = saved.get("settled", self._text)
        # このターンで、もう末尾に出した
        self._placed = False
        self._running = False
        self._approval = False
        # 手順の下に添える行（いま待っていること）
        self._wait = []
        # 結果を待っている TaskCreate・TaskUpdate（tool_use の ID → (ツール名, 入力)）
        self._calls = {}

    def start(self):
        """ターンの始まり。待っていることの表示を外す（終わるときに付け直す）。"""
        self._running = True
        self._wait = []
        self._show(move=False)

    def on_event(self, event):
        """claude の stream-json イベントを受け取り、段取りが変わったらメッセージを書き換える。"""
        session = event.get("session_id")
        if session and session != self._session:
            # セッションが変わると、手順の ID は振り直される
            if self._session:
                self._forget()
            self._session = session
        # tool の呼び出しと結果は assistant と user のイベントにある（system などでは message が文字列のこともある）
        message = event.get("message")
        if event.get("type") not in ("assistant", "user") or not isinstance(message, dict):
            return
        content = message.get("content")
        if not isinstance(content, list):
            return
        changed = False
        for block in content:
            if block.get("type") == "tool_use" and block.get("name") in ("TaskCreate", "TaskUpdate"):
                self._calls[block.get("id")] = (block["name"], block.get("input") or {})
            elif block.get("type") == "tool_result" and block.get("tool_use_id") in self._calls:
                name, tool_input = self._calls.pop(block["tool_use_id"])
                if not block.get("is_error"):
                    changed |= self._apply(name, tool_input, _text(block.get("content")))
        if changed:
            self._show()

    def finish(self, approval=False):
        """ターンの終わり。いま待っていることを添える。前のターンから表示が変わっていたら、末尾に出し直す。"""
        self._running = False
        self._set_wait(approval)
        self._show(move=self._render() != self._settled)
        if self._ts:
            self._settled = self._text
            self._save()

    def refresh(self, approval):
        """ターンの外で、待っていることだけを書き換える。末尾には出し直さない。"""
        self._set_wait(approval)
        self._show(move=False)

    def _set_wait(self, approval):
        self._approval = approval
        wait = []
        if approval:
            wait.append(WAIT_APPROVAL)
        if self.key and not wait and _unfinished(self.tasks):
            wait.append(WAIT_REPLY)
        self._wait = wait

    def _apply(self, name, tool_input, result):
        if name == "TaskCreate":
            if not (m := CREATED.search(result)):
                return False
            # 前の段取りが全部終わっていたら、次の段取りとして新しいメッセージで始める
            if self.tasks and not _unfinished(self.tasks):
                self._forget()
            self.tasks[m[1]] = {"subject": tool_input.get("subject", ""), "status": "pending"}
            return True
        task_id = str(tool_input.get("taskId"))
        # 段取りに載っていない手順は、手順名が分からないので出さない
        if task_id not in self.tasks:
            return False
        if tool_input.get("status") == "deleted":
            del self.tasks[task_id]
        else:
            self.tasks[task_id].update({k: tool_input[k] for k in ("subject", "status") if tool_input.get(k)})
        return True

    def _forget(self):
        """いまの段取りから離れる。前のメッセージは、そのまま記録として残す。"""
        self.tasks, self._ts, self._text, self._settled, self._placed = {}, None, None, None, False
        self._save()

    def _mark(self, status):
        if status == "in_progress" and not self._running:
            # ターンの外では何も動いていない。承認を待っている手順に印を付け、それ以外は戻す
            return PLAN_APPROVAL_MARK if self._approval else PLAN_MARKS["pending"]
        return PLAN_MARKS.get(status, PLAN_MARKS["pending"])

    def _render(self):
        lines = [f"*Steps*  {_bar(list(self.tasks.values()))}"] if self.tasks else []
        # 長くなったら、終わった手順を古い方から畳む
        extra = max(0, len(self.tasks) - PLAN_SHOWN_MAX)
        folded = [task_id for task_id, task in self.tasks.items() if task["status"] == "completed"][:extra]
        if folded:
            lines.append(f"{PLAN_MARKS['completed']} ほか {len(folded)} 件")
        lines += [f"{self._mark(task['status'])} {task['subject']}" for task_id, task in self.tasks.items() if task_id not in folded]
        if self._wait:
            lines += ["", *self._wait]
        return "\n".join(lines)

    def _show(self, move=True):
        # 手順がなくなったら、メッセージも消す
        if not self.tasks:
            if self._ts:
                self._delete(self._ts)
                self._ts, self._text = None, None
                self._save()
            return
        text = self._render()
        if text == self._text:
            return
        self._text = text
        try:
            self._send(text, move)
        except Exception:
            log.exception("failed to show the plan")
            # 消されたメッセージを書き換えようとしたときなど。次の変更で出し直す
            self._ts = None
        self._save()

    def _send(self, text, move):
        if self._ts and (self._placed or not move):
            self.client.chat_update(channel=self.channel, ts=self._ts, text=text)
            return
        if self._ts:
            self._delete(self._ts)
        self._ts = self.client.chat_postMessage(channel=self.channel, thread_ts=self.thread_ts, text=text)["ts"]
        self._placed = True
        if self.on_post:
            self.on_post()

    def _delete(self, ts):
        try:
            self.client.chat_delete(channel=self.channel, ts=ts)
        except Exception as e:
            log.warning("could not delete the old plan message: %s", e)

    def _save(self):
        if not self.key:
            return
        try:
            threadstate.update(
                self.key,
                plan={"ts": self._ts, "text": self._text, "settled": self._settled, "session": self._session, "tasks": self.tasks},
            )
        except OSError:
            log.exception("failed to save the plan")


def _unfinished(tasks):
    return any(task["status"] != "completed" for task in tasks.values())


def _bar(tasks):
    """進み具合のバー。切り捨てで埋めるので、全部終わるまで満タンにはならない。"""
    width = min(len(tasks), PLAN_BAR_MAX)
    filled = sum(task["status"] == "completed" for task in tasks) * width // len(tasks)
    return PLAN_BAR[0] * filled + PLAN_BAR[1] * (width - filled)


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
    return ""
