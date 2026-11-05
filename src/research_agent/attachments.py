"""Slack の添付ファイルを runtime/attachments/ に保存し、Claude に渡す一覧を作る。

Read ツールは Office の形式を開けないので、PowerPoint・Word・Excel は LibreOffice で PDF にして渡す。
"""

import logging
import re
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path

from .config import ATTACHMENTS_DIR, MAX_ATTACHMENT_BYTES, SLACK_BOT_TOKEN

log = logging.getLogger(__name__)

OFFICE_SUFFIXES = {".ppt", ".pptx", ".doc", ".docx", ".xls", ".xlsx", ".odp", ".odt", ".ods"}
CONVERT_TIMEOUT = 180


def to_pdf(path):
    """Office の文書を、同じフォルダに PDF にして、そのパスを返す。できなければ None。"""
    if not (soffice := shutil.which("soffice")):
        log.warning("soffice not found; passing %s as is", path.name)
        return None
    # LibreOffice は設定フォルダを共有すると同時に動かない。ファイル名は引数に載せない（ps で他のユーザーに見える）
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / f"input{path.suffix.lower()}"
        shutil.copyfile(path, source)
        command = [soffice, "--headless", f"-env:UserInstallation=file://{tmp}/profile", "--convert-to", "pdf", "--outdir", tmp, str(source)]
        try:
            subprocess.run(command, capture_output=True, timeout=CONVERT_TIMEOUT, check=True)
        except (subprocess.SubprocessError, OSError) as e:
            log.warning("failed to convert %s: %s", path.name, e)
            return None
        converted = source.with_suffix(".pdf")
        if not converted.exists():
            log.warning("soffice wrote no PDF for %s", path.name)
            return None
        pdf = path.with_suffix(".pdf")
        shutil.move(converted, pdf)
    return pdf


def download(event):
    """メッセージの添付ファイルを保存し、[(保存先, MIME タイプ)] を返す。"""
    saved = []
    for f in event.get("files") or []:
        url = f.get("url_private_download") or f.get("url_private")
        if not url:
            continue
        if (f.get("size") or 0) > MAX_ATTACHMENT_BYTES:
            log.warning("skip large attachment %s (%s bytes)", f.get("name"), f.get("size"))
            continue
        name = re.sub(r"[^\w.\-]", "_", f.get("name") or "file")
        path = ATTACHMENTS_DIR / event["channel"] / event["ts"] / f"{f['id']}_{name}"
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                # files:read 権限がないと、ファイルの代わりにログイン画面の HTML が返る
                if response.headers.get_content_type() == "text/html" and f.get("mimetype") != "text/html":
                    log.warning("got HTML instead of %s; is files:read granted?", f.get("name"))
                    continue
                data = response.read()
        except Exception as e:
            log.warning("failed to download %s: %s", f.get("name"), e)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        if path.suffix.lower() in OFFICE_SUFFIXES and (pdf := to_pdf(path)):
            saved.append((pdf, "application/pdf"))
        else:
            saved.append((path, f.get("mimetype", "")))
    return saved


def describe(saved):
    """プロンプトの末尾に付ける添付ファイルの一覧。"""
    lines = [f"- {path}（{mime}）" for path, mime in saved]
    return "添付ファイル（Read ツールで開ける）:\n" + "\n".join(lines)
