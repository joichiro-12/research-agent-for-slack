"""ガード（guard.py）の判定を、研究チャンネルの guard.toml で確かめる。

実行: uv run python -m unittest discover -s tests
"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from research_agent import approvals, guard

ROOT = Path(__file__).resolve().parents[1]
RULES = guard.load_rules(ROOT / "profiles/research/guard.toml")
DELETE = "recursive-delete"


def rule_name(command):
    rule = guard.match(command, RULES)
    return rule and rule["name"]


class DataHeredocTest(unittest.TestCase):
    """cat・tee でファイルに書くだけのヒアドキュメントの本文は調べない。"""

    def test_note_mentioning_a_command(self):
        # メモを書き足しただけで承認を求めない
        command = (
            "cat >> notes/progress.md <<'EOF'\n"
            "\n"
            "## 流し直し\n"
            "- 承認を得て、出力を消してから 1 本流し直した（`rm -r outputs/run1`）\n"
            "- 後処理はまだ動かしていない【未確認】\n"
            "EOF\n"
            "ls outputs 2>&1 | tail -2"
        )
        self.assertIsNone(rule_name(command))
        self.assertEqual(guard.decide(command, RULES, "C0:1")[0], "allow")

    def test_delimiter_forms(self):
        for op in ("<<EOF", "<<'EOF'", '<<"EOF"', "<<\\EOF", "<< EOF", "<<-EOF"):
            with self.subTest(op=op):
                self.assertIsNone(rule_name(f"cat > notes.md {op}\nrm -r build\nEOF"))
        self.assertIsNone(rule_name("cat > notes.md <<-'EOF'\n\trm -r build\n\tEOF"))

    def test_other_data_commands(self):
        for command in (
            "cat <<'EOF'\nrm -r build\nEOF",
            "cat <<'EOF' >> notes.md\nrm -r build\nEOF",
            "tee -a notes.md <<'EOF'\nrm -r build\nEOF",
            "cd ~/ws && cat >> notes.md <<'EOF'\nkill 123\nEOF\necho done",
            "# don't forget\ncat >> notes.md <<'EOF'\nsudo rm -r x\nEOF",
            "cat > a.md <<'A'; cat > b.md <<'B'\nrm -r x\nA\ntmux kill-server\nB",
        ):
            with self.subTest(command=command):
                self.assertIsNone(rule_name(command))

    def test_text_outside_the_body_is_still_checked(self):
        self.assertEqual(rule_name("cat > notes.md <<'EOF'\nnote\nEOF\nrm -r build"), DELETE)
        self.assertEqual(rule_name("cat > notes.md <<'EOF'\nnote\nEOF\nsudo ls"), "sudo")
        self.assertEqual(rule_name("cat >> ~/.ssh/authorized_keys <<'EOF'\nkey\nEOF"), "secrets")
        self.assertEqual(rule_name("cat > notes.md <<'EOF' && rm -r build\nnote\nEOF"), DELETE)


class ExecutedHeredocTest(unittest.TestCase):
    """本文が実行されうるヒアドキュメントは、これまでどおり本文も調べる。"""

    def assertChecked(self, command, rule=DELETE):
        self.assertEqual(rule_name(command), rule, command)

    def test_fed_to_a_shell_or_interpreter(self):
        for consumer in ("bash", "sh", "zsh", "ssh server", "python3", "xargs", "eval", "bash -s"):
            with self.subTest(consumer=consumer):
                self.assertChecked(f"{consumer} <<EOF\nrm -r x\nEOF")
                self.assertChecked(f"{consumer} <<'EOF'\nrm -r x\nEOF")

    def test_piped(self):
        self.assertChecked("cat <<EOF | bash\nrm -r x\nEOF")
        self.assertChecked("cat <<'EOF' | ssh server bash\nrm -r x\nEOF")
        self.assertChecked("tee log <<'EOF' | bash\nrm -r x\nEOF")
        self.assertChecked("cat <<'EOF' |& bash\nrm -r x\nEOF")

    def test_inside_quotes_or_substitution(self):
        self.assertChecked("ssh server 'cat > f <<EOF\nrm -r x\nEOF\n'")
        self.assertChecked("ssh server \"cat > f <<'EOF'\nrm -r x\nEOF\n\"")
        self.assertChecked("ssh server \"$(cat <<'EOF'\nrm -r x\nEOF\n)\"")
        self.assertChecked("bash -c \"$(cat <<'EOF'\nrm -r x\nEOF\n)\"")
        self.assertChecked("echo '\ncat > f <<EOF\n'\nrm -r x\nEOF")

    def test_inside_compound_commands(self):
        self.assertChecked("{ cat <<'EOF'\nrm -r x\nEOF\n} | bash")
        self.assertChecked("( cat <<'EOF'\nrm -r x\nEOF\n) | bash")
        self.assertChecked("if true; then cat <<'EOF'\nrm -r x\nEOF\nfi | bash")
        self.assertChecked("for i in 1; do cat <<'EOF'\nrm -r x\nEOF\ndone | bash")
        self.assertChecked("exec > >(bash)\ncat <<'EOF'\nrm -r x\nEOF")
        self.assertChecked("cat() { bash; }\ncat <<'EOF'\nrm -r x\nEOF")

    def test_unquoted_body_with_command_substitution(self):
        self.assertChecked("cat > notes.md <<EOF\n$(rm -r build)\nEOF")
        self.assertChecked("cat > notes.md <<EOF\nid=`rm -r build`\nEOF")
        self.assertIsNone(rule_name("cat > notes.md <<'EOF'\n$(rm -r build)\nEOF"))

    def test_written_file_is_used(self):
        self.assertChecked("cat > run.sh <<'EOF'\nrm -r x\nEOF\nbash run.sh")
        self.assertChecked("cat > ~/ws/run.sh <<'EOF'\nrm -r x\nEOF\ncd ~/ws && sh ./run.sh")
        self.assertChecked("tee run.sh <<'EOF'\nrm -r x\nEOF\nchmod +x run.sh")

    def test_other_commands(self):
        self.assertChecked("cat - > f <<'EOF' >(bash)\nrm -r x\nEOF")
        self.assertChecked("command cat > f <<'EOF'\nrm -r x\nEOF")
        self.assertChecked("FOO=1 cat > f <<'EOF'\nrm -r x\nEOF")
        self.assertChecked("cat > f <<'EOF' <<'EOS'\nrm -r x\nEOF\nrm -r y\nEOS")
        # 同じ行の 2 つめのヒアドキュメントが bash に渡る。1 つめ（メモ）だけ除く
        self.assertChecked("cat > a.md <<'A'; bash <<'B'\nsudo note\nA\nrm -r x\nB")
        self.assertIsNone(rule_name("cat > a.md <<'A'; bash <<'B'\nrm -r note\nA\necho hi\nB"))

    def test_unreadable_commands_are_checked_whole(self):
        # 終わりの行が無い
        self.assertChecked("cat > f <<'EOF'\nrm -r x")
        # 引用しない区切りでは、行末の \ で終わりの行の位置が変わる（bash では 1 つめの EOF は本文）
        self.assertChecked("cat > f <<EOF\nx \\\nEOF\ncat > g <<X\nEOF\nrm -r x\nX")
        # 区切りの書き方を読めない
        self.assertChecked("cat > f <<E\"O\"F\nrm -r x\nEOF")
        # 閉じていない引用符
        self.assertChecked("cat > f <<'EOF'\nrm -r x\nEOF\necho 'oops")

    def test_here_string_is_not_a_heredoc(self):
        self.assertChecked("bash <<< 'rm -r x'")


class StripTest(unittest.TestCase):
    def test_strips_only_the_body(self):
        command = "cat >> n.md <<'EOF'\nrm -r x\nEOF\necho ok"
        self.assertEqual(guard.strip_data_heredocs(command), "cat >> n.md <<'EOF'\nEOF\necho ok")

    def test_unchanged_without_heredoc(self):
        for command in ("ls -la", "echo $((1<<2))", "rm -r build", 'echo "${HOME}/x" # note'):
            self.assertEqual(guard.strip_data_heredocs(command), command)


class KillRuleTest(unittest.TestCase):
    """kill は実際に呼ぶ形だけ承認を待つ。"""

    def test_word_in_a_search_or_text(self):
        for command in (
            'cat notes/README.md && echo ---- && ls -la notes/ && echo ---- && grep -rIl -E "kill|学習|point" notes | head -50',
            "grep -rn kill notes",
            'grep -n "kill" docs/a.md | head',
            'grep -n "kill 学習" notes/a.md',
            "find notes -name '*.md' | xargs grep -l kill",
            'echo "kill the old process first"',
        ):
            with self.subTest(command=command):
                self.assertIsNone(rule_name(command))

    def test_real_calls(self):
        for command in (
            "kill 123",
            "/bin/kill 123",
            "kill -9 123",
            "kill -TERM 123",
            "kill -s TERM 123",
            "kill -- -123",
            "kill $PID",
            'kill "$PID"',
            "kill $(cat pid)",
            "kill `cat pid`",
            "kill %1",
            "kill \\\n123",
            "ssh server 'kill 123'",
            'ssh server "cd x && kill -9 123"',
            "for p in 1 2; do kill $p; done",
            "find . -name pid -exec kill {} \\;",
            "xargs kill < pids.txt",
            "<pids.txt xargs -r kill",
        ):
            with self.subTest(command=command):
                self.assertEqual(rule_name(command), "kill")

    def test_earlier_rules_still_apply(self):
        self.assertEqual(rule_name("pkill -f train"), "pattern-kill")
        self.assertEqual(rule_name("killall python"), "pattern-kill")
        self.assertEqual(rule_name("pgrep train | xargs kill"), "pattern-kill")
        self.assertEqual(rule_name("tmux kill-session -t x"), "tmux")


class RecursiveDeleteRuleTest(unittest.TestCase):
    """再帰的な削除は、書き方によらず承認を待つ。"""

    def test_every_form_waits_for_approval(self):
        for command in (
            "rm -rf build",
            "rm -Rf build",
            "rm -v -rf build",
            "rm -r build",
            "rm -R build",
            "rm -fr build",
            "rm -r -f build",
            "cd x && rm -rf build",
            "ssh server 'rm -rf build'",
            "rsync -a --delete a/ b/",
        ):
            with self.subTest(command=command):
                self.assertEqual(rule_name(command), DELETE)

    def test_plain_rm_passes(self):
        self.assertIsNone(rule_name("rm notes.tmp"))
        self.assertIsNone(rule_name("rm -f notes.tmp"))


class SecretsRuleTest(unittest.TestCase):
    def test_bot_tokens_are_out_of_reach(self):
        self.assertEqual(rule_name("cat config/.env"), "secrets")
        self.assertEqual(rule_name("cat ~/research-agent-for-slack/config/.env | head"), "secrets")
        self.assertEqual(rule_name("cat ~/.ssh/id_ed25519"), "secrets")

    def test_the_example_file_is_readable(self):
        self.assertIsNone(rule_name("cat config/.env.example"))


class GhRuleTest(unittest.TestCase):
    """gh は読むだけの操作を通し、トークンの操作は止め、ほかは承認を待つ。"""

    def assertRule(self, name, commands):
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(rule_name(command), name)

    def test_reads_pass(self):
        self.assertRule(None, (
            "gh pr view 1 --repo a/b",
            "gh pr list",
            "gh pr diff 1",
            "gh pr checks 1",
            "gh repo view a/b",
            "gh repo clone a/b",
            "gh run list",
            "gh auth status",
            "gh --version",
            "gh api repos/a/b --silent",
            "gh api repos/a/b/pulls/1/comments --paginate --jq '.[].body'",
            "gh api repos/a/b | jq -f filter.jq",
            "cd ~/x && gh pr view 1 | head -5",
            'ssh server "gh pr list"',
        ))

    def test_words_in_text_pass(self):
        self.assertRule(None, (
            'grep -n "gh が無い" notes/a.md',
            "grep -rn high notes",
            "cat > notes.md <<'EOF'\ngh pr merge 1\nEOF",
        ))

    def test_writes_wait_for_approval(self):
        self.assertRule("gh", (
            "gh pr create --base main --title x --body-file b.md",
            "gh pr edit 1 --body-file b.md",
            "gh pr comment 1 --body x",
            "gh pr merge 1",
            "gh pr close 1",
            "gh repo create x --public",
            "gh repo delete a/b",
            "gh repo edit a/b --visibility private",
            "gh release create v1",
            "gh gist create f",
            "gh secret set X",
            "gh workflow run w",
            "gh alias set x y",
            "gh extension install a/b",
            "gh some-new-command",
            "gh  pr  merge 1",
            "~/.local/bin/gh pr merge 1",
            "cd x && gh pr merge 1",
            "gh pr view 1 && gh pr merge 1",
            "ssh server 'gh pr merge 1'",
        ))

    def test_api_writes_wait_for_approval(self):
        self.assertRule("gh-api-write", (
            "gh api -X POST repos/a/b/issues",
            "gh api -XPOST repos/a/b/issues",
            "gh api --method DELETE repos/a/b",
            "gh api repos/a/b/issues -f title=x",
            "gh api repos/a/b/issues -Ftitle=x",
            "gh api graphql --raw-field query=x",
            "gh api repos/a/b/contents/x --input body.json",
            "gh pr view 1; gh api -X DELETE repos/a/b",
        ))

    def test_token_is_out_of_reach(self):
        self.assertRule("gh-auth", (
            "gh auth token",
            "gh auth login --with-token",
            "gh auth logout",
            "gh auth refresh",
            "gh auth setup-git",
            "gh auth git-credential get",
            "gh auth status --show-token",
            "gh auth status -t",
            "~/.local/bin/gh auth token",
            "git credential fill",
            "git -C ~/x credential fill",
        ))
        self.assertEqual(rule_name("cat ~/.config/gh/hosts.yml"), "secrets")

    def test_credential_config_is_not_a_token_command(self):
        self.assertIsNone(rule_name("git config --get-all credential.helper"))


class PublicDemoRuleTest(unittest.TestCase):
    """デモは手元か tailnet の中だけに出す。インターネットに公開する呼び出しは止める。"""

    def assertRule(self, name, commands):
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(rule_name(command), name)

    def test_tailnet_serve_and_port_forwarding_pass(self):
        self.assertRule(None, (
            "tailscale serve --bg --https=8443 http://127.0.0.1:7860",
            "ssh gpu1 'tailscale serve status'",
            "tailscale serve --https=8443 off",
            "ssh -N -L 7860:localhost:7860 gpu1",
        ))

    def test_words_in_text_pass(self):
        self.assertRule(None, (
            "grep -n funnel docs/setup.md",
            "which cloudflared",
            "grep -rn ngrok notes",
        ))

    def test_public_urls_are_denied(self):
        self.assertRule("public-demo", (
            "tailscale funnel 3000",
            "ssh gpu1 'tailscale funnel --bg --https=443 http://127.0.0.1:7860'",
            "tailscale --socket=/tmp/tailscaled.sock funnel 7860",
            "cloudflared tunnel --url http://127.0.0.1:7860",
            "./cloudflared-linux-amd64 tunnel --url http://localhost:7860",
            "ngrok http 7860",
        ))
        self.assertEqual(guard.decide("tailscale funnel --bg 7860", RULES, "C0:1")[0], "deny")


class PushRuleTest(unittest.TestCase):
    """git push は行き先によらず承認を待ち、履歴を書き換えるものは止める。"""

    def assertRule(self, name, commands):
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(rule_name(command), name)

    def test_every_push_waits_for_approval(self):
        self.assertRule("push", (
            "git push",
            "git push origin",
            "git push origin feature/x",
            "git push -u origin feature/x",
            "git -C ~/x push origin feature/x",
            "git push origin main",
            "git push origin main 2>&1 | tail -2",
            "git push origin HEAD:main",
            "git push upstream feature/x",
            "git push https://github.com/a/b.git feature/x",
            "git push git@github.com:a/b.git feature/x",
            "cd ~/x && git push origin a && git push origin b",
        ))

    def test_deleting_and_rewriting_wait_for_approval(self):
        self.assertRule("push", (
            "git push origin --delete feature/x",
            "git push origin :feature/x",
            "git push origin +feature/x",
            "git push --tags origin",
            "git push --mirror origin",
        ))

    def test_force_is_denied(self):
        self.assertRule("force-push", (
            "git push --force origin feature/x",
            "git push -f origin feature/x",
            "git push origin feature/x --force",
            "git push --force-with-lease origin feature/x",
        ))

    def test_over_ssh_waits_for_approval(self):
        self.assertRule("push", (
            "ssh server 'cd x && git push origin feature/x && echo ok'",
            'ssh server "git push origin feature/x"',
            "ssh server <<'EOF'\ncd x\ngit push origin feature/x\nEOF",
        ))

    def test_stash_push_passes(self):
        self.assertIsNone(rule_name("git stash push -m x"))
        self.assertIsNone(rule_name("ssh server 'git stash push -m x'"))


class WithdrawTest(unittest.TestCase):
    """ガードが止めた操作をせずに済ませるとき、Claude が承認の依頼を取り下げる（bin/agent-approval）。"""

    THREAD = "C1:100.1"

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        for attr, path in (("RUNTIME_DIR", self.folder), ("STATE_FILE", self.folder / "approvals.json"),
                           ("LOCK_FILE", self.folder / "approvals.lock"), ("LOG_FILE", self.folder / "guard.log")):
            patcher = mock.patch.object(approvals, attr, path)
            patcher.start()
            self.addCleanup(patcher.stop)

    def block(self, command):
        """ガードに止めさせて、承認 ID と Claude への返事を返す。"""
        verdict, _, reason = guard.decide(command, RULES, self.THREAD)
        self.assertEqual(verdict, "pending")
        return approvals.pending(self.THREAD)[-1]["id"], reason

    def run_cli(self, *args, thread=THREAD):
        """bot が動かす Claude の Bash と同じく、別のプロセスで動かす。"""
        env = {**os.environ, "AGENT_THREAD": thread, "AGENT_RUNTIME": str(self.folder)}
        return subprocess.run([ROOT / "bin/agent-approval", *args], env=env, capture_output=True, text=True, timeout=60)

    def test_the_guard_tells_how_to_withdraw(self):
        request_id, reason = self.block("rm -rf /tmp/work")
        self.assertIn(f"`agent-approval --withdraw {request_id}`", reason)
        self.assertIsNone(rule_name(f"agent-approval --withdraw {request_id}"))

    def test_only_the_withdrawn_request_stops_waiting(self):
        # 作業用に広げたファイルを消そうとして止まり、消さずに済ませたとき
        request_id, _ = self.block("rm -rf /tmp/work && mkdir /tmp/work")
        kept, _ = self.block("tmux new -d -s train 'bash train.sh'")
        done = self.run_cli("--withdraw", request_id)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(f"取り下げた: {request_id}", done.stdout)
        self.assertEqual([item["id"] for item in approvals.take_unposted(self.THREAD)], [kept])
        record = json.loads((self.folder / "guard.log").read_text().splitlines()[-1])
        self.assertEqual((record["verdict"], record["id"]), ("withdrawn", request_id))

    def test_unknown_ids_and_other_threads_fail(self):
        theirs = approvals.request("C1:999.1", "bash other.sh", "GPU を使う実行")
        done = self.run_cli("--withdraw", theirs, "ffffff")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn(f"このスレッドの承認待ちにない: {theirs} ffffff", done.stderr)
        self.assertEqual(len(approvals.pending("C1:999.1")), 1)

    def test_claude_cannot_approve(self):
        request_id, _ = self.block("tmux new -d -s train 'bash train.sh'")
        self.assertNotEqual(self.run_cli("--approve", request_id).returncode, 0)
        self.assertFalse(approvals.consume(self.THREAD, "tmux new -d -s train 'bash train.sh'"))
        self.assertEqual(len(approvals.pending(self.THREAD)), 1)

    def test_needs_the_thread(self):
        request_id, _ = self.block("tmux new -d -s train 'bash train.sh'")
        done = self.run_cli("--withdraw", request_id, thread="")
        self.assertIn("AGENT_THREAD がない", done.stderr)
        self.assertEqual(len(approvals.pending(self.THREAD)), 1)


if __name__ == "__main__":
    unittest.main()
