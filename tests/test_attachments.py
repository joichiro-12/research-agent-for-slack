"""添付の渡し方（Office の添付を PDF にすること、スレッドの前の投稿も渡すこと）を確かめる。

実行: uv run python -m unittest discover -s tests
"""

import os
import shutil
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

# config.py は Slack のトークンを環境変数から読むので、試すときは仮の値を入れておく
for name in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "ALLOWED_USER_IDS"):
    os.environ.setdefault(name, "test")

from research_agent import attachments, feedback  # noqa: E402
from test_restart import RestartTestCase, init, init_and_die  # noqa: E402


def fake_urlopen():
    response = mock.MagicMock()
    response.__enter__.return_value = response
    response.headers.get_content_type.return_value = "text/plain"
    response.read.return_value = b"data"
    return mock.Mock(return_value=response)


class ConvertTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.source = self.dir / "F1_資料.pptx"
        self.source.write_bytes(b"not really a deck")

    def run_download(self, to_pdf):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.headers.get_content_type.return_value = "application/vnd.ms-powerpoint"
        response.read.return_value = b"deck"
        event = {"channel": "C1", "ts": "1.2", "files": [{"id": "F1", "name": "資料.pptx", "size": 4, "url_private": "https://files.slack.com/x", "mimetype": "application/pptx"}]}
        with mock.patch.object(attachments, "ATTACHMENTS_DIR", self.dir), mock.patch.object(attachments, "to_pdf", to_pdf), mock.patch("urllib.request.urlopen", return_value=response):
            return attachments.download(event)

    def test_office_files_are_passed_as_pdf(self):
        pdf = self.dir / "x.pdf"
        saved = self.run_download(lambda path: pdf)
        self.assertEqual(saved, [(pdf, "application/pdf")])

    def test_original_is_kept_when_conversion_fails(self):
        saved = self.run_download(lambda path: None)
        self.assertEqual(saved[0][1], "application/pptx")
        self.assertEqual(saved[0][0].suffix, ".pptx")

    def test_missing_soffice_returns_none(self):
        with mock.patch.object(shutil, "which", return_value=None):
            self.assertIsNone(attachments.to_pdf(self.source))


def message(ts, user="U1", text="", files=(), **fields):
    return {"ts": ts, "user": user, "text": text, "files": [file(name) for name in files], **fields}


def file(name, url="https://files.slack.com/files-pri/T1-F1/download/x"):
    return {"id": f"F_{name}", "name": name, "size": 4, "url_private_download": url, "mimetype": "text/plain"}


class EarlierTest(unittest.TestCase):
    """スレッドの前の投稿（attachments.earlier）を確かめる。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.client = mock.MagicMock()
        self.urlopen = fake_urlopen()
        for target, attr, value in (
            (attachments, "ATTACHMENTS_DIR", self.dir),
            (attachments, "ALLOWED_USER_IDS", {"U1"}),
            (attachments, "to_pdf", lambda path: None),
            (urllib.request, "urlopen", self.urlopen),
        ):
            patcher = mock.patch.object(target, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def thread(self, *messages):
        """conversations_replies を回すと出てくるページ。"""
        self.client.conversations_replies.return_value = [{"messages": list(messages)}]

    def earlier(self, before_ts="200.1", new_session=False):
        return attachments.earlier(self.client, "C1", "100.1", before_ts, new_session)

    def names(self, found):
        return [[path.name for path, _ in saved] for _, saved in found]

    def test_files_posted_before_the_request_are_passed_once(self):
        self.thread(
            message("100.1", text="図の見直し", files=["root.md"]),
            message("101.1", files=["draft.md", "figures.md"]),
            message("150.1", text="<@UBOT> 見てほしい"),
            message("200.1", files=["now.md"]),
            message("201.1", files=["later.md"]),
        )
        found = self.earlier()
        self.assertEqual(self.names(found), [["F_root.md_root.md"], ["F_draft.md_draft.md", "F_figures.md_figures.md"]])
        self.assertEqual(found[0][0], "図の見直し")
        self.assertTrue(all(path.read_bytes() == b"data" for _, saved in found for path, _ in saved))
        # 一度渡したものは、次のターンでは渡さない
        self.assertEqual(self.earlier(), [])

    def test_a_new_session_gets_every_earlier_post(self):
        deck = {**file("deck.pptx"), "mimetype": "application/pptx"}
        # Claude に届く前に失敗したターンなどで、セッションができる前に保存した添付（PDF にしたもの）
        saved = attachments.saved_path("C1", "102.1", deck)
        saved.parent.mkdir(parents=True)
        saved.write_bytes(b"deck")
        saved.with_suffix(".pdf").write_bytes(b"pdf")
        self.thread(
            message("100.1", text="図の見直し"),
            message("101.1", text="締め切りは来週\n図は 3 枚"),
            {**message("102.1"), "files": [deck]},
            message("103.1", user="U9", text="ほかの人の投稿"),
            message("104.1", user=None, bot_id="B1", text="bot の投稿"),
            message("200.1", text="<@UBOT> 見てほしい"),
            message("201.1", text="あとの投稿"),
        )
        found = self.earlier(new_session=True)
        self.assertEqual([text for text, _ in found], ["図の見直し", "締め切りは来週\n図は 3 枚", ""])
        self.assertEqual(found[2][1], [(saved.with_suffix(".pdf"), "application/pdf")])
        self.urlopen.assert_not_called()
        # セッションができたあとは、文だけの投稿は渡さない（それぞれの依頼で渡している）
        self.assertEqual(self.earlier(), [])

    def test_files_saved_with_their_own_request_are_not_passed_again(self):
        self.thread(message("101.1", files=["a.md"]), message("102.1", files=["b.md"]))
        # 101.1 の添付は、その投稿の依頼で保存済み
        attachments.download({"channel": "C1", "ts": "101.1", "files": [file("a.md")]})
        self.assertEqual(self.names(self.earlier()), [["F_b.md_b.md"]])

    def test_files_of_others_and_of_the_bot_are_not_passed(self):
        self.thread(
            message("101.1", user="U9", files=["other.md"]),
            message("102.1", user=None, bot_id="B1", files=["bot.html"]),
            message("103.1", user="U1", bot_id="B1", files=["app.html"]),
        )
        self.assertEqual(self.earlier(), [])
        self.urlopen.assert_not_called()

    def test_the_token_is_not_sent_outside_slack(self):
        outside = file("drive.md", url="https://docs.google.com/document/d/x")
        self.thread({**message("101.1"), "files": [outside]})
        self.assertEqual(self.earlier(), [])
        self.urlopen.assert_not_called()

    def test_an_unreadable_thread_gives_nothing(self):
        self.client.conversations_replies.side_effect = Exception("ratelimited")
        self.assertEqual(self.earlier(), [])

    def test_description_shows_each_post_and_its_files(self):
        path = self.dir / "F_a.md_a.md"
        long_text = "あ" * (attachments.EARLIER_TEXT_LEN + 10)
        found = [("<@UBOT> 下書きと図です\n- 3 枚", [(path, "text/plain")]), ("", [(path, "text/plain")]), (long_text, [])]
        self.assertEqual(
            attachments.describe_earlier(found).splitlines(),
            [
                attachments.EARLIER_HEADING,
                "- 下書きと図です",
                "  - 3 枚",
                f"  - 添付: {path}（text/plain）",
                "- （本文なし）",
                f"  - 添付: {path}（text/plain）",
                "- " + "あ" * attachments.EARLIER_TEXT_LEN + "…",
            ],
        )

    def test_long_posts_drop_the_oldest_text_but_keep_the_files(self):
        path = self.dir / "F_a.md_a.md"
        posts = [("古い", [(path, "text/plain")]), *[("い" * attachments.EARLIER_TEXT_LEN, [])] * 4, ("新しい", [])]
        lines = attachments.describe_earlier(posts).splitlines()
        self.assertEqual(lines[1:3], ["- （長いので本文は省略）", f"  - 添付: {path}（text/plain）"])
        self.assertEqual(lines[-1], "- 新しい")
        self.assertEqual(sum(line.startswith("- い") for line in lines), 3)


class TurnTest(RestartTestCase):
    """依頼（bot.handle_request）のプロンプトに、スレッドの前の投稿が付くことを確かめる。"""

    def setUp(self):
        super().setUp()
        for target, attr, value in (
            (attachments, "ATTACHMENTS_DIR", self.folder / "attachments"),
            (attachments, "ALLOWED_USER_IDS", {"U1"}),
            (urllib.request, "urlopen", fake_urlopen()),
        ):
            patcher = mock.patch.object(target, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client.conversations_replies.return_value = [{"messages": [
            message("100.1", text="図の見直し"),
            message("101.1", files=["draft.md"]),
        ]}]

    def test_the_first_request_gets_the_earlier_posts_and_the_next_does_not(self):
        self.request()
        self.request(ts="210.1")
        first, second = self.runs()
        self.assertIn(attachments.EARLIER_HEADING, first["prompt"])
        self.assertIn("- 図の見直し\n", first["prompt"])
        self.assertIn("F_draft.md_draft.md", first["prompt"])
        self.assertNotIn(attachments.EARLIER_HEADING, second["prompt"])

    def test_a_new_session_after_a_failed_first_turn_gets_them_again(self):
        # claude が動かずに失敗したら（実行エラー）、セッションはできていない
        def fail(on_event):
            raise RuntimeError("claude failed to start")

        self.during_claude = fail
        self.request()
        self.during_claude = init
        self.request(ts="210.1")
        self.assertIn("F_draft.md_draft.md", self.runs()[1]["prompt"])
        self.assertIn("- 図の見直し\n", self.runs()[1]["prompt"])

    def test_a_turn_stopped_by_a_restart_still_has_them(self):
        # 依頼の記録に残るので、Claude に届く前に止まっても、続きのターンで渡る。重ねては渡さない
        self.during_claude = init_and_die
        self.killed_in(self.request)
        self.during_claude = init
        self.start_bot()
        prompt = self.runs()[1]["prompt"]
        self.assertEqual(prompt.count("F_draft.md_draft.md"), 1)
        self.assertEqual(prompt.count("- 図の見直し\n"), 1)

    def test_feedback_leaves_them_for_the_next_turn(self):
        for target, attr, value in (
            (feedback, "profile", lambda base: base),
            (feedback, "session", lambda: None),
            (feedback, "set_session", lambda session_id: None),
            (self.bot, "post_proposal", mock.Mock()),
        ):
            patcher = mock.patch.object(target, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.request("【フィードバック】直して", feedback_mode=True)
        self.request(ts="210.1")
        fb, turn = self.runs()
        self.assertNotIn(attachments.EARLIER_HEADING, fb["prompt"])
        self.assertIn("F_draft.md_draft.md", turn["prompt"])
        self.assertIn("- 図の見直し\n", turn["prompt"])

    def test_top_level_requests_do_not_read_the_thread(self):
        self.bot.handle_request(self.client, {"channel": "C1", "ts": "100.1", "user": "U1"}, "依頼", "opus")
        self.client.conversations_replies.assert_not_called()


if __name__ == "__main__":
    unittest.main()
