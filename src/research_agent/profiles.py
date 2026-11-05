"""チャンネルごとのプロファイル（追加の指示と権限）。

システムプロンプトには、どのチャンネルでも profiles/persona.md（共通のペルソナ）を入れる。
そのうえで config/channels.toml でチャンネル名とプロファイル名を対応づけ、
profiles/<名前>/prompt.md（システムプロンプトに追記）と permissions.json（許可・禁止を追加）を読む。
guard.toml があるプロファイルでは、それを規則にしたガード（guard.py）を PreToolUse フックとして付け、bot のリポジトリを Edit できなくする。
channels.toml では、チャンネルごとに作業場（work_dir）と、その git での同期（git_sync）、既定のモデル（model）とエフォート（effort）も決められる。
対応づけのないチャンネルは、ペルソナと共通の設定（config/claude-settings.json）だけで動く。
channels.toml の [feedback] report には、フィードバックの PR を報告するチャンネルを書く。
ファイルは依頼のたびに読み直すので、編集は再起動なしで反映される。
"""

import json
import logging
import shlex
import sys
import tomllib
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path

from .config import (
    CHANNELS_FILE,
    CLAUDE_MD_EXCLUDES,
    DEFAULT_EFFORT,
    EFFORTS,
    MODELS,
    PERSONA_FILE,
    PROFILES_DIR,
    ROOT_DIR,
    SETTINGS_FILE,
    STATUS_HINT,
    WORK_DIR,
)

log = logging.getLogger(__name__)

DEFAULT_PROFILE = "default"

_channel_names = {}
_channel_ids = {}


@dataclass
class Profile:
    name: str
    prompt: str  # claude --append-system-prompt-file で渡す全文
    settings: str  # claude --settings に渡す JSON
    work_dir: Path = WORK_DIR  # claude を動かすディレクトリ
    git_sync: bool = False  # 依頼の前後に work_dir を git で pull・push する
    guard: bool = False  # ガードを付けている（承認を受け付けるスレッドを claude に渡す）
    model: str | None = None  # このチャンネルの既定のモデル（メッセージで opus を指定しなかったとき）
    effort: str = DEFAULT_EFFORT  # claude --effort に渡す値


def _normalize(name):
    # 全角・半角や大文字・小文字の違いで一致しなくならないようにそろえる
    return unicodedata.normalize("NFKC", name).lower()


def channel_names(client, channel_id):
    """チャンネルの名前の候補（name と name_normalized）を返す。"""
    if channel_id not in _channel_names:
        try:
            info = client.conversations_info(channel=channel_id)["channel"]
        except Exception as e:
            log.warning("could not resolve channel name for %s: %s", channel_id, e)
            return set()
        _channel_names[channel_id] = {_normalize(info[k]) for k in ("name", "name_normalized") if info.get(k)}
    return _channel_names[channel_id]


def _mapping():
    return tomllib.loads(CHANNELS_FILE.read_text()) if CHANNELS_FILE.exists() else {}


def entries():
    """channels.toml のチャンネルの対応づけを、(チャンネル名, 表) で返す。"""
    for channel, entry in _mapping().items():
        # "チャンネル" = "プロファイル" と、"チャンネル" = { profile = …, work_dir = … } の 2 通りの書き方がある
        if isinstance(entry, str):
            entry = {"profile": entry}
        # [feedback] のような、チャンネルの対応づけではない表は飛ばす
        if "profile" in entry:
            yield channel, entry


def for_channel(client, channel_id):
    names = channel_names(client, channel_id)
    for channel, entry in entries():
        if _normalize(channel) in names:
            return load(entry["profile"], entry)
    return load(DEFAULT_PROFILE)


def report_channel(client):
    """フィードバックの PR を報告するチャンネルの ID を返す（未設定、または見つからなければ None）。"""
    name = _mapping().get("feedback", {}).get("report")
    return find_channel(client, name) if name else None


def find_channel(client, name):
    """チャンネル名から ID を返す（見つからなければ None）。"""
    if name not in _channel_ids:
        target, cursor = _normalize(name), None
        while True:
            res = client.conversations_list(
                types="public_channel,private_channel", exclude_archived=True, limit=1000, cursor=cursor
            )
            for info in res["channels"]:
                if target in {_normalize(info[k]) for k in ("name", "name_normalized") if info.get(k)}:
                    _channel_ids[name] = info["id"]
            cursor = res.get("response_metadata", {}).get("next_cursor")
            if name in _channel_ids or not cursor:
                break
        if name not in _channel_ids:
            log.warning("channel %s not found; is the bot invited?", name)
            return None
    return _channel_ids[name]


def base_settings():
    """共通の設定（config/claude-settings.json）に、リポジトリの CLAUDE.md を読ませない指定を足して返す。"""
    settings = json.loads(SETTINGS_FILE.read_text())
    settings["claudeMdExcludes"] = settings.get("claudeMdExcludes", []) + CLAUDE_MD_EXCLUDES
    return settings


def load(name, channel=None):
    """プロファイルを読む。channel は channels.toml のそのチャンネルの表（work_dir・git_sync・model・effort）。"""
    channel = channel or {}
    settings = base_settings()
    prompt = [STATUS_HINT]
    if PERSONA_FILE.exists():
        prompt.append(PERSONA_FILE.read_text())
    profile_dir = PROFILES_DIR / name
    if (profile_dir / "prompt.md").exists():
        prompt.append((profile_dir / "prompt.md").read_text())
    if (profile_dir / "permissions.json").exists():
        extra = json.loads((profile_dir / "permissions.json").read_text())
        permissions = settings.setdefault("permissions", {})
        for kind in ("allow", "deny"):
            permissions[kind] = permissions.get(kind, []) + extra.get(kind, [])
    guard_file = profile_dir / "guard.toml"
    if guard := guard_file.exists():
        # bot と同じ python で動かす。フックが落ちたり時間切れになったりしたら、コマンドを止める
        command = f"{shlex.quote(sys.executable)} -m research_agent.guard {shlex.quote(str(guard_file))}"
        hook = {"type": "command", "command": command, "timeout": 30, "onFailure": "block"}
        settings.setdefault("hooks", {}).setdefault("PreToolUse", []).append({"matcher": "Bash", "hooks": [hook]})
        # ガードの規則や承認の状態を、Edit で書き換えさせない（// で始まる規則は絶対パス）
        permissions = settings.setdefault("permissions", {})
        permissions["deny"] = permissions.get("deny", []) + [f"Edit(/{ROOT_DIR}/**)"]
    work_dir = Path(channel["work_dir"]).expanduser() if "work_dir" in channel else WORK_DIR
    if (model := channel.get("model")) and model not in MODELS:
        log.warning("unknown model %s for profile %s; using the default", model, name)
        model = None
    if (effort := channel.get("effort", DEFAULT_EFFORT)) not in EFFORTS:
        log.warning("unknown effort %s for profile %s; using the default", effort, name)
        effort = DEFAULT_EFFORT
    return Profile(
        name,
        "\n\n".join(prompt),
        json.dumps(settings, ensure_ascii=False),
        work_dir,
        bool(channel.get("git_sync")),
        guard,
        model,
        effort,
    )


def allow_edit(profile, folder):
    """folder の下にだけファイルを書けるようにしたプロファイルを返す（// で始まる規則は絶対パス）。"""
    settings = json.loads(profile.settings)
    settings.setdefault("permissions", {}).setdefault("allow", []).append(f"Edit(/{folder}/**)")
    return replace(profile, settings=json.dumps(settings, ensure_ascii=False))
