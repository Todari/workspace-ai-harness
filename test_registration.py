import copy
import os
import unittest

import register_codex
import register_hooks
import sync_context


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

    def test_codex_registers_the_expected_scripts(self):
        """개수가 아니라 어떤 스크립트가 걸리는지를 고정한다."""
        for script in ("repo_map.py", "obsidian_bridge.py", "verify_gate.py"):
            self.assertIn(script, register_codex.BLOCK, script)
        self.assertNotIn("obsidian_scheduler.py", register_codex.BLOCK)
        # 모든 훅에 리비전이 붙어 코드가 바뀌면 Codex가 재승인을 요구한다
        self.assertEqual(register_codex.BLOCK.count("--harness-rev="),
                         register_codex.BLOCK.count('type = "command"'))

    def test_codex_has_no_async_hooks(self):
        """Codex 0.144는 async 훅을 건너뛴다 — 등록하면 조용히 실행되지 않는다."""
        self.assertNotIn("async = true", register_codex.BLOCK)

    def test_subagent_hook_stays_claude_only(self):
        """Codex에는 subagent_start 이벤트가 없다."""
        self.assertNotIn("vault_subagent.py", register_codex.BLOCK)

    def test_commented_fallback_setting_does_not_count(self):
        self.assertFalse(register_codex.has_project_doc_fallback(
            '# project_doc_fallback_filenames = ["CLAUDE.md"]\n'))
        self.assertTrue(register_codex.has_project_doc_fallback(
            'project_doc_fallback_filenames = ["CLAUDE.md"]\n'))


class ClaudeRegistrationTest(unittest.TestCase):
    def fixture(self):
        harness = register_hooks.HARNESS
        return {
            "enabledPlugins": {
                "frontend-design@claude-plugins-official": True,
                register_hooks.INCOMPATIBLE_PLUGIN: True,
            },
            "hooks": {
                "SessionStart": [{
                    "hooks": [
                        {"type": "command", "command":
                         "python3 %s/repo_map.py" % harness},
                        {"type": "command", "command":
                         "python3 %s/obsidian_bridge.py" % harness},
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

    def test_preserves_unrelated_handler_in_mixed_group(self):
        settings = self.fixture()
        register_hooks.update_settings(settings)
        serialized = str(settings)
        self.assertIn("obsidian_bridge.py", serialized)
        self.assertNotIn("bash_gate.py", serialized)
        self.assertNotIn("obsidian_scheduler.py", serialized)
        self.assertEqual(serialized.count("repo_map.py"), 1)

    def test_update_is_idempotent(self):
        settings = self.fixture()
        register_hooks.update_settings(settings)
        once = copy.deepcopy(settings)
        register_hooks.update_settings(settings)
        self.assertEqual(settings, once)

    def test_disables_conflicting_plugin_and_preserves_others(self):
        settings = self.fixture()
        register_hooks.update_settings(settings)
        self.assertFalse(settings["enabledPlugins"][register_hooks.INCOMPATIBLE_PLUGIN])
        self.assertTrue(
            settings["enabledPlugins"]["frontend-design@claude-plugins-official"])


class ContextSyncTest(unittest.TestCase):
    def test_source_is_tracked_harness_context(self):
        self.assertIn(sync_context.SOURCE, (sync_context.LOCAL_SOURCE, sync_context.EXAMPLE_SOURCE))

    def test_sync_targets_are_workspace_local(self):
        self.assertEqual(sync_context.TARGETS, (
            os.path.join(sync_context.lib.WORKSPACE_ROOT, "CLAUDE.md"),
            os.path.join(sync_context.lib.WORKSPACE_ROOT, "AGENTS.md"),
        ))

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


if __name__ == "__main__":
    unittest.main()
