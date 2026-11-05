"""プロファイルの Edit の許可（スレッドの outbox、ガードのあるプロファイルの bot のリポジトリ）を確かめる。

実行: uv run python -m unittest discover -s tests
"""

import json
import os
import unittest

# config.py は Slack のトークンを環境変数から読むので、試すときは仮の値を入れておく
for name in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "ALLOWED_USER_IDS"):
    os.environ.setdefault(name, "test")

from research_agent import profiles, workspace  # noqa: E402
from research_agent.config import ROOT_DIR  # noqa: E402


class OutboxTest(unittest.TestCase):
    def test_every_channel_can_write_its_outbox(self):
        # Write を許していないプロファイルでも、スレッドの outbox にだけは書ける
        entries = [*profiles.entries(), ("対応づけのないチャンネル", {"profile": profiles.DEFAULT_PROFILE})]
        for channel, entry in entries:
            with self.subTest(channel=channel):
                profile = profiles.load(entry["profile"], entry)
                outbox = workspace.outbox_dir(profile.work_dir, "100.1")
                # 規則の // は絶対パスの印なので、outbox も絶対パスでないといけない
                self.assertTrue(outbox.is_absolute())
                rule = f"Edit(/{outbox}/**)"
                self.assertIn(rule, json.loads(profiles.allow_edit(profile, outbox).settings)["permissions"]["allow"])
                self.assertNotIn(rule, json.loads(profile.settings)["permissions"]["allow"])


class OwnRepositoryTest(unittest.TestCase):
    RULE = f"Edit(/{ROOT_DIR}/**)"

    def test_a_guarded_profile_cannot_edit_the_bot(self):
        # ガードの規則や承認の状態を、Claude に書き換えさせない
        profile = profiles.load("research", {"work_dir": "~/research-workspace"})
        self.assertTrue(profile.guard)
        self.assertIn(self.RULE, json.loads(profile.settings)["permissions"]["deny"])

    def test_the_default_profile_can_write_in_its_workspace(self):
        # 既定の作業場（runtime/workspace）はリポジトリの中にあり、ガードもない
        profile = profiles.load(profiles.DEFAULT_PROFILE)
        self.assertFalse(profile.guard)
        self.assertNotIn(self.RULE, json.loads(profile.settings)["permissions"]["deny"])


if __name__ == "__main__":
    unittest.main()
