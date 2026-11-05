"""bot の再起動で止まったターンの続き（threadstate.turn、bot.resume_thread、claude.stop_leftovers）を確かめる。

再起動で bot が死ぬと finally は走らないので、依頼を子プロセスで動かして本当に SIGKILL で殺し、そのあとに bot.main を動かす。

実行: uv run python -m unittest discover -s tests
"""

import importlib
import json
import logging
import os
import signal
import tempfile
import threading
import time
import traceback
import unittest
import warnings
from dataclasses import replace
from pathlib import Path
from unittest import mock

# config.py は Slack のトークンを環境変数から読むので、試すときは仮の値を入れておく
for name in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "ALLOWED_USER_IDS"):
    os.environ.setdefault(name, "test")

from research_agent import approvals, claude, feedback, profiles, sessions, threadstate, workspace  # noqa: E402
from research_agent.config import RESTART_GIVE_UP, RESTART_NOTICE, WAIT_REPLY  # noqa: E402

KEY = "C1:100.1"
SESSION = "session-1"
REPLY = "返答"


def init(on_event):
    """claude は最初のイベントでセッション ID を出す。"""
    on_event({"type": "system", "subtype": "init", "session_id": SESSION})


def die(on_event=None):
    os.kill(os.getpid(), signal.SIGKILL)


def init_and_die(on_event):
    init(on_event)
    die()


class RestartTestCase(unittest.TestCase):
    """状態は一時的なフォルダに置き、Claude の実行と投稿はファイルに残す（子プロセスの分も読めるように）。"""

    @classmethod
    def setUpClass(cls):
        # bot.py は読み込むときにログの出力先を設定するので、ほかのテストの出力に混ざらないよう止める
        with mock.patch("slack_bolt.App"), mock.patch("logging.basicConfig"):
            cls.bot = importlib.import_module("research_agent.bot")

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.client = mock.MagicMock()
        self.client.chat_postMessage.return_value = {"ts": "300.1"}
        self.profile = profiles.Profile("default", "指示", "{}", self.folder / "work")
        self.during_claude = init
        self.mocks = {name: mock.Mock() for name in ("stop_leftovers", "socket")}
        patches = (
            (approvals, "RUNTIME_DIR", self.folder),
            (approvals, "STATE_FILE", self.folder / "approvals.json"),
            (approvals, "LOCK_FILE", self.folder / "approvals.lock"),
            (threadstate, "THREADS_FILE", self.folder / "threads.json"),
            (sessions, "SESSIONS_FILE", self.folder / "sessions.json"),
            (feedback, "enabled", lambda: False),
            (profiles, "for_channel", lambda client, channel: self.profile),
            (self.bot, "RUNTIME_DIR", self.folder),
            (self.bot, "WORK_DIR", self.folder / "work"),
            (self.bot, "run_claude", self.fake_claude),
            (self.bot, "post_reply", self.fake_post),
            (self.bot, "stop_leftovers", self.mocks["stop_leftovers"]),
            (self.bot, "SocketModeHandler", self.mocks["socket"]),
            (self.bot.app, "client", self.client),
        )
        for target, attr, value in patches:
            patcher = mock.patch.object(target, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def fake_claude(self, prompt, session, model, profile, on_event, env=None):
        run = {"prompt": prompt, "session": session, "model": model, "effort": profile.effort, "system": profile.prompt}
        self._append("claude.jsonl", run)
        self.during_claude(on_event)
        return REPLY, SESSION, False

    def fake_post(self, client, channel, thread_ts, text):
        self._append("posts.jsonl", text)
        return "300.1"

    def _append(self, name, item):
        with open(self.folder / name, "a") as f:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    def _read(self, name):
        path = self.folder / name
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def runs(self):
        return self._read("claude.jsonl")

    def posts(self):
        return self._read("posts.jsonl")

    def request(self, prompt="元の依頼", ts="200.1", model="opus", user="U1", **kwargs):
        event = {"channel": "C1", "ts": ts, "thread_ts": "100.1", "user": user}
        self.bot.handle_request(self.client, event, prompt, model, **kwargs)

    def start_bot(self):
        """bot を起動して、続きのターンが終わるのを待つ。"""
        self.bot.main()
        for thread in threading.enumerate():
            if thread.name.startswith("resume-"):
                thread.join(30)

    def in_bot_process(self, work):
        """work を別のプロセスで動かして、終わるのを待つ。プロセスの終わり方（waitpid の status）を返す。"""
        with warnings.catch_warnings():
            # ほかのテストのスレッドが残っていると警告が出る。子プロセスは固まったら SIGALRM で死ぬので、テストは止まらない
            warnings.simplefilter("ignore", DeprecationWarning)
            pid = os.fork()
        if pid == 0:
            signal.alarm(30)
            try:
                work()
                os._exit(0)
            except BaseException:
                traceback.print_exc()
                os._exit(1)
        return os.waitpid(pid, 0)[1]

    def killed_in(self, work):
        status = self.in_bot_process(work)
        self.assertTrue(os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL, f"not killed: {status}")


class ResumeTest(RestartTestCase):
    def test_finished_turn_is_not_resumed(self):
        self.request()
        self.start_bot()
        self.assertEqual(len(self.runs()), 1)
        self.assertEqual(self.posts(), [REPLY])

    def test_killed_turn_continues_once_in_the_same_session(self):
        self.during_claude = init_and_die
        self.killed_in(self.request)
        self.during_claude = init
        self.start_bot()
        first, second = self.runs()
        self.assertIsNone(first["session"])
        self.assertEqual(second["session"], SESSION)
        self.assertTrue(second["prompt"].startswith("【再起動】"))
        self.assertIn("途中で止まったコマンドの結果は分からないものとして", second["prompt"])
        self.assertTrue(second["prompt"].endswith("【元の依頼】\n元の依頼"))
        self.assertEqual(self.posts(), [RESTART_NOTICE, REPLY])
        # 次の起動では、もう動かない
        self.start_bot()
        self.assertEqual(len(self.runs()), 2)
        self.assertEqual(self.posts(), [RESTART_NOTICE, REPLY])

    def test_restarting_again_and_again_runs_the_continuation_only_once(self):
        self.during_claude = init_and_die
        self.killed_in(self.request)
        for _ in range(4):
            self.in_bot_process(self.start_bot)
        self.assertEqual(len(self.runs()), 2)
        self.assertEqual(self.posts(), [RESTART_NOTICE, RESTART_GIVE_UP])
        self.assertEqual(threadstate.take_unfinished_turns(), {})

    def test_notice_comes_before_the_continuation_and_the_record_is_gone_by_then(self):
        def record_is_gone(on_event):
            # 続きを動かしているあいだの記録は、続きの実行のものだけ
            self.assertEqual(self.posts(), [RESTART_NOTICE])
            turns = threadstate.get(KEY)["turns"]
            self.assertEqual(list(turns), ["200.1"])
            self.assertTrue(turns["200.1"]["resumed"])

        self.during_claude = init_and_die
        self.killed_in(self.request)
        self.during_claude = record_is_gone
        self.start_bot()
        self.assertEqual(self.posts(), [RESTART_NOTICE, REPLY])

    def test_giving_up_takes_the_running_mark_off_the_steps(self):
        tasks = {"1": {"subject": "調べる", "status": "in_progress"}}
        threadstate.update(KEY, plan={"ts": "150.1", "text": "", "session": SESSION, "tasks": tasks})
        self.during_claude = init_and_die
        self.killed_in(self.request)
        self.killed_in(self.start_bot)
        self.during_claude = init
        self.start_bot()
        self.assertEqual(self.posts(), [RESTART_NOTICE, RESTART_GIVE_UP])
        text = threadstate.get(KEY)["plan"]["text"]
        self.assertIn("⬜ 調べる", text)
        self.assertIn(WAIT_REPLY, text)

    def test_killed_before_the_session_is_known(self):
        self.during_claude = die
        self.killed_in(self.request)
        self.during_claude = init
        self.start_bot()
        second = self.runs()[1]
        self.assertIsNone(second["session"])
        self.assertTrue(second["prompt"].endswith("【元の依頼】\n元の依頼"))

    def test_killed_before_claude_started_resumes_the_session_of_the_thread(self):
        sessions.save(KEY, "older-session")
        self.during_claude = die
        self.killed_in(self.request)
        self.during_claude = init
        self.start_bot()
        self.assertEqual(self.runs()[1]["session"], "older-session")

    def test_model_and_effort_are_kept(self):
        self.during_claude = init_and_die
        self.killed_in(lambda: self.request(model="opus", effort="max"))
        self.during_claude = init
        self.start_bot()
        second = self.runs()[1]
        self.assertEqual((second["model"], second["effort"]), ("opus", "max"))

    def test_request_waiting_for_its_turn_is_not_lost(self):
        def work():
            # 動いているターンがスレッドのロックを持っているあいだに、次の依頼が来る
            threading.Thread(target=self.request, daemon=True).start()
            while not self.runs():
                time.sleep(0.01)
            threading.Thread(target=self.request, args=("次の依頼", "200.2"), daemon=True).start()
            while len(threadstate.get(KEY).get("turns", {})) < 2:
                time.sleep(0.01)
            die()

        self.during_claude = lambda on_event: threading.Event().wait()
        self.killed_in(work)
        self.during_claude = init
        self.start_bot()
        _, running, waiting = self.runs()
        self.assertTrue(running["prompt"].endswith("【元の依頼】\n元の依頼"))
        self.assertTrue(waiting["prompt"].endswith("【元の依頼】\n次の依頼"))
        self.assertEqual(self.posts(), [RESTART_NOTICE, REPLY, REPLY])


class NotResumedTest(RestartTestCase):
    def assert_nothing_resumed(self):
        self.assertEqual(threadstate.get(KEY).get("turns", {}), {})
        before = len(self.runs())
        self.start_bot()
        self.assertEqual(len(self.runs()), before)
        self.assertNotIn(RESTART_NOTICE, self.posts())

    def test_feedback_turn(self):
        self.during_claude = init_and_die
        with mock.patch.object(feedback, "profile", lambda profile: profile), mock.patch.object(feedback, "session", lambda: None):
            self.killed_in(lambda: self.request(feedback_mode=True))
        self.assert_nothing_resumed()

    def test_turn_that_failed_with_an_error(self):
        def fail(on_event):
            raise RuntimeError("だめだった")

        self.during_claude = fail
        self.request()
        self.assertIn("実行エラー", self.posts()[0])
        self.assert_nothing_resumed()

    def test_turn_whose_reply_could_not_be_posted(self):
        with mock.patch.object(self.bot, "post_reply", side_effect=RuntimeError("Slack")):
            self.request()
        self.assert_nothing_resumed()

    def test_killed_after_the_reply_was_posted(self):
        # 返信のあとの作業場の push で止まっても、返信は届いているので続けない
        self.profile = replace(self.profile, git_sync=True)
        with mock.patch.object(workspace, "_pull"), mock.patch.object(workspace, "_push", side_effect=lambda *args: die()):
            self.killed_in(self.request)
        self.assertEqual(self.posts()[0], REPLY)
        self.assert_nothing_resumed()


class StartupTest(RestartTestCase):
    def test_cleans_up_before_anything_can_start_a_new_turn(self):
        order = []
        take = threadstate.take_unfinished_turns
        self.mocks["stop_leftovers"].side_effect = lambda: order.append("stop")
        self.mocks["socket"].side_effect = lambda *args: order.append("socket") or mock.Mock()

        def take_and_note():
            order.append("take")
            return take()

        with mock.patch.object(threadstate, "take_unfinished_turns", take_and_note):
            self.start_bot()
        self.assertEqual(order, ["stop", "take", "socket"])


class StopLeftoversTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.proc = Path(folder.name)
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        for target, attr, value in ((claude, "PROC_DIR", self.proc), (claude, "CLAUDE_BIN", "/usr/local/bin/claude")):
            patcher = mock.patch.object(target, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(claude.os, "kill")
        self.kill = patcher.start()
        self.addCleanup(patcher.stop)

    def process(self, pid, comm, environ=None):
        folder = self.proc / str(pid)
        folder.mkdir()
        (folder / "comm").write_text(comm + "\n")
        # environ がないのは、ほかのユーザーのプロセス（読めない）の代わり
        if environ is not None:
            (folder / "environ").write_bytes(environ)

    def test_stops_only_the_claude_the_bot_started(self):
        self.process(100, "claude", b"HOME=/home/a\0AGENT_THREAD=C1:100.1\0")
        self.process(101, "claude", b"HOME=/home/a\0")
        self.process(102, "python3", b"AGENT_THREAD=C1:100.1\0")
        self.process(103, "claude")
        # SIGTERM で止まる
        self.kill.side_effect = lambda pid, sig: (self.proc / str(pid) / "comm").unlink()
        claude.stop_leftovers()
        self.kill.assert_called_once_with(100, signal.SIGTERM)

    def test_kills_what_does_not_stop_in_time(self):
        self.process(100, "claude", b"AGENT_THREAD=C1:100.1\0")
        with mock.patch.object(claude, "STOP_WAIT_SEC", 0):
            claude.stop_leftovers()
        self.assertEqual(self.kill.call_args_list, [mock.call(100, signal.SIGTERM), mock.call(100, signal.SIGKILL)])

    def test_does_nothing_when_nothing_is_left(self):
        self.process(101, "claude", b"HOME=/home/a\0")
        claude.stop_leftovers()
        self.kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
