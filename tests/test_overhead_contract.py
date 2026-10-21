"""Consumer-level delivery of the versioned overhead contract.

These tests exercise the actual project attachment, setup refresh, live policy
settings and lifecycle hook boundaries.  They also keep the human-maintained
linked guide and every path outside the isolated runtime homes byte-identical.
"""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from codex_deepseek_team import (cli, coordinator_hooks, onboarding, overhead_contract,
                                 project, settings)


LINKED = b"<!-- codex-deepseek-team:guidance:linked -->"
PROFILE_HEADER = b"### Effective delegation profile"
GUIDE_RELATIVE = Path("docs") / "agents" / "delegation.md"
ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "scripts" / "check_installation.py"
MAX_LINKED_BLOCK = 8192
CONTINUITY_HEADING = "### Correction and workspace continuity"
CONTINUITY_TOKENS = (
    "correction_of",
    "workspace_id",
    "job_id",
    "attempt_id",
    "conflicting",
    "ambiguous",
    "stale",
    "foreign",
    "finalized",
    "allocating or preparing a new copy",
    "--resume-after-failure",
    "not universal program enforcement",
)


def load_checker():
    spec = importlib.util.spec_from_file_location("installation_check_overhead", CHECKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class OverheadContractCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="dsteam-overhead-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", os.fspath(self.repo)], check=True,
                       stderr=subprocess.DEVNULL)
        (self.repo / "a.py").write_text("VALUE = 1\n", encoding="utf-8")
        subprocess.run(["git", "-C", os.fspath(self.repo), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", os.fspath(self.repo), "-c", "user.name=Test",
             "-c", "user.email=test@example.test", "commit", "-qm", "base"], check=True)

        self.home = self.root / "home"
        self.home.mkdir()
        self.codex = self.root / "codex"
        self.claude = self.root / "claude"
        self.config = self.root / "config"
        self.state = self.root / "state"
        environment = mock.patch.dict(os.environ, {
            "HOME": str(self.home),
            "CODEX_HOME": str(self.codex),
            "CLAUDE_CONFIG_DIR": str(self.claude),
            "XDG_CONFIG_HOME": str(self.config),
            "DEEPSEEK_TEAM_STATE_DIR": str(self.state),
        }, clear=False)
        environment.start()
        self.addCleanup(environment.stop)
        self.addCleanup(os.environ.pop, "DEEPSEEK_TEAM_DISABLED", None)
        os.environ.pop("DEEPSEEK_TEAM_DISABLED", None)

        # Paths that resemble inherited homes but are not selected.  No command
        # under this isolated environment may create or modify them.
        self.outside = self.root / "outside"
        self.outside.mkdir()
        (self.outside / ".config" / "deepseek-team").mkdir(parents=True)
        (self.outside / ".config" / "deepseek-team" / "config.toml").write_bytes(
            b'delegation_level = "75"\n')
        (self.outside / ".codex").mkdir()
        (self.outside / ".codex" / "hooks.json").write_bytes(b'{"outside": true}\n')
        (self.outside / ".claude").mkdir()
        (self.outside / ".claude" / "settings.json").write_bytes(b'{"outside": true}\n')
        self.decoy_before = self.tree(self.outside)

    @staticmethod
    def tree(root):
        return {path.relative_to(root): (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
                for path in sorted(Path(root).rglob("*")) if path.is_file()}

    def target(self, runtime="codex"):
        return self.repo / project.TARGETS[runtime]

    def guide(self, data=None):
        path = self.repo / GUIDE_RELATIVE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data or b"# Human guide\n\nHuman-maintained delegation rules.\n")
        path.chmod(0o640)
        return path

    def attach_full(self, runtime="codex"):
        return project.attach(self.repo, coordinator=runtime)

    def inject_linked_marker(self, runtime="codex"):
        path = self.target(runtime)
        content = path.read_bytes()
        after = content.index(project.START_MARKER) + len(project.START_MARKER)
        metadata_end = content.index(b"\n", after + 1) + 1
        path.write_bytes(content[:metadata_end] + LINKED + b"\n"
                         + content[metadata_end:])

    def attach_linked(self, runtime="codex"):
        self.guide()
        self.attach_full(runtime)
        self.inject_linked_marker(runtime)
        self.assertTrue(project.attach(self.repo, coordinator=runtime))
        return self.block(runtime)

    def block(self, runtime="codex"):
        content = self.target(runtime).read_bytes()
        begin = content.index(project.START_MARKER)
        end = content.index(project.END_MARKER) + len(project.END_MARKER)
        return content[begin:end]

    def hook(self, event, runtime="codex", **extra):
        payload = dict(session_id="overhead-session", turn_id="overhead-turn",
                       cwd=str(self.repo), hook_event_name=event)
        payload.update(extra)
        return coordinator_hooks.handle(payload, runtime=runtime)

    @staticmethod
    def context(result):
        return result["hookSpecificOutput"]["additionalContext"]

    def assert_contract(self, text, label):
        contract = overhead_contract.instructions()
        with self.subTest(surface=label):
            self.assertIn(contract.strip(), text)
            self.assertEqual(text.count(contract.strip()), 1)
            self.assertIn("checks_reusable", contract)
            self.assertIn("workspace verify --reuse-checks", contract)
            self.assertIn("--force-checks", contract)
            self.assertIn("--recheck-environment", contract)
            self.assertIn("Software-enforced guarantees", contract)
            self.assertIn("Agent recommendations", contract)

    @staticmethod
    def continuity_rule():
        contract = overhead_contract.instructions()
        start = contract.index(CONTINUITY_HEADING)
        rest = contract[start + len(CONTINUITY_HEADING):]
        ends = [index for index in (rest.find("\n### "), rest.find("\n## "))
                if index >= 0]
        if ends:
            rest = rest[:min(ends)]
        return (CONTINUITY_HEADING + rest).strip()

    def assert_continuity_rule(self, text, label):
        rule = self.continuity_rule()
        normalized = " ".join(rule.split())
        with self.subTest(surface=label):
            self.assertEqual(text.count(rule), 1)
            for token in CONTINUITY_TOKENS:
                self.assertIn(token, normalized)

    def assert_outside_untouched(self):
        self.assertEqual(self.tree(self.outside), self.decoy_before)


class RenderedContractTests(OverheadContractCase):
    def test_cli_init_delivers_contract_for_both_runtimes(self):
        self.guide()
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = cli.main(["init", "--coordinator", "both", str(self.repo)])
        self.assertEqual(code, 0, output.getvalue())
        for runtime in ("codex", "claude"):
            self.assert_contract(self.block(runtime).decode("utf-8"), "init/" + runtime)
        self.assert_outside_untouched()

    def test_fresh_full_attachment_carries_contract_for_both_runtimes(self):
        self.guide()
        guide_before = (self.repo / GUIDE_RELATIVE).read_bytes()
        self.assertTrue(self.attach_full("both"))
        for runtime in ("codex", "claude"):
            body = self.block(runtime).decode("utf-8")
            self.assertIn(PROFILE_HEADER.decode(), body)
            self.assert_contract(body, "full/" + runtime)
        self.assertEqual((self.repo / GUIDE_RELATIVE).read_bytes(), guide_before)
        self.assertEqual(stat.S_IMODE((self.repo / GUIDE_RELATIVE).stat().st_mode), 0o640)
        self.assert_outside_untouched()

    def test_fresh_linked_opt_in_stays_compact_and_carries_current_contract(self):
        body = self.attach_linked()
        text = body.decode("utf-8")
        self.assertIn(LINKED.decode(), text)
        self.assertIn("docs/agents/delegation.md", text)
        self.assertNotIn(PROFILE_HEADER.decode(), text)
        self.assert_contract(text, "linked")
        self.assertLessEqual(len(body), MAX_LINKED_BLOCK)
        self.assertEqual(stat.S_IMODE((self.repo / GUIDE_RELATIVE).stat().st_mode), 0o640)
        self.assert_outside_untouched()

    def test_live_policy_exposes_contract_once_for_both_runtimes(self):
        policy = settings.resolve(self.repo, delegation_level="auto", access="full-access")
        for runtime in ("codex", "claude"):
            self.assert_contract(settings.instructions(policy, runtime), "policy/" + runtime)

    def test_every_prompt_and_session_start_exposes_contract_without_rewriting(self):
        self.attach_full()
        with mock.patch.object(project, "_write_atomic",
                               side_effect=AssertionError("unchanged render wrote")) as write:
            session = self.context(self.hook("SessionStart", source="startup"))
            prompt = self.context(self.hook("UserPromptSubmit", prompt="status"))
        write.assert_not_called()
        self.assert_contract(session, "session-start")
        self.assert_contract(prompt, "prompt-unchanged")

    def test_fresh_init_full_and_linked_output_carries_continuity_rule_once(self):
        self.guide()
        guide_before = (self.repo / GUIDE_RELATIVE).read_bytes()
        guide_mode = stat.S_IMODE((self.repo / GUIDE_RELATIVE).stat().st_mode)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = cli.main(["init", "--coordinator", "both", str(self.repo)])
        self.assertEqual(code, 0, output.getvalue())
        for runtime in ("codex", "claude"):
            text = self.block(runtime).decode("utf-8")
            self.assert_continuity_rule(text, "full-init/" + runtime)
            self.assertNotIn("using the same still-unfinalized", text)
            self.assertNotIn("needs a new planned correction linked to the original",
                             text)

        for runtime in ("codex", "claude"):
            self.inject_linked_marker(runtime)
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = cli.main(["init", "--coordinator", "both", str(self.repo)])
        self.assertEqual(code, 0, output.getvalue())
        for runtime in ("codex", "claude"):
            block = self.block(runtime)
            self.assertIn(LINKED, block)
            self.assertNotIn(PROFILE_HEADER, block)
            self.assert_continuity_rule(block.decode("utf-8"), "linked-init/" + runtime)
            self.assertLessEqual(len(block), MAX_LINKED_BLOCK)

        self.assertEqual((self.repo / GUIDE_RELATIVE).read_bytes(), guide_before)
        self.assertEqual(stat.S_IMODE((self.repo / GUIDE_RELATIVE).stat().st_mode),
                         guide_mode)
        self.assert_outside_untouched()

    def test_compact_session_and_prompt_hooks_carry_continuity_rule_once(self):
        self.attach_full("both")
        for runtime in ("codex", "claude"):
            session = self.context(self.hook(
                "SessionStart", runtime=runtime, source="compact",
                session_id="continuity-session-" + runtime))
            prompt = self.context(self.hook(
                "UserPromptSubmit", runtime=runtime, prompt="status",
                session_id="continuity-session-" + runtime))
            self.assert_continuity_rule(session, "compact-session/" + runtime)
            self.assert_continuity_rule(prompt, "prompt/" + runtime)


class UpgradeAndPreservationTests(OverheadContractCase):
    def test_stale_upgrade_refresh_replaces_full_and_linked_with_current_rule_once(self):
        self.guide()
        stale = self.root / "stale-overhead-contract.md"
        stale.write_bytes(
            b"<!-- codex-deepseek-team:overhead-contract:v1 -->\n"
            b"# Versioned overhead contract (v1)\n\nSTALE-CONTRACT\n")
        with mock.patch.object(overhead_contract, "DATA_FILE", stale):
            self.attach_full("both")
            for runtime in ("codex", "claude"):
                self.inject_linked_marker(runtime)
            self.assertTrue(project.attach(self.repo, coordinator="both"))
            for runtime in ("codex", "claude"):
                self.assertIn(b"STALE-CONTRACT", self.block(runtime))

        guide_before = (self.repo / GUIDE_RELATIVE).read_bytes()
        guide_mode = stat.S_IMODE((self.repo / GUIDE_RELATIVE).stat().st_mode)
        with contextlib.chdir(self.repo):
            self.assertTrue(onboarding._refresh_current_repository())

        for runtime in ("codex", "claude"):
            block = self.block(runtime)
            text = block.decode("utf-8")
            self.assertNotIn("STALE-CONTRACT", text)
            self.assert_continuity_rule(text, "stale-upgrade/" + runtime)
            self.assertIn(LINKED, block)
            self.assertNotIn(PROFILE_HEADER, block)
            self.assertLessEqual(len(block), MAX_LINKED_BLOCK)
        self.assertEqual((self.repo / GUIDE_RELATIVE).read_bytes(), guide_before)
        self.assertEqual(stat.S_IMODE((self.repo / GUIDE_RELATIVE).stat().st_mode),
                         guide_mode)
        self.assert_outside_untouched()

    def test_session_bootstrap_refreshes_upgraded_contract_in_full_and_linked_blocks(self):
        self.guide()
        self.attach_full("both")
        self.inject_linked_marker("claude")
        self.assertTrue(project.attach(self.repo, coordinator="claude"))
        prefix = b"# prefix\r\nkeep\r\n"
        suffix = b"\r\n## suffix\r\nkeep\r\n"
        for runtime in ("codex", "claude"):
            path = self.target(runtime)
            path.write_bytes(prefix + path.read_bytes() + suffix)
            path.chmod(0o600)
        guide_before = (self.repo / GUIDE_RELATIVE).read_bytes()
        guide_mode = stat.S_IMODE((self.repo / GUIDE_RELATIVE).stat().st_mode)

        upgraded = overhead_contract.DATA_FILE.read_bytes() + b"\nUPGRADED-CONTRACT-MARKER\n"
        replacement = self.root / "overhead-contract.md"
        replacement.write_bytes(upgraded)
        with mock.patch.object(overhead_contract, "DATA_FILE", replacement):
            self.hook("SessionStart", source="resume")

        for runtime in ("codex", "claude"):
            body = self.target(runtime).read_bytes()
            with self.subTest(runtime=runtime):
                self.assertIn(b"UPGRADED-CONTRACT-MARKER", body)
                self.assertTrue(body.startswith(prefix))
                self.assertTrue(body.endswith(suffix))
                self.assertEqual(stat.S_IMODE(self.target(runtime).stat().st_mode), 0o600)
        self.assertIn(LINKED, self.block("claude"))
        self.assertNotIn(PROFILE_HEADER, self.block("claude"))
        self.assertEqual((self.repo / GUIDE_RELATIVE).read_bytes(), guide_before)
        self.assertEqual(stat.S_IMODE((self.repo / GUIDE_RELATIVE).stat().st_mode), guide_mode)
        self.assert_outside_untouched()

    def test_setup_refresh_uses_upgraded_package_source_and_is_a_no_op_when_unchanged(self):
        self.attach_full("both")
        replacement = self.root / "overhead-contract.md"
        replacement.write_bytes(
            overhead_contract.DATA_FILE.read_bytes() + b"\nSETUP-UPGRADE-MARKER\n")
        with mock.patch.object(overhead_contract, "DATA_FILE", replacement), \
             contextlib.chdir(self.repo):
            before = {runtime: self.target(runtime).read_bytes()
                      for runtime in ("codex", "claude")}
            self.assertTrue(onboarding._refresh_current_repository())
            for runtime in ("codex", "claude"):
                body = self.target(runtime).read_bytes()
                self.assertIn(b"SETUP-UPGRADE-MARKER", body)
                self.assertNotEqual(body, before[runtime])
            unchanged = {runtime: self.target(runtime).read_bytes()
                         for runtime in ("codex", "claude")}
            with mock.patch.object(project, "_write_atomic",
                                   side_effect=AssertionError("unchanged setup wrote")) as write:
                self.assertTrue(onboarding._refresh_current_repository())
            write.assert_not_called()
            for runtime in ("codex", "claude"):
                self.assertEqual(self.target(runtime).read_bytes(), unchanged[runtime])
        self.assert_outside_untouched()


class ResourceAndDocumentationTests(unittest.TestCase):
    def test_checker_requires_full_linked_and_contract_resources(self):
        checker = load_checker()
        for resource in (
                "codex_deepseek_team/data/delegation.md",
                "codex_deepseek_team/data/delegation-linked.md",
                "codex_deepseek_team/data/overhead-contract.md"):
            with self.subTest(resource=resource):
                self.assertIn(resource, checker.RESOURCES)

    def _load_replacement(self, data):
        directory = tempfile.TemporaryDirectory(prefix="dsteam-contract-")
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "overhead-contract.md"
        path.write_bytes(data)
        return path

    def test_missing_contract_resource_is_reported(self):
        directory = tempfile.TemporaryDirectory(prefix="dsteam-contract-")
        self.addCleanup(directory.cleanup)
        missing = Path(directory.name) / "missing.md"
        with mock.patch.object(overhead_contract, "DATA_FILE", missing), \
             self.assertRaisesRegex(overhead_contract.OverheadContractError,
                                    "overhead contract is unavailable"):
            overhead_contract.instructions()

    def test_unsupported_contract_version_is_rejected(self):
        replacement = self._load_replacement(
            b"<!-- codex-deepseek-team:overhead-contract:v2 -->\n# future\n")
        with mock.patch.object(overhead_contract, "DATA_FILE", replacement), \
             self.assertRaisesRegex(overhead_contract.OverheadContractError,
                                    "missing or unsupported version marker"):
            overhead_contract.instructions()

    def test_malformed_contract_version_marker_is_rejected(self):
        replacement = self._load_replacement(
            b"# Versioned overhead contract (v1)\nNo machine marker.\n")
        with mock.patch.object(overhead_contract, "DATA_FILE", replacement), \
             self.assertRaisesRegex(overhead_contract.OverheadContractError,
                                    "missing or unsupported version marker"):
            overhead_contract.instructions()

    def test_non_utf8_contract_resource_is_rejected(self):
        replacement = self._load_replacement(
            b"<!-- codex-deepseek-team:overhead-contract:v1 -->\n\xff\n")
        with mock.patch.object(overhead_contract, "DATA_FILE", replacement), \
             self.assertRaisesRegex(overhead_contract.OverheadContractError,
                                    "not UTF-8"):
            overhead_contract.instructions()


if __name__ == "__main__":
    unittest.main()
