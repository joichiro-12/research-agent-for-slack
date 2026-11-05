"""ガード。Claude Code の PreToolUse フックとして、Bash の実行前に呼ばれる。

コマンドの全文（ssh で送る中身も含む）を、プロファイルの guard.toml の規則（正規表現）で上から順に調べ、
最初に当たった規則で決める。
- deny: 実行させない。理由を Claude に返す
- approve: このスレッドで利用者が承認したコマンドだけ通す。承認がなければ承認待ちに積み、
  承認を求めてターンを終えるよう Claude に返す（bot が承認の案内をスレッドに出す）
どれにも当たらなければ何も返さない（Bash は permissions で許可しているので、そのまま実行される）。
文字列で調べるので、スクリプトの中で何をするかまでは見ない。防ぎたいのは操作の誤りで、悪意ではない。
ただし、cat・tee でファイルに書くだけのヒアドキュメントの本文はデータなので調べない（strip_data_heredocs）。

使い方: python -m research_agent.guard <guard.toml>。標準入力にフックの JSON、環境変数 AGENT_THREAD にスレッド。
"""

import json
import os
import re
import sys
import tomllib
from pathlib import Path

from . import approvals

ACTIONS = {"deny", "approve"}

APPROVE_MESSAGE = """【承認が必要】{reason}。このコマンドは、利用者が Slack で承認するまで実行できない（承認 ID: {id}）。
次のように進めること。
1. 何を・なぜ実行するか、使う資源（GPU・時間・消すものなど）を 3 行以内で書いて承認を求め、このターンを終える。別のコマンドで同じことをしない
2. 承認されたら、状況を確かめ直してから、まったく同じコマンドで実行する"""

DENY_MESSAGE = "【実行できない】{reason}。このコマンドはガードの規則で止めている。別の方法を考えるか、利用者に相談すること。"

NO_THREAD_MESSAGE = "【実行できない】{reason}。承認が必要な操作だが、この実行には承認を受け付けるスレッドが無い。"


def load_rules(path):
    rules = tomllib.loads(Path(path).read_text()).get("rule", [])
    for rule in rules:
        if rule.get("action") not in ACTIONS or not rule.get("pattern") or not rule.get("reason"):
            raise ValueError(f"guard rule needs action (deny/approve), pattern and reason: {rule}")
        re.compile(rule["pattern"])
    return rules


# ---- ヒアドキュメント ----
# メモに「rm -r ...」と書いただけで承認を求めないよう、cat・tee でファイル（か画面）に書くだけの
# ヒアドキュメントの本文は調べない。本文が実行されうる書き方は、これまでどおり本文も調べる。
# - 受け手が cat・tee 以外（bash・ssh・python など）、パイプでほかのコマンドに渡す
# - 引用符・$( )・( )・{ }・if や for などの中にある（ssh '...' の中身や、後ろの | bash に渡るもの）
# - 区切りを引用していない本文に $( ) か ` がある（書く前にコマンドとして実行される）
# - 書いたファイルを、同じコマンドのほかの場所で使う（cat > run.sh <<EOF ... EOF; bash run.sh）
# 読み切れない書き方が 1 か所でもあれば、何も除かず全文を調べる。


class _Unreadable(Exception):
    """シェルの書き方を読み切れなかった。"""


# <<EOF・<<'EOF'・<<"EOF"・<<\EOF・<<-EOF（-は本文の行頭のタブを除く）
_HEREDOC_OP = re.compile(r"<<(-?)[ \t]*(?:'([\w.-]+)'|\"([\w.-]+)\"|\\([\w.-]+)|([\w.-]+))(?=[\s;&|<>()]|$)")
# ${VAR}・${VAR:-x} のような単純なもの。引用符や $( ) を含むものは読まない
_PARAM = re.compile(r"\$\{(?:[^{}'\"`\\$()]|\$\w+)*\}")
# いちばん外側に出てきたら、ヒアドキュメントがどこに渡るか読み切れない語。if・for などはコマンドの位置だけ見る
# （fi・done・esac は必ず区切りのあとに来るので、echo done のような引数と分けられる）
_RESERVED = re.compile(
    r"(?<![^\s;&|])(\{|\}|function|coproc|exec|alias)(?![^\s;&|])"
    r"|(?:^|[;&|\n])\s*(?:!\s+)?(if|then|else|elif|fi|while|until|for|select|do|done|case|esac)(?![^\s;&|])"
)
_PATH = r"(?:[\w./~+@%:,=-]|\$\w+|\$\{\w+\})+"
_DATA_COMMAND = re.compile(r"\s*(cat|tee)(?=\s|$)")
_DATA_ARG = re.compile(rf"\s*\d?>&\d|\s*\d?>>?\s*({_PATH})|\s+({_PATH})")
# ヒアドキュメントのコマンドの前後に来てよい区切り（パイプは不可）
_SEP_BEFORE = {"\n", ";", "&&", "||", "&"}
_SEP_AFTER = {"\n", ";", "&&", "||"}


def _scan(command):
    """コマンドを先頭から読み、(骨組み, 区切り, ヒアドキュメント) を返す。

    骨組みは command と同じ長さで、引用符・$( )・( )・コメント・ヒアドキュメントの本文の中を \\0 で埋めたもの
    （いちばん外側の構造だけが残る）。区切りは、いちばん外側の ; & && || | 改行の (始まり, 終わり, 種類)。
    """
    n = len(command)
    skel = list(command)
    seps = []
    docs = []
    pending = []  # 本文をまだ読んでいないヒアドキュメント（次の改行のあとに本文が来る）
    stack = []  # いま中にいるもの: ' $' " $( (
    i = 0
    while i < n:
        start, top = i, (stack[-1] if stack else None)
        c, two = command[i], command[i : i + 2]
        if top in ("'", "$'"):
            if c == "\\" and top == "$'":
                i += 2
            else:
                if c == "'":
                    stack.pop()
                i += 1
        elif c == "`":
            raise _Unreadable  # `...` は入れ子の引用符の扱いが込み入るので読まない
        elif top == '"':
            if c == "\\":
                i += 2
            elif c == '"':
                stack.pop()
                i += 1
            elif two == "${":
                i = _param_end(command, i)
            elif two == "$(":
                stack.append("$(")
                i += 2
            else:
                i += 1
        # ここから下は、コマンドとして読む場所（いちばん外側か $( ) ( ) の中）
        elif c == "\\":
            i += 2
        elif c == "'" or c == '"':
            stack.append(c)
            i += 1
        elif two == "$'":
            stack.append("$'")
            i += 2
        elif two == "${":
            i = _param_end(command, i)
        elif two == "$(" or c == "(":
            stack.append("$(" if c == "$" else "(")
            i += len(stack[-1])
        elif c == ")":
            if top not in ("$(", "("):
                raise _Unreadable
            stack.pop()
            i += 1
        elif c == "#" and (i == 0 or command[i - 1] in " \t\n;&|()<>"):
            i = _line_end(command, i)
            skel[start:i] = "\0" * (i - start)
        elif command.startswith("<<<", i):
            i += 3
        elif two == "<<":
            m = _HEREDOC_OP.match(command, i)
            if stack or not m:
                raise _Unreadable
            delim = next(d for d in m.groups()[1:] if d)
            pending.append({"op": m.span(), "delim": delim, "strip_tabs": bool(m[1]), "quoted": not m[5]})
            i = m.end()
        elif c == "\n":
            if stack and pending:
                raise _Unreadable
            if not stack:
                seps.append((i, i + 1, "\n"))
            if pending:
                end = _read_bodies(command, i + 1, pending)
                skel[i + 1 : end] = "\0" * (end - i - 1)
                docs += pending
                pending = []
                i = end
            else:
                i += 1
        else:
            token = next(t for t in ("&&", "||", "|&", ";;", "&>", ">&", ">>", ">|", "<&", "<>", c) if command.startswith(t, i))
            if not stack and token in ("&&", "||", "|&", ";;", ";", "|", "&"):
                seps.append((i, i + len(token), token.replace("|&", "|").replace(";;", ";")))
            i += len(token)
        i = min(i, n)
        if top or stack:
            skel[start:i] = "\0" * (i - start)
    if stack or pending:
        raise _Unreadable
    return "".join(skel), seps, docs


def _line_end(command, pos):
    end = command.find("\n", pos)
    return len(command) if end < 0 else end


def _param_end(command, i):
    m = _PARAM.match(command, i)
    if not m:
        raise _Unreadable
    return m.end()


def _read_bodies(command, pos, pending):
    """pos から pending の本文を順に読み、それぞれに本文の範囲を付ける。最後の終わりの行の末尾を返す。"""
    for doc in pending:
        body_start = pos
        while True:
            if pos > len(command):
                raise _Unreadable  # 終わりの行が無い
            end = _line_end(command, pos)
            line = command[pos:end]
            if (line.lstrip("\t") if doc["strip_tabs"] else line) == doc["delim"]:
                doc["body"] = (body_start, pos)
                break
            # 区切りを引用しない本文では、行末の \ で次の行とつながり、終わりの行の位置が変わる
            if not doc["quoted"] and line.endswith("\\"):
                raise _Unreadable
            pos = end + 1
        pos = end + 1
    return end


def _data_targets(text):
    """text（ヒアドキュメントを受けるコマンド）が cat・tee で書くだけなら書き先の一覧を、それ以外なら None を返す。"""
    m = _DATA_COMMAND.match(text)
    if not m:
        return None
    targets, pos = [], m.end()
    while text[pos:].strip():
        arg = _DATA_ARG.match(text, pos)
        if not arg:
            return None
        if arg[1] or (arg[2] and m[1] == "tee" and not arg[2].startswith("-")):
            targets.append(arg[1] or arg[2])
        pos = arg.end()
    return targets


def strip_data_heredocs(command):
    """cat・tee でファイル（か画面）に書くだけのヒアドキュメントの本文を除いたコマンドを返す。"""
    try:
        skel, seps, docs = _scan(command)
    except _Unreadable:
        return command
    if _RESERVED.search(skel):
        return command
    spans = []
    for doc in docs:
        (op_start, op_end), (body_start, body_end) = doc["op"], doc["body"]
        before = [sep for sep in seps if sep[1] <= op_start]
        after = next(sep for sep in seps if sep[0] >= op_end)
        if (before and before[-1][2] not in _SEP_BEFORE) or after[2] not in _SEP_AFTER:
            continue
        start, end = before[-1][1] if before else 0, after[0]
        targets = _data_targets(skel[start:op_start] + " " + skel[op_end:end])
        if targets is None:
            continue
        if not doc["quoted"] and re.search(r"\$\(|`", command[body_start:body_end]):
            continue
        rest = command[:start] + command[end:body_start] + command[body_end:]
        if any((target.rstrip("/").rsplit("/", 1)[-1] or target) in rest for target in targets):
            continue
        spans.append((body_start, body_end))
    for body_start, body_end in reversed(spans):
        command = command[:body_start] + command[body_end:]
    return command


def match(command, rules):
    """最初に当たった規則を返す（当たらなければ None）。データとして書くだけのヒアドキュメントの本文は調べない。"""
    command = strip_data_heredocs(command)
    return next((rule for rule in rules if re.search(rule["pattern"], command)), None)


def decide(command, rules, thread):
    """(判定, 当たった規則, Claude に返す理由) を返す。判定は allow・approved・pending・deny。"""
    rule = match(command, rules)
    if rule is None:
        return "allow", None, None
    if rule["action"] == "deny":
        return "deny", rule, DENY_MESSAGE.format(reason=rule["reason"])
    if not thread:
        return "deny", rule, NO_THREAD_MESSAGE.format(reason=rule["reason"])
    if approvals.consume(thread, command):
        return "approved", rule, None
    request_id = approvals.request(thread, command, rule["reason"])
    return "pending", rule, APPROVE_MESSAGE.format(reason=rule["reason"], id=request_id)


def main():
    event = json.load(sys.stdin)
    if event.get("tool_name") != "Bash":
        return
    command = event.get("tool_input", {}).get("command", "")
    thread = os.environ.get("AGENT_THREAD", "")
    verdict, rule, reason = decide(command, load_rules(sys.argv[1]), thread)
    if rule is not None:
        approvals.log({"thread": thread, "verdict": verdict, "rule": rule.get("name", ""), "command": command[:2000]})
    if reason:
        output = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
        print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
