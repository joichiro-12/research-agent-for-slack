"""Slack の入口。メンションとスレッド内の返信を受け、claude の結果を同じスレッドに返す。

先頭に fb を付けた依頼は、bot 自身の設定（profiles/）を直すフィードバックとして feedback.py で扱う。
ガードが止めた操作は、依頼のメッセージのボタンか、スレッドへの「承認」の返信で実行させる（approvals.py）。答えが出たら、依頼のメッセージは消す。
Claude がその操作をせずに済ませて取り下げた依頼は、スレッドに出さない（出していたら消す）。
スレッドの前の投稿で Claude に届いていないもの（bot を呼ぶ前に書いた文や、まだ渡していない添付）も、依頼に添えて渡す（attachments.py）。
Claude が作業場の outbox に置いたファイルは、返信のあとにスレッドへ添付する（workspace.py）。
1 つのスレッドでターンが続いても、段取りは引き継ぐ（plan.py）。
bot の再起動で止まったターンは、次の起動でスレッドに知らせて、同じセッションで 1 回だけ続けさせる（resume_thread）。
"""

import logging
import re
import subprocess
import threading
import time
import unicodedata
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import replace

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from . import approvals, attachments, feedback, profiles, sessions, threadstate, workspace
from .claude import run_claude, stop_leftovers
from .config import (
    ALLOWED_USER_IDS,
    APPROVE_ACTION,
    APPROVE_PATTERN,
    CANCEL_PATTERN,
    DEFAULT_MODEL,
    DIFF_MAX_LEN,
    EFFORT_PATTERN,
    FEEDBACK_PATTERN,
    LISTENER_WORKERS,
    MAX_ATTACHMENT_BYTES,
    MAX_UPLOADS,
    MODELS,
    OK_PATTERN,
    OPUS_PATTERN,
    OUTBOX_HINT,
    PLAN_ENV,
    PLAN_HINT,
    REJECT_ACTION,
    REJECT_PATTERN,
    RESTART_GIVE_UP,
    RESTART_NOTICE,
    RESTART_PROMPT,
    RUNTIME_DIR,
    SLACK_APP_TOKEN,
    SLACK_BOT_TOKEN,
    SLACK_CHUNK,
    THREAD_BRIEF,
    THREAD_HISTORY_MAX_LEN,
    THREAD_MESSAGE_SUBTYPES,
    TIMEOUT_CONTINUES,
    TIMEOUT_GIVE_UP,
    TIMEOUT_NOTICE,
    TIMEOUT_PROMPT,
    TIMEOUT_SEC,
    WORK_DIR,
)
from .plan import PlanReporter
from .status import StatusReporter

# Bolt は App を作った時点のログレベルを引き継ぐので、App より先に設定する
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger(__name__)

app = App(token=SLACK_BOT_TOKEN, listener_executor=ThreadPoolExecutor(max_workers=LISTENER_WORKERS))
thread_locks = defaultdict(threading.Lock)
seen_events = set()


def pick_model(prompt):
    """メッセージからモデルを選び、(モデル, キーワードを除いたプロンプト) を返す。"""
    if OPUS_PATTERN.search(prompt):
        return "opus", OPUS_PATTERN.sub("", prompt).strip()
    return DEFAULT_MODEL, prompt


def pick_effort(prompt):
    """メッセージからエフォートを選び、(エフォート, 指定を除いたプロンプト) を返す。指定がなければエフォートは None。"""
    if m := EFFORT_PATTERN.search(prompt):
        return m.group(1).lower(), EFFORT_PATTERN.sub("", prompt).strip()
    return None, prompt


def post_chunks(client, channel, thread_ts, text, last_blocks=()):
    """長い文は分けて投稿し、投稿したメッセージの ts をすべて返す。thread_ts が None ならチャンネルに投稿する。

    last_blocks は最後のメッセージに足すブロック（ボタンなど）。
    """
    posted = []
    chunks = [text[i : i + SLACK_CHUNK] for i in range(0, len(text), SLACK_CHUNK)] or [""]
    for index, chunk in enumerate(chunks):
        try:
            # markdown ブロックなら Claude の Markdown をそのまま表示できる
            res = client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=chunk,
                blocks=[{"type": "markdown", "text": chunk}, *(last_blocks if index == len(chunks) - 1 else ())],
            )
        except Exception:
            log.exception("markdown block failed, falling back to plain text")
            res = client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=chunk)
        posted.append(res["ts"])
    return posted


def post_reply(client, channel, thread_ts, text):
    """thread_ts が None ならチャンネルに投稿する。最後に投稿したメッセージの ts を返す。"""
    return post_chunks(client, channel, thread_ts, text)[-1]


def accept(event):
    """二重に届いたイベントと、許可していないユーザーのメッセージを除外する。"""
    # 再接続時などに同じイベントが二重に届くことがある
    event_key = f"{event['channel']}:{event['ts']}"
    if event_key in seen_events:
        return False
    seen_events.add(event_key)
    if event.get("user") not in ALLOWED_USER_IDS:
        log.info("ignored message from %s", event.get("user"))
        return False
    return True


def permalink(client, channel, ts):
    try:
        return client.chat_getPermalink(channel=channel, message_ts=ts)["permalink"]
    except Exception:
        log.warning("could not get permalink for %s:%s", channel, ts)
        return ""


def thread_history(client, channel, thread_ts, exclude_ts):
    """スレッドのやりとりを「利用者: / bot:」の行にして返す（フィードバックの文脈として Claude に渡す）。"""
    try:
        messages = client.conversations_replies(channel=channel, ts=thread_ts, limit=100)["messages"]
    except Exception:
        log.warning("could not read thread %s:%s", channel, thread_ts)
        return ""
    lines = [
        f"{'bot' if m.get('bot_id') else '利用者'}: {m.get('text', '')}"
        for m in messages
        if m.get("ts") != exclude_ts and m.get("text")
    ]
    return "\n".join(lines)[-THREAD_HISTORY_MAX_LEN:]


def report(client, text):
    """フィードバック反映の報告チャンネルに投稿する（channels.toml で設定していなければ何もしない）。"""
    try:
        if channel := profiles.report_channel(client):
            post_reply(client, channel, None, text)
    except Exception:
        log.exception("failed to post report")


def route(client, event, prompt, model, effort=None):
    """依頼を振り分ける：確認待ちのフィードバックへの返事 / 承認待ちの操作への返事 /
    fb 付きの新しいフィードバック / 通常の依頼。effort はメッセージで指定したエフォート（なければチャンネルの既定）。"""
    channel = event["channel"]
    thread_ts = event.get("thread_ts") or event["ts"]
    key = sessions.thread_key(channel, thread_ts)
    if (pending := feedback.pending()) and pending["thread"] == key:
        answer = unicodedata.normalize("NFKC", prompt).strip()
        approve, cancel = OK_PATTERN.match(answer), CANCEL_PATTERN.match(answer)
        if approve or cancel:
            finish_feedback(client, event, approve=bool(approve))
            return
        feedback.add_request(prompt)
        handle_request(client, event, f"【修正の依頼】{prompt}", model, effort, feedback_mode=True)
        return
    if approvals.pending(key):
        answer = unicodedata.normalize("NFKC", prompt).strip()
        approve, reject = APPROVE_PATTERN.match(answer), REJECT_PATTERN.match(answer)
        if approve or reject:
            finish_approval(client, event, model, effort, approve=bool(approve))
            return
        # 承認の返事でなければ、承認待ちは取り下げて、新しい指示として扱う
        approvals.withdraw(key)
        clear_approval_messages(client, channel, key)
        prompt = f"（承認を求めた操作は、承認されないまま次の指示が来た。その操作は実行しないこと）\n{prompt}"
    if not (m := FEEDBACK_PATTERN.match(prompt)):
        handle_request(client, event, prompt, model, effort)
        return
    request = prompt[m.end() :].strip()
    if not feedback.enabled():
        post_reply(client, channel, thread_ts, "フィードバックの反映は、まだ設定されていません（GITHUB_TOKEN と GITHUB_REPO が必要です）。")
        return
    if not request and not event.get("files"):
        post_reply(client, channel, thread_ts, "何を直しましょう？")
        return
    try:
        busy = feedback.start(key, request, permalink(client, channel, event["ts"]))
    except Exception as e:
        log.exception("failed to start feedback")
        post_reply(client, channel, thread_ts, f"フィードバックの準備に失敗しました: {e}")
        return
    if busy:
        post_reply(
            client, channel, thread_ts,
            f"別のフィードバックが確認待ちです（[スレッド]({busy['permalink']})）。"
            "そちらを「ok」か「やめる」で閉じてから、もう一度送ってください。",
        )
        return
    prompt = f"【フィードバック】{request}"
    if event.get("thread_ts") and (history := thread_history(client, channel, thread_ts, event["ts"])):
        prompt += f"\n\n【このスレッドのやりとり】\n{history}"
    handle_request(client, event, prompt, model, effort, feedback_mode=True)


def post_proposal(client, channel, thread_ts, reply, is_error):
    """フィードバックの修正案（worktree の差分）を貼り、ok / やめる を案内する。差分がなければ閉じる。"""
    try:
        diff, broken = feedback.proposal(reply)
        if not diff:
            feedback.discard()
    except Exception as e:
        log.exception("failed to build feedback proposal")
        post_reply(client, channel, thread_ts, f"修正案の差分を取れませんでした: {e}")
        return
    if not diff:
        if is_error:
            text = "エラーで修正案を作れなかったので、このフィードバックは閉じました。直ったら、もう一度 fb で送ってください。"
        else:
            text = "設定ファイルは変わらなかったので、このフィードバックは閉じました。"
        post_reply(client, channel, thread_ts, text)
        return
    if len(diff) > DIFF_MAX_LEN:
        diff = diff[:DIFF_MAX_LEN] + "\n…（長いので省略）"
    text = f"```diff\n{diff}\n```\n"
    if broken:
        text += f"JSON・TOML として読めないファイルがあります: {', '.join(broken)}\n"
    text += "「ok」で PR に追加、「やめる」で破棄します。それ以外の返信は、修正の依頼として案を作り直します。"
    post_reply(client, channel, thread_ts, text)


def finish_feedback(client, event, approve):
    """確認待ちのフィードバックを閉じる：ok なら PR に積んで報告し、やめるなら破棄する。"""
    channel = event["channel"]
    thread_ts = event.get("thread_ts") or event["ts"]
    with thread_locks[sessions.thread_key(channel, thread_ts)]:
        try:
            if approve:
                pr = feedback.approve()
                text = f"PR に追加しました: {pr['url']}\nマージされると、次の依頼から反映されます。"
            else:
                feedback.discard()
                text = "この修正案は破棄しました。"
            is_error = False
        except Exception as e:
            log.exception("failed to finish feedback")
            text, is_error = f"失敗しました: {e}", True
    post_reply(client, channel, thread_ts, text)
    if approve and not is_error:
        report(client, f"PR #{pr['number']} に追加: {pr['summary']}\n[元のスレッド]({pr['permalink']}) ・ {pr['url']}")


def finish_approval(client, event, model, effort, approve, ids=None):
    """承認待ちの操作を閉じ、結果を伝えて Claude に続きをさせる。承認なら、同じコマンドが 1 回だけ通るようになる。

    ids は答える承認 ID（ボタンは、押したメッセージの分だけ）。なければ、このスレッドの承認待ち全部（返信での答え）。
    もう答えが出ていたら、何もしない（ボタンの 2 度押しなど）。
    """
    key = sessions.thread_key(event["channel"], event.get("thread_ts") or event["ts"])
    clear_approval_messages(client, event["channel"], key, ids)
    if not approve:
        if approvals.withdraw(key, ids):
            prompt = "【却下】承認を求めた操作は却下された。実行しないこと。ここまでの状況と、ほかに取れる手を短く返すこと。"
            handle_request(client, event, prompt, model, effort)
        return
    if not (items := approvals.approve(key, ids)):
        return
    waited = int((time.time() - min(item["at"] for item in items)) // 60)
    commands = "\n".join(f"```\n{item['command']}\n```" for item in items)
    prompt = (
        f"【承認】{time.strftime('%H:%M')} に、次の操作が承認された（承認を求めてから {waited} 分後）。\n{commands}\n"
        "時間がたって状況が変わっているかもしれないので、必要なら確かめ直してから、同じコマンドで実行して続けること。"
        "実行すべきでなくなっていたら、実行せずに理由を報告すること。"
    )
    handle_request(client, event, prompt, model, effort)


def approval_buttons(ids):
    """承認の依頼に付けるボタン。押したメッセージの分だけに答えられるよう、value に承認 ID を入れる。

    スマホで触っただけでコマンドが走らないよう、承認には確認を挟む。却下は何も実行しないので挟まない。
    """
    value = ",".join(ids)
    confirm = {
        "title": {"type": "plain_text", "text": "承認する？"},
        "text": {"type": "plain_text", "text": "このコマンドを実行するよ"},
        "confirm": {"type": "plain_text", "text": "実行する"},
        "deny": {"type": "plain_text", "text": "戻る"},
    }
    return [{
        "type": "actions",
        "elements": [
            {"type": "button", "action_id": APPROVE_ACTION, "text": {"type": "plain_text", "text": "承認"}, "style": "primary", "value": value, "confirm": confirm},
            {"type": "button", "action_id": REJECT_ACTION, "text": {"type": "plain_text", "text": "却下"}, "value": value},
        ],
    }]


def post_approval_requests(client, channel, thread_ts, key):
    """ガードが承認待ちに積んだ操作を、ボタンを付けてスレッドに出す。ターンの終わりに呼ぶ。

    Claude が取り下げた操作（agent-approval）は出さない。前に出した依頼を取り下げていたら、そのメッセージを消し、
    同じメッセージで待っていた残りは、新しいものと一緒に出し直す。
    理由は Claude 向けの説明が続くので、1 文目からカッコ書きを除いて見せる。コマンドは、承認すると走るものなので削らない。
    """
    # 利用者が答えたときは、メッセージを消してから承認待ちを外す（finish_approval）。承認待ちを先に読んでおけば、
    # 答えが出たばかりのものを取り下げと取り違えない
    pending = {item["id"]: item for item in approvals.pending(key)}
    requests = threadstate.get(key).get("approval_messages", [])
    withdrawn = {i for request in requests for i in request["ids"] if i not in pending}
    again = [pending[i] for request in requests if withdrawn & set(request["ids"]) for i in request["ids"] if i in pending]
    if withdrawn:
        clear_approval_messages(client, channel, key, withdrawn)
    if not (items := again + approvals.take_unposted(key)):
        return
    lines = [
        f"🔐 承認待ち：{re.sub(r'（[^）]*）', '', item['reason'].split('。')[0])}\n```\n{item['command'][:1500]}\n```"
        for item in items
    ]
    ids = [item["id"] for item in items]
    posted = post_chunks(client, channel, thread_ts, "\n".join(lines), approval_buttons(ids))
    threadstate.update(key, approval_messages=threadstate.get(key).get("approval_messages", []) + [{"ts": posted, "ids": ids}])


def clear_approval_messages(client, channel, key, ids=None):
    """答えが出たか、取り下げた承認の依頼のメッセージを消す。スマホで読むときに邪魔になるため。ids を渡すと、その承認 ID のメッセージだけ。

    Claude はこのメッセージを読んでいない（コマンドはガードの返事と【承認】の依頼で受け取る）ので、消しても文脈は変わらない。
    何を承認したかは guard.log に残る。
    """
    requests = threadstate.get(key).get("approval_messages", [])
    answered = [request for request in requests if ids is None or set(request["ids"]) & set(ids)]
    for request in answered:
        for ts in request["ts"]:
            try:
                client.chat_delete(channel=channel, ts=ts)
            except Exception as e:
                log.warning("could not delete the approval request %s: %s", ts, e)
    threadstate.update(key, approval_messages=[request for request in requests if request not in answered])


def upload_outbox(client, channel, thread_ts, outbox):
    """Claude が outbox に置いたファイルをスレッドに添付し、sent/ に移す。"""
    files = workspace.unsent(outbox)
    for path in files[:MAX_UPLOADS]:
        try:
            size = path.stat().st_size
            if size > MAX_ATTACHMENT_BYTES:
                post_reply(client, channel, thread_ts, f"{path.name} は {size / 1e6:.0f} MB あるので添付しませんでした。")
            else:
                client.files_upload_v2(channel=channel, thread_ts=thread_ts, file=str(path), filename=path.name, title=path.name)
            workspace.mark_sent(path)
        except Exception:
            log.exception("failed to upload %s", path)
    if len(files) > MAX_UPLOADS:
        post_reply(client, channel, thread_ts, f"添付は 1 回 {MAX_UPLOADS} 件までなので、残り {len(files) - MAX_UPLOADS} 件は次の返信で送ります。")


def thread_brief(client, channel, thread_ts, key):
    """スレッドの題と URL を、システムプロンプトに足す文にする。

    システムプロンプトが変わると Claude のキャッシュが切れるので、一度取ったら取り直さない。
    """
    brief = threadstate.get(key).get("brief")
    if not brief:
        try:
            root = client.conversations_replies(channel=channel, ts=thread_ts, limit=1)["messages"][0]
        except Exception:
            log.warning("could not read the first message of %s:%s", channel, thread_ts)
            return THREAD_BRIEF.format(title="分からない", url="分からない", thread_ts=thread_ts)
        # ほかの人が立てたスレッドの本文は、システムプロンプトに入れない
        text = root.get("text", "") if root.get("user") in ALLOWED_USER_IDS else ""
        _, text = pick_effort(re.sub(r"<@[A-Z0-9]+>", "", text).strip())
        brief = {"title": (text.splitlines() or [""])[0][:100], "url": permalink(client, channel, thread_ts)}
        threadstate.update(key, brief=brief)
    return THREAD_BRIEF.format(title=brief["title"] or "分からない", url=brief["url"] or "分からない", thread_ts=thread_ts)


def run_turn(client, channel, thread_ts, prompt, resume, model, profile, on_event, env, continues):
    """claude を動かす。時間切れになったら、同じセッションで continues 回まで続きをさせる。"""
    session = {"id": resume}

    def track(event):
        session["id"] = event.get("session_id") or session["id"]
        on_event(event)

    minutes = TIMEOUT_SEC // 60
    for attempt in range(continues + 1):
        try:
            return run_claude(prompt, session["id"], model, profile, track, env)
        except subprocess.TimeoutExpired:
            log.warning("claude timed out after %d s (attempt %d) in %s:%s", TIMEOUT_SEC, attempt + 1, channel, thread_ts)
            if attempt < continues:
                post_reply(client, channel, thread_ts, TIMEOUT_NOTICE.format(minutes=minutes))
                prompt = TIMEOUT_PROMPT.format(minutes=minutes)
    return TIMEOUT_GIVE_UP.format(minutes=minutes, times=continues + 1), session["id"], True


def handle_request(client, event, prompt, model, effort=None, feedback_mode=False, resumed=False):
    """1 件の依頼を処理する：claude 実行（ステータス更新）→ スレッドに返信。

    返信のあとに、ガードが止めた操作の承認の案内と、outbox のファイルを出す。
    作業場を git で同期するチャンネルでは、実行の前に pull し、終わったら commit・push する。
    feedback_mode では profiles/ の修正案を作らせ、返答のあとに差分と確認の案内を貼る。
    それ以外は、受けてから返信するまでを記録し、bot が途中で止まったら次の起動で続きをさせる（resume_thread）。
    resumed はその続きの実行で、また止まっても、もう続けない。
    """
    channel = event["channel"]
    ts = event["ts"]
    thread_ts = event.get("thread_ts") or ts
    key = sessions.thread_key(channel, thread_ts)

    profile = profiles.for_channel(client, channel)
    # メッセージで opus を指定しなかったときは、チャンネルの既定のモデルを使う
    if model == DEFAULT_MODEL and profile.model:
        model = profile.model
    # エフォートも、メッセージで指定したときだけチャンネルの既定を上書きする（fb のプロファイルにも引き継がれる）
    if effort:
        profile = replace(profile, effort=effort)
    if saved := attachments.download(event):
        prompt = "\n\n".join([prompt or "（本文なし。添付ファイルを確認して）", attachments.describe(saved)])
    # スレッドの前の投稿で Claude に届いていないものを渡す。セッションがまだなければ、bot を呼ぶ前に書いた文も全部。
    # フィードバックは別のセッションなので、次のターンに回す。再起動の続きでは、元の依頼の記録に入っている
    if not feedback_mode and event.get("thread_ts"):
        new_session = not resumed and not sessions.get(key)
        if found := attachments.earlier(client, channel, thread_ts, ts, new_session):
            prompt += "\n\n" + attachments.describe_earlier(found)
    outbox = None
    if feedback_mode:
        profile = feedback.profile(profile)
    else:
        outbox = workspace.outbox_dir(profile.work_dir, thread_ts)
        # Write を許していないプロファイルでも、このスレッドの outbox にだけは書けるようにする
        profile = profiles.allow_edit(profile, outbox)
        profile = replace(profile, prompt=profile.prompt + "\n\n" + OUTBOX_HINT.format(dir=outbox))
        # ガードのあるチャンネルは作業場。スレッドを 1 つの作業の単位として進める
        if profile.guard:
            profile = replace(profile, prompt=profile.prompt + "\n\n" + thread_brief(client, channel, thread_ts, key))
    profile = replace(profile, prompt=profile.prompt + "\n\n" + PLAN_HINT)
    # ガードは承認待ちの操作を、このスレッドに積む
    env = {"AGENT_THREAD": key, **PLAN_ENV}
    commit_message = "agent: " + (prompt.strip().splitlines() or [""])[0][:60]
    turn = nullcontext()
    if not feedback_mode:
        # 順番待ちのあいだも記録する。Claude に届く前に止まっても続けられるよう、依頼の文も残す
        turn = threadstate.turn(key, ts, user=event.get("user"), prompt=prompt, model=model, effort=effort, resumed=resumed)
    # 同じスレッドの依頼は 1 件ずつ順に処理する（同じセッションを同時に再開しないため）
    with turn, thread_locks[key], workspace.synced(profile, commit_message) as sync_errors:
        status = StatusReporter(client, channel, thread_ts, MODELS[model]["label"], profile.effort)
        status.start()
        plan = PlanReporter(client, channel, thread_ts, None if feedback_mode else key, on_post=status.refresh)
        plan.start()

        def on_event(event):
            status.on_event(event)
            plan.on_event(event)
            # bot が途中で止まっても同じセッションで続けられるよう、分かったらすぐに残す
            if not feedback_mode and (session := event.get("session_id")) and session != sessions.get(key):
                sessions.save(key, session)

        try:
            # フィードバックは、スレッドのセッションとは別のセッションで扱う
            resume = feedback.session() if feedback_mode else sessions.get(key)
            continues = 0 if feedback_mode else TIMEOUT_CONTINUES
            result, session_id, is_error = run_turn(client, channel, thread_ts, prompt, resume, model, profile, on_event, env, continues)
            if is_error and resume and "No conversation found" in result:
                # 作業場を変えたチャンネルでは、前の作業場で始めたセッションを再開できないので、新しく始める
                log.warning("session %s not found in %s; starting a new one", resume, profile.work_dir)
                result, session_id, is_error = run_turn(client, channel, thread_ts, prompt, None, model, profile, on_event, env, continues)
        except Exception as e:
            log.exception("claude run failed")
            result, session_id, is_error = f"実行エラー: {e}", None, True
        if session_id:
            if feedback_mode:
                feedback.set_session(session_id)
            else:
                sessions.save(key, session_id)

        status.stop()
        try:
            # 返信より先に、待っていることを段取りに添える（実行のあいだに積まれた承認から分かる）
            plan.finish(not feedback_mode and bool(approvals.pending(key)))
        except Exception:
            log.exception("failed to finish the plan")
        try:
            post_reply(client, channel, thread_ts, result)
            if feedback_mode:
                post_proposal(client, channel, thread_ts, result, is_error)
            else:
                post_approval_requests(client, channel, thread_ts, key)
                upload_outbox(client, channel, thread_ts, outbox)
                # 返信が届いたら、このあと止まっても続きは要らない
                threadstate.end_turn(key, ts)
        except Exception:
            log.exception("failed to post reply")
            status.clear()
    if sync_errors:
        post_reply(client, channel, thread_ts, "\n".join(sync_errors))


def resume_thread(key, turns):
    """bot が止まって終わらなかったターン（threadstate.turn の記録）を、スレッドに知らせてから順に続けさせる。

    続けるのは 1 回だけ。続きの実行がまた止まったら、知らせるだけにする。承認はガードが今までどおり止める。
    """
    channel, thread_ts = key.split(":", 1)
    client = app.client
    fresh = {ts: turn for ts, turn in turns.items() if not turn.get("resumed")}
    log.info("resuming %d of %d unfinished turns in %s", len(fresh), len(turns), key)
    lines = ([RESTART_GIVE_UP] if len(fresh) < len(turns) else []) + ([RESTART_NOTICE] if fresh else [])
    try:
        post_reply(client, channel, thread_ts, "\n".join(lines))
    except Exception:
        log.exception("failed to post the restart notice to %s", key)
    if not fresh:
        # 続けないので、進行中のままの段取りを「返事を待ってる」に戻す
        refresh_plan(key)
    for ts, turn in fresh.items():
        event = {"channel": channel, "ts": ts, "thread_ts": thread_ts, "user": turn.get("user")}
        prompt = RESTART_PROMPT.format(now=time.strftime("%m/%d %H:%M"), prompt=turn["prompt"])
        try:
            handle_request(client, event, prompt, turn["model"], turn.get("effort"), resumed=True)
        except Exception:
            log.exception("failed to resume the turn %s in %s", ts, key)


def refresh_plan(key):
    """ターンの外で、段取りに添えた「待っていること」を書き換える。ターンの途中なら、終わるときに書き直すので何もしない。"""
    lock = thread_locks[key]
    if not lock.acquire(blocking=False):
        return
    try:
        channel, thread_ts = key.split(":", 1)
        PlanReporter(app.client, channel, thread_ts, key).refresh(bool(approvals.pending(key)))
    except Exception:
        log.exception("failed to refresh the plan of %s", key)
    finally:
        lock.release()


def answer_approval_button(client, body, approve):
    """承認・却下のボタンが押されたら、返信で答えたときと同じように進める。

    許可していない人のボタンは無視する。Claude を動かすあいだ Slack を待たせないよう、別スレッドで進める。
    """
    user = body["user"]["id"]
    if user not in ALLOWED_USER_IDS:
        log.info("ignored an approval button from %s", user)
        return
    action, message = body["actions"][0], body["message"]
    event = {
        "channel": body["channel"]["id"],
        "ts": action["action_ts"],
        "thread_ts": message.get("thread_ts") or message["ts"],
        "user": user,
    }
    args = (client, event, DEFAULT_MODEL, None, approve, action["value"].split(","))
    threading.Thread(target=finish_approval, args=args, daemon=True).start()


@app.action(APPROVE_ACTION)
def on_approve_button(ack, body, client):
    ack()
    answer_approval_button(client, body, True)


@app.action(REJECT_ACTION)
def on_reject_button(ack, body, client):
    ack()
    answer_approval_button(client, body, False)


@app.event("app_mention")
def on_mention(event, client):
    """メンションで起動する。opus やエフォート（--max など）を指定できるのはこの経路だけ。"""
    if not accept(event):
        return
    prompt = re.sub(r"<@[A-Z0-9]+>", "", event.get("text", "")).strip()
    model, prompt = pick_model(prompt)
    effort, prompt = pick_effort(prompt)
    thread_ts = event.get("thread_ts") or event["ts"]
    if not prompt and not event.get("files"):
        client.chat_postMessage(channel=event["channel"], thread_ts=thread_ts, text="何をしましょう？")
        return
    sessions.register(sessions.thread_key(event["channel"], thread_ts))
    route(client, event, prompt, model, effort)


@app.event("message")
def on_thread_message(event, client, context):
    """一度メンションされたスレッドでは、メンションなしの返信にも Sonnet で応じる。"""
    thread_ts = event.get("thread_ts")
    if not thread_ts or event.get("subtype") not in THREAD_MESSAGE_SUBTYPES or event.get("bot_id"):
        return
    # メンション付きの返信は app_mention 側で処理する（opus やエフォートの指定もそちらで判定する）
    if f"<@{context.bot_user_id}>" in event.get("text", ""):
        return
    if not sessions.is_registered(sessions.thread_key(event["channel"], thread_ts)):
        return
    if not accept(event):
        return
    prompt = event.get("text", "").strip()
    if prompt or event.get("files"):
        route(client, event, prompt, DEFAULT_MODEL)


def main():
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    log.info("starting: work_dir=%s allowed_users=%s", WORK_DIR, sorted(ALLOWED_USER_IDS))
    # 新しいターンが動き出す前に、前の bot の後始末をする（新しいターンの claude と記録を巻き込まないため）
    stop_leftovers()
    unfinished = threadstate.take_unfinished_turns()
    if feedback.enabled():
        feedback.start_sync(lambda text: report(app.client, text))
    for key, turns in unfinished.items():
        threading.Thread(target=resume_thread, args=(key, turns), daemon=True, name=f"resume-{key}").start()
    SocketModeHandler(app, SLACK_APP_TOKEN).start()
