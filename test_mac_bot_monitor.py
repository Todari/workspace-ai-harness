import contextlib
import datetime as dt
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import mac_bot_monitor as monitor


class MacBotMonitorTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state = Path(directory.name) / 'monitor.json'
        self.db = self.state.with_suffix('.sqlite3')

    def run_probe(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return monitor.run('https://bot.test/healthz', 'https://bot.test/webhook/mac-monitor',
                               'test-secret', 'fixture-discord', self.state, self.db)

    def test_unsent_failure_and_recovery_are_retried_in_order(self):
        probe = [(True, 200), (False, 503), (True, 200), (True, 200)]
        with mock.patch.object(monitor, 'probe', side_effect=probe), \
                mock.patch.object(monitor, 'github_last_started', return_value=None), \
                mock.patch.object(monitor, 'heartbeat', side_effect=[True, False, True, True]) as heartbeat, \
                mock.patch.object(monitor.ops_report, 'deliver',
                                  side_effect=['failed', 'sent', 'uncertain', 'sent']) as notify:
            self.assertEqual(self.run_probe(), 0)
            notify.assert_not_called()
            self.assertEqual(self.run_probe(), 1)
            self.assertEqual(self.run_probe(), 1)
            self.assertEqual(self.run_probe(), 0)

        failed, retry_failure, recovered, retry_recovery = notify.call_args_list
        self.assertIn('응답 이상', failed.args[2])
        self.assertIn('HTTP 503', failed.args[2])
        self.assertEqual(failed, retry_failure)
        self.assertIn('복구', recovered.args[2])
        self.assertEqual(recovered, retry_recovery)
        self.assertTrue(failed.kwargs['retry_failed'])
        self.assertTrue(failed.kwargs['retry_uncertain'])
        self.assertEqual(heartbeat.call_count, 4)
        self.assertEqual(json.loads(self.state.read_text())['alerts'], [])

    def test_github_schedule_notifies_once_per_delay_and_recovery(self):
        now = dt.datetime.now(dt.timezone.utc)
        old = now - dt.timedelta(hours=10)
        recent = now - dt.timedelta(minutes=2)
        with mock.patch.object(monitor, 'probe', return_value=(True, 200)), \
                mock.patch.object(monitor, 'github_last_started', side_effect=[None, old, old, None, recent]), \
                mock.patch.object(monitor, 'heartbeat', return_value=True) as heartbeat, \
                mock.patch.object(monitor.ops_report, 'deliver', return_value='sent') as notify:
            self.assertEqual([self.run_probe() for _ in range(5)], [0, 1, 1, 0, 0])
        self.assertEqual(notify.call_count, 2)
        self.assertIn('예약 실행 지연', notify.call_args_list[0].args[2])
        self.assertIn('일정 복구', notify.call_args_list[1].args[2])
        self.assertEqual([call.args[-1] for call in heartbeat.call_args_list],
                         [None, monitor.stamp(old), monitor.stamp(old), None, monitor.stamp(recent)])

    def test_github_uses_authenticated_cli_when_available_and_reports_missing_as_unknown(self):
        now = dt.datetime(2026, 1, 1, 12, tzinfo=dt.timezone.utc)
        output = json.dumps([
            {'startedAt': '2026-01-01T11:50:00Z', 'status': 'queued'},
            {'startedAt': '2026-01-01T11:30:00Z', 'status': 'completed'},
        ])
        with mock.patch.object(monitor.subprocess, 'run', return_value=mock.Mock(stdout=output)) as run:
            self.assertEqual(monitor.github_last_started(now), now - dt.timedelta(minutes=30))
        self.assertIn('--workflow', run.call_args.args[0])
        self.assertIn('schedule', run.call_args.args[0])
        with mock.patch.object(monitor.subprocess, 'run', side_effect=FileNotFoundError), \
                mock.patch.object(monitor.urllib.request, 'build_opener') as opener:
            request = opener.return_value.open
            request.return_value.__enter__.return_value.read.return_value = json.dumps({
                'workflow_runs': [{'status': 'completed', 'run_started_at': '2026-01-01T11:00:00Z'}]}).encode()
            self.assertEqual(monitor.github_last_started(now), now - dt.timedelta(hours=1))
            self.assertFalse(request.call_args.args[0].has_header('Authorization'))
            request.side_effect = OSError
            self.assertIsNone(monitor.github_last_started(now))

    def test_get_validates_response_shape(self):
        opener = mock.MagicMock()
        response = opener.open.return_value.__enter__.return_value
        response.status = 200
        with mock.patch.object(monitor.urllib.request, 'build_opener', return_value=opener):
            response.read.return_value = b'{"ok":true}'
            self.assertEqual(monitor.probe('https://bot.test/healthz'), (True, 200))
            response.read.return_value = b'{"ok":"true"}'
            self.assertEqual(monitor.probe('https://bot.test/healthz'), (False, 200))
            response.read.return_value = b'not JSON'
            self.assertEqual(monitor.probe('https://bot.test/healthz'), (False, None))

    def test_endpoints_share_validated_origin(self):
        self.assertEqual(monitor.endpoints('https://bot.test/webhook/vault-sync'),
                         ('https://bot.test/healthz', 'https://bot.test/webhook/mac-monitor'))
        with self.assertRaises(ValueError):
            monitor.endpoints('http://bot.test/webhook/vault-sync')


if __name__ == '__main__':
    unittest.main()
