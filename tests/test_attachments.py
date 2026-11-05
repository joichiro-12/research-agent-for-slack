"""Office の添付を PDF にして渡すことを確かめる。

実行: uv run python -m unittest discover -s tests
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# config.py は Slack のトークンを環境変数から読むので、試すときは仮の値を入れておく
for name in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "ALLOWED_USER_IDS"):
    os.environ.setdefault(name, "test")

from research_agent import attachments  # noqa: E402


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
        event = {"channel": "C1", "ts": "1.2", "files": [{"id": "F1", "name": "資料.pptx", "size": 4, "url_private": "https://x", "mimetype": "application/pptx"}]}
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


if __name__ == "__main__":
    unittest.main()
