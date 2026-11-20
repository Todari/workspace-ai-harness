#!/usr/bin/env python3
"""Mac mini external health probe. Notify Discord on transitions and publish probe freshness."""

import datetime as dt
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

import harness_lib as lib
import ops_report
from vault_sync import load_ops_env

STATE = Path(lib.CACHE_DIR) / 'mac-bot-monitor.json'
TIMEOUT = 10
GITHUB_MAX_AGE = dt.timedelta(minutes=90)


def endpoints(sync_url):
    url = urllib.parse.urlsplit(sync_url)
    if (url.scheme != 'https' or not url.hostname or url.username or url.password
            or url.port not in (None, 443) or url.path != '/webhook/vault-sync'
            or url.query or url.fragment):
        raise ValueError('invalid vault sync URL')
    return tuple(urllib.parse.urlunsplit(url._replace(path=path))
                 for path in ('/healthz', '/webhook/mac-monitor'))


def probe(url):
    try:
        with urllib.request.build_opener(ops_report.NoRedirect()).open(url, timeout=TIMEOUT) as response:
            body = json.loads(response.read(65536))
            return response.status == 200 and isinstance(body, dict) and body.get('ok') is True, response.status
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        return False, code
    except (OSError, ValueError):
        return False, None


def github_last_started(now):
    try:
        result = subprocess.run(
            ['gh', 'run', 'list', '--repo', 'Todari/todari-ops', '--workflow', 'monitor.yml',
             '--event', 'schedule', '--limit', '5', '--json', 'startedAt,status'],
            check=True, timeout=TIMEOUT, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env={**os.environ, 'GH_PROMPT_DISABLED': '1'})
        runs = json.loads(result.stdout)
        starts = []
        for row in runs:
            if row['status'] in ('completed', 'in_progress') and row['startedAt']:
                started = dt.datetime.fromisoformat(row['startedAt'].replace('Z', '+00:00'))
                if started.tzinfo and started <= now:
                    starts.append(started.astimezone(dt.timezone.utc))
        return max(starts, default=None)
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError, AttributeError):
        return None


def stamp(moment):
    return moment.isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def heartbeat(url, secret, checked_at, healthy, github_started_at):
    body = json.dumps({'checkedAt': checked_at, 'healthy': healthy, 'githubLastStartedAt': github_started_at}).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    request = urllib.request.Request(url, data=body, method='POST', headers={
        'Content-Type': 'application/json', 'x-vault-signature': signature})
    try:
        with urllib.request.build_opener(ops_report.NoRedirect()).open(request, timeout=TIMEOUT) as response:
            return response.status == 200
    except urllib.error.HTTPError as exc:
        exc.close()
        return False
    except OSError:
        return False


def run(health_url, heartbeat_url, secret, discord_url, state_path=STATE, db=ops_report.DB):
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with state_path.with_suffix('.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 75
        try:
            state = json.loads(state_path.read_text())
        except FileNotFoundError:
            state = {'healthy': None, 'alerts': []}

        healthy, code = probe(health_url)
        now = dt.datetime.now(dt.timezone.utc)
        checked_at = stamp(now)
        if (state['healthy'] is False and healthy) or (state['healthy'] is not False and not healthy):
            state['alerts'].append({'checkedAt': checked_at, 'healthy': healthy, 'code': code})
        github_started = github_last_started(now)
        github_started_at = stamp(github_started) if github_started else None
        github_status = 'unknown'
        if github_started:
            github_status = 'stale' if now - github_started > GITHUB_MAX_AGE else 'fresh'
            previous = state.get('githubStatus')
            if (github_status == 'stale' and previous != 'stale') or (github_status == 'fresh' and previous == 'stale'):
                state['alerts'].append({'kind': 'github', 'checkedAt': checked_at, 'healthy': github_status == 'fresh',
                                        'startedAt': github_started_at})
            state['githubStatus'] = github_status
        state.update(healthy=healthy, checkedAt=checked_at, githubLastStartedAt=github_started_at)
        ops_report.atomic_write(state_path, json.dumps(state))
        reported = heartbeat(heartbeat_url, secret, checked_at, healthy, github_started_at)

        while state['alerts']:
            alert = state['alerts'][0]
            kind = alert.get('kind', 'healthz')
            if kind == 'github':
                result = '🟢 일정 복구' if alert['healthy'] else '🟡 예약 실행 지연'
                detail = f'GitHub monitor · 마지막 시작 {alert["startedAt"]}'
            else:
                result = '🟢 복구' if alert['healthy'] else '🔴 응답 이상'
                code = f'HTTP {alert["code"]}' if alert['code'] is not None else '응답 없음 또는 형식 오류'
                detail = f'todari-ops /healthz · {code}'
            text = f'{result} · {detail} · Mac mini 관측 {alert["checkedAt"]}'
            status = ops_report.deliver(
                'discord', discord_url, text, f'mac-bot-monitor:{kind}:{alert["checkedAt"]}:{alert["healthy"]}',
                db=db, retry_failed=True, retry_uncertain=True)
            if status not in ('sent', 'already-sent'):
                break
            del state['alerts'][0]
            ops_report.atomic_write(state_path, json.dumps(state))

        print(f'mac-bot-monitor probe={"healthy" if healthy else "failed"} '
              f'github={github_status} heartbeat={"sent" if reported else "failed"} pending={len(state["alerts"])}')
        return 0 if healthy and reported and github_status != 'stale' and not state['alerts'] else 1


def main():
    try:
        config = load_ops_env()
        health_url, heartbeat_url = endpoints(config.get('VAULT_SYNC_URL', ''))
        secret = config.get('VAULT_SYNC_SECRET', '')
        discord_url = os.environ.get('HARNESS_DISCORD_WEBHOOK_URL') or config.get('HARNESS_DISCORD_WEBHOOK_URL', '')
        if not secret or not discord_url:
            raise ValueError('missing monitor configuration')
        ops_report.validate_webhook('discord', discord_url)
        return run(health_url, heartbeat_url, secret, discord_url)
    except (OSError, ValueError) as exc:
        print(f'mac-bot-monitor failed ({type(exc).__name__})', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
