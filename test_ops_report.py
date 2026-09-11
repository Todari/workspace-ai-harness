import fcntl
import json
import signal
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock
import urllib.error

import ops_report as ops


class OperationsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'ops.sqlite'

    def test_receipt_requires_durable_marker_and_survives_later_note_edits(self):
        root = Path(self.tmp.name) / 'vault'
        root.mkdir()
        note = root / 'note.md'
        note.write_text('task')
        with self.assertRaises(ValueError):
            ops.record_receipt('123', note='note.md', db=self.db, vault=root)
        note.write_text('task <!-- inbox:discord:123 -->')
        written = ops.record_receipt('123', note='note.md', db=self.db, vault=root)
        note.write_text('later edit')
        self.assertEqual(ops.receipt('123', self.db), written)
        self.assertEqual(written['disposition'], 'written')
        with self.assertRaises(ValueError):
            ops.record_receipt('234', note='../outside.md', db=self.db, vault=root)

    def test_real_failed_exit_preserves_last_success(self):
        self.assertEqual(ops.run_job('weekly-review', [sys.executable, '-c', 'pass'], db=self.db), 0)
        success = next(r for r in ops.job_health(self.db) if r['job'] == 'weekly-review')['last_success']
        self.assertEqual(ops.run_job('weekly-review', [sys.executable, '-c', 'exit(7)'], db=self.db), 7)
        row = next(r for r in ops.job_health(self.db) if r['job'] == 'weekly-review')
        self.assertEqual((row['status'], row['exit_code'], row['last_success']), ('failed', 7, success))
        self.assertGreaterEqual(row['last_attempt'], success)

    def test_report_exposes_sync_failure_without_claiming_note_write_failed(self):
        report = {'generated_at': 'now', 'jobs': [
            {'job': 'vault-push', 'status': 'failed', 'exit_code': 7,
             'last_attempt': 100, 'last_success': 50}],
            'metrics': {'days': 1, 'codex_workers': {'runs': 0, 'statuses': {}, 'usage': {}}}}
        text = ops.markdown(report)
        self.assertIn('vault-push | failed | 7 |', text)
        self.assertIn('최근 실행(UTC)', text)
        self.assertIn('vault_sync.py --dry-run', text)
        self.assertIn('별개입니다', text)

    def test_missing_history_is_unknown_and_old_success_stale(self):
        self.assertTrue(all(r['status'] == 'unknown' for r in ops.job_health(self.db)))
        ops.run_job('inbox-sweep', [sys.executable, '-c', 'pass'], db=self.db)
        row = next(r for r in ops.job_health(self.db, time.time() + 40*3600) if r['job'] == 'inbox-sweep')
        self.assertEqual(row['status'], 'stale')

    def test_running_job_is_not_started_twice_even_with_shorter_timeout(self):
        lockpath = self.db.parent / '.job-inbox-sweep.lock'
        with lockpath.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with mock.patch.object(ops.subprocess, 'Popen') as spawn:
                self.assertEqual(ops.run_job('inbox-sweep', ['unused'], timeout=1, db=self.db), 75)
                spawn.assert_not_called()

    def test_timeout_kills_group_even_if_leader_has_exited(self):
        proc = mock.Mock(pid=12345)
        with mock.patch.object(ops.os, 'killpg') as kill:
            ops.stop_process_group(proc, grace=0)
        self.assertEqual(kill.call_args_list, [mock.call(12345, signal.SIGTERM), mock.call(12345, signal.SIGKILL)])


    def test_timeout_is_failure(self):
        self.assertEqual(ops.run_job('inbox-sweep', [sys.executable, '-c', 'import time; time.sleep(5)'], timeout=.02, db=self.db), 124)

    def test_delivery_deduplicates_and_does_not_store_webhook(self):
        opener = mock.MagicMock()
        opener.open.return_value.__enter__.return_value.status = 200
        url = 'https://discord.com/api/webhooks/123/SECRET'
        with mock.patch.object(ops.urllib.request, 'build_opener', return_value=opener):
            self.assertEqual(ops.deliver('discord', url, 'summary', 'daily:1', db=self.db), 'sent')
            self.assertEqual(ops.deliver('discord', url, 'changed summary', 'daily:1', db=self.db), 'already-sent')
        self.assertEqual(opener.open.call_count, 1)
        self.assertNotIn(b'SECRET', self.db.read_bytes())
        req = opener.open.call_args.args[0]
        self.assertIn('wait=true', req.full_url)
        self.assertEqual(json.loads(req.data)['allowed_mentions'], {'parse': []})

    def test_ambiguous_delivery_is_not_retried(self):
        opener = mock.MagicMock()
        opener.open.side_effect = TimeoutError('SECRET')
        with mock.patch.object(ops.urllib.request, 'build_opener', return_value=opener):
            url = 'https://hooks.slack.com/services/SECRET'
            self.assertEqual(ops.deliver('slack', url, 'summary', 'one', db=self.db), 'uncertain')
            self.assertEqual(ops.deliver('slack', url, 'summary', 'one', db=self.db, retry_failed=True), 'already-uncertain')
        self.assertEqual(opener.open.call_count, 1)
        self.assertNotIn(b'SECRET', self.db.read_bytes())

    def test_preview_never_sends(self):
        report = {'generated_at': 'now', 'jobs': [], 'metrics': {'days': 1, 'codex_workers': {'runs': 0, 'statuses': {}, 'usage': {}}}}
        with mock.patch.object(ops, 'build_report', return_value=report), mock.patch.object(ops, 'deliver') as send:
            with mock.patch('sys.stdout'):
                self.assertEqual(ops.main(['report']), 0)
            send.assert_not_called()

    def test_rejects_wrong_webhook_host(self):
        with self.assertRaises(ValueError):
            ops.deliver('slack', 'https://example.com/services/SECRET', 'text', 'key', db=self.db)
        self.assertFalse(self.db.exists())


if __name__ == '__main__':
    unittest.main()
