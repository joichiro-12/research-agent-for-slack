"""段取りのメッセージ（plan.py）。ターンをまたいで更新し、いま待っていることを添える。

実行: uv run python -m unittest discover -s tests
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# config.py は Slack のトークンを環境変数から読むので、試すときは仮の値を入れておく
for name in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "ALLOWED_USER_IDS"):
    os.environ.setdefault(name, "test")

from research_agent import threadstate  # noqa: E402
from research_agent.plan import PlanReporter  # noqa: E402

KEY = "C1:100.1"
SESSION = "session-1"


class FakeSlack:
    """スレッドのメッセージを投稿順に持つ。"""

    def __init__(self):
        self.messages = {}
        self.calls = []
        self._next = 200

    def chat_postMessage(self, channel, thread_ts, text):
        self._next += 1
        ts = str(self._next)
        self.messages[ts] = text
        self.calls.append("post")
        return {"ts": ts}

    def chat_update(self, channel, ts, text):
        if ts not in self.messages:
            raise RuntimeError("message_not_found")
        self.messages[ts] = text
        self.calls.append("update")

    def chat_delete(self, channel, ts):
        del self.messages[ts]
        self.calls.append("delete")

    @property
    def last(self):
        return list(self.messages.values())[-1]


def created(task_id, subject):
    call = f"c{task_id}"
    return [
        {"type": "assistant", "session_id": SESSION, "message": {"content": [{"type": "tool_use", "id": call, "name": "TaskCreate", "input": {"subject": subject}}]}},
        {"type": "user", "session_id": SESSION, "message": {"content": [{"type": "tool_result", "tool_use_id": call, "content": f"Task #{task_id} created successfully: {subject}"}]}},
    ]


def updated(task_id, status, session=SESSION):
    call = f"u{task_id}{status}"
    return [
        {"type": "assistant", "session_id": session, "message": {"content": [{"type": "tool_use", "id": call, "name": "TaskUpdate", "input": {"taskId": task_id, "status": status}}]}},
        {"type": "user", "session_id": session, "message": {"content": [{"type": "tool_result", "tool_use_id": call, "content": f"Updated task #{task_id} status"}]}},
    ]


class PlanTestCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        patcher = mock.patch.object(threadstate, "THREADS_FILE", Path(folder.name) / "threads.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.slack = FakeSlack()

    def turn(self, *events, approval=False):
        """1 回のターンを流す。"""
        reporter = PlanReporter(self.slack, "C1", "100.1", KEY)
        reporter.start()
        for event in events:
            reporter.on_event(event)
        reporter.finish(approval)
        return reporter


class AcrossTurnsTest(PlanTestCase):
    def test_a_later_turn_updates_the_same_plan(self):
        self.turn(*created("31", "設定を作る"), *created("32", "実験を動かす"), *created("33", "結果を確かめる"), *updated("31", "completed"))
        self.turn(*updated("32", "completed"))
        self.assertEqual(len(self.slack.messages), 1)
        self.assertEqual(
            self.slack.last,
            "*Steps*  ▰▰▱\n✅ 設定を作る\n✅ 実験を動かす\n⬜ 結果を確かめる\n\nいまは返事を待ってるよ",
        )

    def test_a_change_moves_the_plan_to_the_end_of_the_thread(self):
        self.turn(*created("1", "調べる"), *created("2", "まとめる"))
        first = list(self.slack.messages)[0]
        self.slack.calls.clear()
        self.turn(*updated("1", "in_progress"))
        self.assertNotIn(first, self.slack.messages)
        # 表示を外す書き換え → 消す → 出し直す → 待っていることを付ける書き換え
        self.assertEqual(self.slack.calls, ["update", "delete", "post", "update"])

    def test_the_plan_moves_only_once_in_a_turn(self):
        self.turn(*created("1", "調べる"), *created("2", "まとめる"))
        self.slack.calls.clear()
        self.turn(*updated("1", "in_progress"), *updated("1", "completed"), *updated("2", "in_progress"))
        self.assertEqual(self.slack.calls.count("post"), 1)
        self.assertEqual(self.slack.calls.count("delete"), 1)

    def test_a_turn_without_changes_leaves_the_plan_in_place(self):
        self.turn(*created("1", "調べる"), *created("2", "まとめる"))
        before = dict(self.slack.messages)
        self.slack.calls.clear()
        self.turn()
        self.assertEqual(self.slack.messages, before)
        self.assertNotIn("post", self.slack.calls)
        self.assertNotIn("delete", self.slack.calls)

    def test_a_new_wait_moves_the_plan_even_without_step_changes(self):
        self.turn(*created("1", "調べる"), *created("2", "まとめる"))
        self.slack.calls.clear()
        self.turn(approval=True)
        self.assertIn("post", self.slack.calls)
        self.assertTrue(self.slack.last.endswith("いまは承認を待ってるよ"))

    def test_a_refresh_before_the_turn_does_not_keep_the_plan_in_its_old_place(self):
        # ターンの外で表示が書き換わり、次のターンでは手順が変わらなかったとき
        self.turn(*created("33", "結果を確かめる"), *updated("33", "in_progress"), approval=True)
        first = list(self.slack.messages)[0]
        PlanReporter(self.slack, "C1", "100.1", KEY).refresh(False)
        self.assertIn(first, self.slack.messages)
        self.turn()
        self.assertNotIn(first, self.slack.messages)
        self.assertEqual(len(self.slack.messages), 1)
        self.assertTrue(self.slack.last.endswith("いまは返事を待ってるよ"))

    def test_the_next_turn_without_changes_stays_after_a_move(self):
        self.turn(*created("1", "調べる"), approval=True)
        PlanReporter(self.slack, "C1", "100.1", KEY).refresh(False)
        self.turn()
        self.slack.calls.clear()
        self.turn()
        self.assertNotIn("post", self.slack.calls)
        self.assertNotIn("delete", self.slack.calls)

    def test_new_steps_join_an_unfinished_plan(self):
        self.turn(*created("1", "調べる"), *created("2", "まとめる"), *updated("1", "completed"))
        self.turn(*created("3", "測り直す"))
        self.assertEqual(len(self.slack.messages), 1)
        self.assertIn("⬜ 測り直す", self.slack.last)
        self.assertIn("✅ 調べる", self.slack.last)

    def test_a_finished_plan_stays_and_the_next_plan_starts_anew(self):
        self.turn(*created("1", "調べる"), *updated("1", "completed"))
        old = self.slack.last
        self.turn(*created("2", "次を調べる"))
        self.assertEqual(len(self.slack.messages), 2)
        self.assertEqual(list(self.slack.messages.values())[0], old)
        self.assertNotIn("✅ 調べる", self.slack.last)

    def test_a_new_session_starts_a_new_plan(self):
        # セッションが変わると、手順の ID は振り直される
        self.turn(*created("1", "調べる"), *created("2", "まとめる"))
        self.turn(*updated("1", "completed", session="session-2"))
        self.assertIn("⬜ 調べる", self.slack.last)
        self.assertEqual(threadstate.get(KEY)["plan"]["tasks"], {})
        reporter = self.turn(
            {"type": "assistant", "session_id": "session-2", "message": {"content": [{"type": "tool_use", "id": "x", "name": "TaskCreate", "input": {"subject": "新しい手順"}}]}},
            {"type": "user", "session_id": "session-2", "message": {"content": [{"type": "tool_result", "tool_use_id": "x", "content": "Task #1 created successfully: 新しい手順"}]}},
        )
        self.assertEqual(list(reporter.tasks), ["1"])
        self.assertEqual(reporter.tasks["1"]["subject"], "新しい手順")

    def test_steps_from_before_the_plan_are_ignored(self):
        self.turn(*created("10", "調べる"))
        reporter = self.turn(*updated("5", "deleted"))
        self.assertEqual(list(reporter.tasks), ["10"])

    def test_deleting_every_step_removes_the_message(self):
        self.turn(*created("1", "調べる"))
        self.turn(*updated("1", "deleted"))
        self.assertEqual(self.slack.messages, {})
        self.assertIsNone(threadstate.get(KEY)["plan"]["ts"])

    def test_a_message_deleted_by_hand_is_posted_again(self):
        self.turn(*created("1", "調べる"), *created("2", "まとめる"))
        self.slack.messages.clear()
        # 1 回目の書き換えは失敗し、次の変更で出し直す
        with self.assertLogs("research_agent.plan", "ERROR"):
            self.turn()
        self.turn(*updated("1", "completed"))
        self.assertEqual(len(self.slack.messages), 1)
        self.assertIn("✅ 調べる", self.slack.last)


class WaitingTest(PlanTestCase):
    def test_approval_marks_the_running_step(self):
        self.turn(*created("1", "実験を動かす"), *updated("1", "in_progress"), approval=True)
        self.assertEqual(self.slack.last, "*Steps*  ▱\n🔐 実験を動かす\n\nいまは承認を待ってるよ")

    def test_the_mark_turns_back_while_the_turn_runs(self):
        self.turn(*created("1", "実験を動かす"), *updated("1", "in_progress"), approval=True)
        reporter = PlanReporter(self.slack, "C1", "100.1", KEY)
        reporter.start()
        self.assertEqual(self.slack.last, "*Steps*  ▱\n⏳ 実験を動かす")

    def test_nothing_running_means_waiting_for_a_reply(self):
        self.turn(*created("1", "調べる"), *updated("1", "in_progress"))
        self.assertEqual(self.slack.last, "*Steps*  ▱\n⬜ 調べる\n\nいまは返事を待ってるよ")

    def test_finished_steps_wait_for_nothing(self):
        self.turn(*created("1", "調べる"), *updated("1", "completed"))
        self.assertEqual(self.slack.last, "*Steps*  ▰\n✅ 調べる")

    def test_no_plan_means_no_message(self):
        self.turn(approval=True)
        self.assertEqual(self.slack.messages, {})


class RefreshTest(PlanTestCase):
    def test_refresh_rewrites_in_place_only_when_the_text_changes(self):
        self.turn(*created("1", "調べる"), approval=True)
        self.slack.calls.clear()
        PlanReporter(self.slack, "C1", "100.1", KEY).refresh(True)
        self.assertEqual(self.slack.calls, [])
        PlanReporter(self.slack, "C1", "100.1", KEY).refresh(False)
        self.assertEqual(self.slack.calls, ["update"])
        self.assertTrue(self.slack.last.endswith("いまは返事を待ってるよ"))

    def test_no_plan_refreshes_nothing(self):
        PlanReporter(self.slack, "C1", "100.1", KEY).refresh(True)
        self.assertEqual(self.slack.calls, [])


class RenderTest(PlanTestCase):
    def test_old_finished_steps_are_folded(self):
        events = []
        for i in range(1, 15):
            events += created(str(i), f"手順 {i}")
        for i in range(1, 7):
            events += updated(str(i), "completed")
        self.turn(*events)
        lines = self.slack.last.splitlines()
        self.assertEqual(lines[1], "✅ ほか 4 件")
        self.assertEqual(lines[2], "✅ 手順 5")
        self.assertNotIn("✅ 手順 4", lines)
        self.assertIn("⬜ 手順 14", lines)
        # バーは畳んだ手順も数える
        self.assertEqual(lines[0], "*Steps*  " + "▰" * 4 + "▱" * 6)

    def test_same_names_are_not_folded_together(self):
        events = []
        for i in range(1, 13):
            events += created(str(i), "同じ手順")
        for i in range(1, 12):
            events += updated(str(i), "completed")
        self.turn(*events)
        lines = self.slack.last.splitlines()
        self.assertEqual(lines[1], "✅ ほか 2 件")
        self.assertEqual(lines.count("✅ 同じ手順"), 9)


class WithoutStateTest(PlanTestCase):
    def test_feedback_does_not_touch_the_threads_plan(self):
        self.turn(*created("1", "スレッドの手順"))
        before = threadstate.get(KEY)["plan"]
        reporter = PlanReporter(self.slack, "C1", "100.1")
        reporter.start()
        for event in created("1", "修正案を作る"):
            reporter.on_event(event)
        reporter.finish()
        self.assertEqual(threadstate.get(KEY)["plan"], before)
        self.assertNotIn("待ってるよ", self.slack.last)


class ThreadStateTest(PlanTestCase):
    def test_a_broken_file_starts_over(self):
        threadstate.THREADS_FILE.write_text("{")
        with self.assertLogs("research_agent.threadstate", "WARNING"):
            self.assertEqual(threadstate.get(KEY), {})


if __name__ == "__main__":
    unittest.main()
