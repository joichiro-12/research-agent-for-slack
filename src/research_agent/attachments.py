"""Slack の添付ファイルを runtime/attachments/<チャンネル>/<投稿の ts>/ に保存し、Claude に渡す一覧を作る。

依頼のメッセージの添付に加えて、同じスレッドの前の投稿のうち、Claude に届いていないものも渡す（earlier）。
bot を呼ぶ前に書いた文や添付、bot が止まっていたあいだの添付は、そのままでは Claude に届かないため。
添付を渡したかどうかは、保存先にファイルがあるかで見る（保存するのは Claude に渡すときだけ）。
Read ツールは Office の形式を開けないので、PowerPoint・Word・Excel は LibreOffice で PDF にして渡す。
"""

import logging
import re
import shutil
import subprocess
import tempfile
import urllib.parse
import urllib.request
from decimal import Decimal
from pathlib import Path

from .config import ALLOWED_USER_IDS, ATTACHMENTS_DIR, MAX_ATTACHMENT_BYTES, SLACK_BOT_TOKEN

log = logging.getLogger(__name__)

OFFICE_SUFFIXES = {".ppt", ".pptx", ".doc", ".docx", ".xls", ".xlsx", ".odp", ".odt", ".ods"}
CONVERT_TIMEOUT = 180
# 前の投稿の本文は 1 件 EARLIER_TEXT_LEN 字まで、合わせて EARLIER_TOTAL_LEN 字まで渡す（超えたら古い投稿から省く）
EARLIER_TEXT_LEN = 2000
EARLIER_TOTAL_LEN = 8000
EARLIER_HEADING = "このスレッドで利用者が前に投稿していて、まだ渡していなかったもの（bot を呼ぶ前の投稿など。古い順。添付は Read ツールで開ける）:"


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


def saved_path(channel, ts, f):
    """投稿 ts の添付 f の保存先（PDF にしたときも、元の形式のファイルはここに残る）。"""
    name = re.sub(r"[^\w.\-]", "_", f.get("name") or "file")
    return ATTACHMENTS_DIR / channel / ts / f"{f['id']}_{name}"


def saved_entry(path, mime):
    """保存済みの添付を、渡すときの (保存先, MIME タイプ) にする。PDF にしてあれば PDF を渡す。"""
    pdf = path.with_suffix(".pdf")
    if path.suffix.lower() in OFFICE_SUFFIXES and pdf.exists():
        return pdf, "application/pdf"
    return path, mime


def fetch(channel, ts, files):
    """投稿 ts の添付ファイルを保存し、[(保存先, MIME タイプ)] を返す。"""
    saved = []
    for f in files:
        url = f.get("url_private_download") or f.get("url_private")
        if not url or not f.get("id"):
            continue
        # Google ドライブなど外部のファイルは、URL が Slack の外を指す。そこへ bot のトークンを送らない
        if not (urllib.parse.urlparse(url).hostname or "").endswith(".slack.com"):
            log.warning("skip %s, which is not stored in Slack", f.get("name"))
            continue
        if (f.get("size") or 0) > MAX_ATTACHMENT_BYTES:
            log.warning("skip large attachment %s (%s bytes)", f.get("name"), f.get("size"))
            continue
        path = saved_path(channel, ts, f)
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


def download(event):
    """依頼のメッセージの添付ファイルを保存し、[(保存先, MIME タイプ)] を返す。"""
    return fetch(event["channel"], event["ts"], event.get("files") or [])


def earlier(client, channel, thread_ts, before_ts, new_session=False):
    """スレッドの before_ts より前の、許可した利用者の投稿のうち、Claude に届いていないものを返す。

    new_session なら、Claude はこのスレッドをまだ何も見ていないので、本文も添付も全部渡す（bot を呼ぶ前に書いた文など）。
    そうでなければ、まだ保存していない添付と、その投稿の本文だけを渡す。ほかの投稿は、それぞれの依頼で渡している。
    添付は、まだ保存していないものだけ取ってくる。ほかの人の投稿は渡さない。bot が添付したものは、作業場の outbox に残っている。
    [(投稿の本文, [(保存先, MIME タイプ)])] を古い順に返す。スレッドを読めなければ空にして、依頼はそのまま続ける。
    """
    try:
        # 返事が多いスレッドは何ページかに分かれる。SlackResponse を回すと、続きのページも取ってくる
        messages = [m for page in client.conversations_replies(channel=channel, ts=thread_ts, limit=200) for m in page["messages"]]
    except Exception as e:
        log.warning("could not read %s:%s for earlier posts: %s", channel, thread_ts, e)
        return []
    found = []
    for m in messages:
        if m.get("bot_id") or m.get("user") not in ALLOWED_USER_IDS or Decimal(m["ts"]) >= Decimal(before_ts):
            continue
        saved = []
        for f in m.get("files") or []:
            if not f.get("id"):
                continue
            if not (path := saved_path(channel, m["ts"], f)).exists():
                saved += fetch(channel, m["ts"], [f])
            elif new_session:
                # 前に保存したもの（Claude に届く前に失敗したターンの添付など）も、このセッションには届いていない
                saved.append(saved_entry(path, f.get("mimetype", "")))
        text = m.get("text", "")
        if saved or (new_session and text.strip()):
            found.append((text, saved))
    if found:
        log.info("passing %d earlier posts in %s:%s", len(found), channel, thread_ts)
    return found


def describe(saved):
    """プロンプトの末尾に付ける添付ファイルの一覧。"""
    lines = [f"- {path}（{mime}）" for path, mime in saved]
    return "添付ファイル（Read ツールで開ける）:\n" + "\n".join(lines)


def describe_earlier(found):
    """プロンプトの末尾に付ける、スレッドの前の投稿の一覧（earlier の結果）。

    本文が長すぎるときは、古い投稿の本文から省く。添付は、渡したものとして扱うので省かない。
    """
    texts, budget = [], EARLIER_TOTAL_LEN
    for text, _ in reversed(found):
        text = re.sub(r"<@[A-Z0-9]+>", "", text).strip()
        if len(text) > EARLIER_TEXT_LEN:
            text = text[:EARLIER_TEXT_LEN] + "…"
        if text and len(text) > budget:
            text, budget = "（長いので本文は省略）", 0
        else:
            budget -= len(text)
        texts.append(text)
    lines = [EARLIER_HEADING]
    for text, (_, saved) in zip(reversed(texts), found):
        first, *rest = (text or "（本文なし）").splitlines()
        lines.append(f"- {first}")
        lines += [f"  {line}" for line in rest]
        lines += [f"  - 添付: {path}（{mime}）" for path, mime in saved]
    return "\n".join(lines)
