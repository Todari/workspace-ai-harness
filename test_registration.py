import copy
import os
import tempfile
import unittest

import claude_auto_route
import claude_handoff_gate
import codex_worker
import hooks_manifest as manifest
import register_codex
import register_hooks
import register_orchestration
import sync_context


class ManifestTest(unittest.TestCase):
    def test_every_script_exists(self):
        for spec in manifest.HOOKS:
            self.assertTrue(os.path.exists(manifest.script_path(spec["script"])), spec["script"])

    def test_retired_scripts_are_still_owned(self):
        """재등록 때 낡은 항목이 정리되려면 은퇴한 스크립트도 소유 목록에 있어야 한다."""
        for script in ("plan_gate.py", "bash_gate.py", "quick_bypass.py"):
            self.assertTrue(manifest.is_owned_command("python3 /x/%s" % script))
        self.assertTrue(manifest.is_owned_command("python3 /x/repo_map.py --harness-rev=abc"))
        self.assertFalse(manifest.is_owned_command("python3 /x/other_hook.py"))

    def test_auto_router_is_claude_only(self):
        claude_scripts = {spec["script"] for spec in manifest.claude_hooks()}
        codex_scripts = {spec["script"] for spec in manifest.codex_hooks()}
        self.assertIn("claude_auto_route.py", claude_scripts)
        self.assertIn("claude_handoff_gate.py", claude_scripts)
        self.assertNotIn("claude_auto_route.py", codex_scripts)
        self.assertNotIn("claude_handoff_gate.py", codex_scripts)


class CodexRegistrationTest(unittest.TestCase):
    def test_remove_managed_block_preserves_other_config(self):
        text = "model = \"x\"\n\n%s\n[hooks]\n%s\n\n[projects.x]\ntrust_level = \"trusted\"\n" % (
            register_codex.BEGIN, register_codex.END)
        cleaned = register_codex.remove_managed_block(text)
        self.assertIn('model = "x"', cleaned)
        self.assertIn("[projects.x]", cleaned)
        self.assertNotIn(register_codex.BEGIN, cleaned)

    def test_extracts_existing_trust_state(self):
        text = "%s\n[hooks]\n[hooks.state]\n\"k\" = { trusted_hash = \"h\" }\n%s\n" % (
            register_codex.BEGIN, register_codex.END)
        state = register_codex.existing_state_section(text)
        self.assertIn("[hooks.state]", state)
        self.assertIn("trusted_hash", state)

    def test_codex_matcher_uses_canonical_tool_names(self):
        self.assertIn('matcher = "^(Bash|apply_patch|Edit|Write)$"',
                      register_codex.BLOCK)
        self.assertNotIn("exec_command|shell|unified_exec", register_codex.BLOCK)

    def test_codex_block_follows_manifest(self):
        for spec in manifest.codex_hooks():
            self.assertIn("%s = [" % spec["event"], register_codex.BLOCK)
            self.assertIn(manifest.codex_command(spec["script"]), register_codex.BLOCK)
        codex_scripts = {spec["script"] for spec in manifest.codex_hooks()}
        for spec in manifest.HOOKS:
            if spec["script"] in codex_scripts:
                continue
            self.assertNotIn(manifest.codex_command(spec["script"]), register_codex.BLOCK)
        for script in manifest.RETIRED_SCRIPTS:
            self.assertNotIn(script, register_codex.BLOCK)
        # 모든 훅에 리비전이 붙어 코드가 바뀌면 Codex가 재승인을 요구한다
        self.assertEqual(register_codex.BLOCK.count("--harness-rev="),
                         register_codex.BLOCK.count('type = "command"'))

    def test_codex_gets_subagent_hook(self):
        """Codex 0.151+는 SubagentStart를 지원한다."""
        self.assertIn("SubagentStart = [", register_codex.BLOCK)
        self.assertIn("vault_subagent.py", register_codex.BLOCK)

    def test_commented_fallback_setting_does_not_count(self):
        self.assertFalse(register_codex.has_project_doc_fallback(
            '# project_doc_fallback_filenames = ["CLAUDE.md"]\n'))
        self.assertTrue(register_codex.has_project_doc_fallback(
            'project_doc_fallback_filenames = ["CLAUDE.md"]\n'))


class ClaudeRegistrationTest(unittest.TestCase):
    def fixture(self):
        harness = register_hooks.HARNESS
        return {
            "enabledPlugins": {"frontend-design@claude-plugins-official": True},
            "hooks": {
                "SessionStart": [{
                    "hooks": [
                        {"type": "command", "command":
                         "python3 %s/repo_map.py" % harness},
                        {"type": "command", "command":
                         "python3 /elsewhere/other_hook.py"},
                        {"type": "command", "command":
                         "python3 %s/obsidian_scheduler.py" % harness},
                    ]
                }],
                "PreToolUse": [{
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command":
                               "python3 %s/bash_gate.py" % harness}],
                }],
            }
        }

    def test_preserves_unrelated_handler_and_removes_retired(self):
        settings = self.fixture()
        register_hooks.update_settings(settings)
        serialized = str(settings)
        self.assertIn("other_hook.py", serialized)
        self.assertNotIn("bash_gate.py", serialized)
        self.assertNotIn("obsidian_scheduler.py", serialized)
        self.assertIn("claude_handoff_gate.py", serialized)
        self.assertEqual(serialized.count("repo_map.py"), 1)

    def test_installs_every_manifest_entry(self):
        settings = self.fixture()
        register_hooks.update_settings(settings)
        for spec in manifest.HOOKS:
            entries = settings["hooks"][spec["event"]]
            commands = [(e.get("matcher"), h["command"]) for e in entries for h in e["hooks"]]
            self.assertIn((spec.get("claude_matcher"), manifest.claude_command(spec["script"])),
                          commands)

    def test_update_is_idempotent(self):
        settings = self.fixture()
        register_hooks.update_settings(settings)
        once = copy.deepcopy(settings)
        register_hooks.update_settings(settings)
        self.assertEqual(settings, once)

    def test_does_not_touch_plugins(self):
        settings = self.fixture()
        register_hooks.update_settings(settings)
        self.assertEqual(settings["enabledPlugins"],
                         {"frontend-design@claude-plugins-official": True})


class AutoRouteContextTest(unittest.TestCase):
    def test_hard_context_uses_tracked_model_and_requires_no_command(self):
        import json as _json
        config = _json.loads(_json.dumps(codex_worker.load_config()))
        config["routing"]["enforcement"] = "hard"
        context = claude_auto_route.routing_context(config)
        self.assertIn("hard Codex handoff v2", context)
        self.assertIn("gpt-5.6-sol", context)
        self.assertIn("직접 해줘", context)
        self.assertIn("Never delegate again", context)

    def test_advisory_context_is_short_and_non_blocking(self):
        import json as _json
        config = _json.loads(_json.dumps(codex_worker.load_config()))
        config["routing"]["enforcement"] = "advisory"
        context = claude_auto_route.routing_context(config)
        self.assertIn("advisory", context)
        self.assertIn("--detach", context)
        self.assertNotIn("Do not poll", context)
        self.assertLess(len(context), 900)


class OrchestrationRegistrationTest(unittest.TestCase):
    def test_updates_actual_fable_effort_without_touching_other_models(self):
        settings = {
            "model": "claude-fable-5-1[1m]",
            "effortLevel": "xhigh",
            "modelSettings": {"claude-sonnet-5": {"effortLevel": "high"}},
        }
        changed = register_orchestration.update_planner_effort(
            settings, codex_worker.load_config())
        self.assertTrue(changed)
        self.assertEqual(settings["effortLevel"], "medium")
        self.assertEqual(settings["modelSettings"]["claude-fable-5-1"]["effortLevel"],
                         "medium")
        self.assertEqual(settings["modelSettings"]["claude-fable-5-1[1m]"]["effortLevel"],
                         "medium")
        self.assertEqual(settings["modelSettings"]["claude-sonnet-5"]["effortLevel"],
                         "high")

    def test_syncs_managed_assets_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as target:
            source_path = os.path.join(source, "asset.md")
            target_path = os.path.join(target, "nested", "asset.md")
            with open(source_path, "w") as f:
                f.write(register_orchestration.MANAGED_MARKER + "\ncontent\n")
            self.assertEqual(register_orchestration.sync_asset(source_path, target_path), "created")
            self.assertEqual(register_orchestration.sync_asset(source_path, target_path), "unchanged")
            self.assertTrue(register_orchestration.is_synced(source_path, target_path))

    def test_preserves_unmanaged_existing_asset(self):
        with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as target:
            source_path = os.path.join(source, "asset.md")
            target_path = os.path.join(target, "asset.md")
            with open(source_path, "w") as f:
                f.write(register_orchestration.MANAGED_MARKER + "\nnew\n")
            with open(target_path, "w") as f:
                f.write("personal\n")
            with self.assertRaises(ValueError):
                register_orchestration.sync_asset(source_path, target_path)


class ContextSyncTest(unittest.TestCase):
    def test_source_is_tracked_harness_context(self):
        self.assertIn(sync_context.SOURCE, (sync_context.LOCAL_SOURCE, sync_context.EXAMPLE_SOURCE))

    def test_sync_targets_include_workspace_and_codex_global(self):
        self.assertEqual(sync_context.TARGETS[:2], (
            os.path.join(sync_context.lib.WORKSPACE_ROOT, "CLAUDE.md"),
            os.path.join(sync_context.lib.WORKSPACE_ROOT, "AGENTS.md"),
        ))
        if os.path.isdir(sync_context.CODEX_HOME):
            self.assertEqual(sync_context.TARGETS[2], sync_context.CODEX_GLOBAL)

    def test_first_sync_wraps_managed_block(self):
        updated = sync_context.replace_managed_block("", "# rules\n")
        self.assertIn(sync_context.BEGIN, updated)
        self.assertIn("# rules", updated)

    def test_resync_preserves_unmanaged_content(self):
        current = "# personal\n\n%s\nold\n%s\n" % (sync_context.BEGIN, sync_context.END)
        updated = sync_context.replace_managed_block(current, "# new\n")
        self.assertIn("# personal", updated)
        self.assertIn("# new", updated)
        self.assertNotIn("\nold\n", updated)

    def test_legacy_marker_is_migrated(self):
        legacy = "<!-- BEGIN workspace-harness: synced from /Users/example/workspace/CLAUDE.md -->"
        current = "%s\nold\n%s\n" % (legacy, sync_context.END)
        updated = sync_context.replace_managed_block(current, "# new\n")
        self.assertIn(sync_context.BEGIN, updated)
        self.assertNotIn(legacy, updated)
        self.assertIn("# new", updated)

    def test_unbalanced_managed_block_fails(self):
        with self.assertRaises(ValueError):
            sync_context.replace_managed_block(
                "%s\nold\n" % sync_context.BEGIN, "# new\n")

    def test_repo_table_placeholder_is_rendered(self):
        rendered = sync_context.render_source("a\n%s\nb\n" % sync_context.REPO_TABLE_PLACEHOLDER)
        self.assertNotIn(sync_context.REPO_TABLE_PLACEHOLDER, rendered)
        self.assertTrue(rendered.startswith("a\n"))
        self.assertTrue(rendered.endswith("\nb\n"))


if __name__ == "__main__":
    unittest.main()
