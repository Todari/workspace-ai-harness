#!/usr/bin/env python3
"""Local operation receipts and a token-free Slack/Discord report preview.

report: metadata only; never sends unless --send is explicit.
run: executes the supplied argv once, records its actual exit code, forwards output.
"""
import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

import harness_lib as lib
import stats

DB = Path(lib.CACHE_DIR) / 'operations.sqlite3'
# Tolerances include ordinary sleep; missing history is unknown, not healthy.
JOB_MAX_AGE = {'inbox-sweep': 36 * 3600, 'weekly-review': 8 * 86400,
               'vault-sync': 24 * 3600, 'vault-push': 36 * 3600,
               'jp-snapshot': 36 * 3600}


def utcnow():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')


@contextlib.contextmanager
def database(path=DB):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    try:
        conn.executescript('''
        CREATE TABLE IF NOT EXISTS jobs (
          name TEXT PRIMARY KEY, status TEXT NOT NULL, started_at REAL NOT NULL,
          finished_at REAL, last_success REAL, exit_code INTEGER);
        CREATE TABLE IF NOT EXISTS receipts (
          message_id TEXT PRIMARY KEY, disposition TEXT NOT NULL, note_path TEXT,
          content_hash TEXT, updated_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS deliveries (
          delivery_key TEXT PRIMARY KEY, status TEXT NOT NULL, updated_at REAL NOT NULL,
          error_code TEXT);
        ''')
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()



def receipt(message_id, db=DB):
    if not Path(db).exists():
        return None
    with database(db) as conn:
        row = conn.execute('SELECT disposition,note_path,content_hash FROM receipts WHERE message_id=?',
                           (message_id,)).fetchone()
    return dict(zip(('disposition', 'note_path', 'content_hash'), row)) if row else None


def record_receipt(message_id, note=None, skip_reason=None, db=DB, vault=lib.VAULT_ROOT):
    if not message_id.isdigit():
        raise ValueError('message ID must be numeric')
    if bool(note) == bool(skip_reason):
        raise ValueError('supply one note or skip reason')
    relative, digest = None, None
    if note:
        root = Path(vault).resolve()
        path = (root / note).resolve()
        try:
            relative = str(path.relative_to(root))
        except ValueError:
            raise ValueError('note must be inside the vault') from None
        if path.suffix != '.md':
            raise ValueError('receipt requires a Markdown note')
        data = path.read_bytes()
        marker = ('<!-- inbox:discord:%s -->' % message_id).encode()
        if marker not in data:
            raise ValueError('save the message marker in the note before recording the receipt')
        digest = hashlib.sha256(data).hexdigest()
        disposition = 'written'
    else:
        if skip_reason not in ('duplicate', 'no-learning'):
            raise ValueError('unsupported skip reason')
        disposition = 'skipped:' + skip_reason
    with database(db) as conn:
        conn.execute('INSERT OR REPLACE INTO receipts VALUES(?,?,?,?,?)',
                     (message_id, disposition, relative, digest, time.time()))
    return receipt(message_id, db)


def stop_process_group(proc, grace=2):
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        proc.wait()
        return
    deadline = time.monotonic() + grace
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    # Wait the full group grace, even when the shell leader exits immediately.
    remaining = deadline - time.monotonic()
    if remaining > 0:
        time.sleep(remaining)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        # Some managed macOS runtimes return EPERM for an already-exited group.
        # Do not silently call it a successful cleanup; the job remains failed.
        print('process group cleanup could not be confirmed', file=sys.stderr)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        print('job process is still alive; its inherited lock prevents a retry', file=sys.stderr)



class JobInterrupted(Exception):
    def __init__(self, signum):
        self.code = 128 + signum


def run_job(name, command, timeout=1800, db=DB):
    if name not in JOB_MAX_AGE:
        raise ValueError('unknown job')
    lockpath = Path(db).parent / ('.job-' + name + '.lock')
    lockpath.parent.mkdir(parents=True, exist_ok=True)
    with lockpath.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('job already running: ' + name, file=sys.stderr)
            return 75
        # The child also holds the lock if this parent is forcibly killed.
        return execute_job(name, command, timeout, db, lock.fileno())


def execute_job(name, command, timeout, db, lock_fd):
    now = time.time()
    with database(db) as conn:
        conn.execute("""INSERT INTO jobs(name,status,started_at) VALUES(?, 'running', ?)
          ON CONFLICT(name) DO UPDATE SET status='running', started_at=excluded.started_at,
          finished_at=NULL, exit_code=NULL""", (name, now))
    proc = None
    handlers = {}
    def interrupt(signum, frame):
        raise JobInterrupted(signum)
    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            handlers[signum] = signal.signal(signum, interrupt)
        # No shell interpolation; command output and credentials stay out of the DB.
        proc = subprocess.Popen(command, start_new_session=True, pass_fds=(lock_fd,))
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        stop_process_group(proc)
        code = 124
    except JobInterrupted as exc:
        # Avoid reentrant cleanup on a repeated cancellation signal.
        for signum in handlers:
            signal.signal(signum, signal.SIG_IGN)
        if proc is not None:
            stop_process_group(proc)
        code = exc.code
    except OSError:
        code = 127
    finally:
        for signum, handler in handlers.items():
            signal.signal(signum, handler)
    finished = time.time()
    with database(db) as conn:
        conn.execute("""UPDATE jobs SET status=?, finished_at=?, exit_code=?,
          last_success=CASE WHEN ?=0 THEN ? ELSE last_success END WHERE name=?""",
          ('success' if code == 0 else 'failed', finished, code, code, finished, name))
    return code if code >= 0 else 128 - code


def job_health(db=DB, now=None):
    now = time.time() if now is None else now
    rows = {}
    if Path(db).exists():
        with contextlib.closing(sqlite3.connect('file:' + str(Path(db).resolve()) + '?mode=ro', uri=True)) as conn:
            rows = {row[0]: row[1:] for row in conn.execute(
                'SELECT name,status,started_at,finished_at,last_success,exit_code FROM jobs')}
    result = []
    for name, age in JOB_MAX_AGE.items():
        row = rows.get(name)
        if not row:
            result.append({'job': name, 'status': 'unknown', 'last_success': None})
            continue
        status, started, finished, success, code = row
        if status == 'running' and now - started > 3600:
            status = 'stalled'
        elif status == 'success' and (success is None or now - success > age):
            status = 'stale'
        result.append({'job': name, 'status': status, 'last_success': success,
                       'exit_code': code, 'last_attempt': finished or started})
    return result


def build_report(days=1, db=DB):
    return {'schema_version': 1, 'generated_at': utcnow(),
            'metrics': stats.collect_report(days, include_claude=False), 'jobs': job_health(db)}


def markdown(report):
    m = report['metrics']
    w = m['codex_workers']
    usage = w['usage']
    lines = ['# 워크스페이스 운영 리포트', '', report['generated_at'], '',
             '- 집계 기간: 최근 %g일' % m['days'],
             '- Codex worker: %d회 (%s)' % (w['runs'], ', '.join(
                 '%s %s' % item for item in sorted(w['statuses'].items())) or '기록 없음'),
             '- Worker input: %s / cached: %s / output: %s' % (
                 usage.get('input_tokens', 0), usage.get('cached_input_tokens', 0),
                 usage.get('output_tokens', 0)),
             '- Claude 전체 사용량: 생략 (`stats.py --include-claude`로 별도 조회)',
             '', '| 자동화 | 상태 | 종료 코드 | 최근 실행(UTC) | 마지막 성공(UTC) |',
             '|---|---|---|---|---|']
    for row in report['jobs']:
        success = row.get('last_success')
        stamp = dt.datetime.fromtimestamp(success, dt.timezone.utc).isoformat(timespec='minutes') if success else '기록 없음'
        attempt = row.get('last_attempt')
        attempted = dt.datetime.fromtimestamp(attempt, dt.timezone.utc).isoformat(timespec='minutes') if attempt else '기록 없음'
        code = row.get('exit_code')
        lines.append('| %s | %s | %s | %s | %s |' % (
            row['job'], row['status'], code if code is not None else '—', attempted, stamp))
    sync_issues = {row['job'] for row in report['jobs']
                   if row['status'] in ('failed', 'stale', 'stalled')}
    if sync_issues & {'vault-push', 'vault-sync'}:
        lines.extend(['', '확인할 일: Obsidian Git 동기화·충돌을 확인하고, '
                      '`vault_sync.py --dry-run`으로 요약 수집을 점검하세요. '
                      '볼트 기록 성공과 원격·Discord 반영 성공은 별개입니다.'])
    lines.extend(['', 'unknown은 실행 기록 미수집, stale은 마지막 성공 기한 초과입니다.',
                  '기록된 worker 사용량만 포함합니다. 직접 실행한 Codex 세션과 비용 추정은 포함하지 않습니다.',
                  '작업 원문·볼트 본문·인증값은 이 리포트에 포함하지 않습니다.'])
    return '\n'.join(lines) + '\n'


def atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name, dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def webhook_payload(channel, text):
    if channel == 'slack':
        # Plain text prevents user/channel mentions and link expansion.
        return {'text': '워크스페이스 운영 리포트', 'blocks': [
            {'type': 'section', 'text': {'type': 'plain_text', 'text': text[:2900]}}],
            'unfurl_links': False, 'unfurl_media': False}
    if channel == 'discord':
        return {'content': text[:1900], 'allowed_mentions': {'parse': []}}
    raise ValueError('unsupported channel')


def validate_webhook(channel, url):
    parsed = urllib.parse.urlsplit(url)
    hosts = {'slack': ('hooks.slack.com',), 'discord': ('discord.com', 'discordapp.com')}
    prefix = '/services/' if channel == 'slack' else '/api/webhooks/'
    if (parsed.scheme != 'https' or parsed.hostname not in hosts.get(channel, ())
            or parsed.username or parsed.password or parsed.port not in (None, 443)
            or not parsed.path.startswith(prefix) or parsed.fragment):
        raise ValueError('invalid webhook URL for selected channel')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def deliver(channel, url, text, key, db=DB, retry_failed=False):
    validate_webhook(channel, url)
    # Include destination identity without persisting the credential-bearing URL.
    identity = hashlib.sha256((channel + '\0' + url + '\0' + key).encode()).hexdigest()
    with database(db) as conn:
        conn.execute('BEGIN IMMEDIATE')
        old = conn.execute('SELECT status FROM deliveries WHERE delivery_key=?', (identity,)).fetchone()
        if old and (old[0] != 'failed' or not retry_failed):
            return 'already-' + old[0]
        conn.execute('INSERT OR REPLACE INTO deliveries VALUES(?,?,?,NULL)',
                     (identity, 'sending', time.time()))
    request_url = url
    if channel == 'discord':
        p = urllib.parse.urlsplit(url)
        query = dict(urllib.parse.parse_qsl(p.query))
        query['wait'] = 'true'
        request_url = urllib.parse.urlunsplit(p._replace(query=urllib.parse.urlencode(query)))
    req = urllib.request.Request(request_url, method='POST',
        data=json.dumps(webhook_payload(channel, text), ensure_ascii=False).encode(),
        headers={'Content-Type': 'application/json', 'User-Agent': 'WorkspaceHarness/1.0'})
    error, status = None, 'sent'
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=20) as res:
            if not 200 <= res.status < 300:
                raise ValueError('unexpected status')
    except urllib.error.HTTPError as exc:
        error, status = 'http_%d' % exc.code, 'failed'
    except Exception:
        # Timeout may mean accepted-but-response-lost. Never automatically resend.
        error, status = 'delivery_unknown', 'uncertain'
    with database(db) as conn:
        conn.execute('UPDATE deliveries SET status=?,updated_at=?,error_code=? WHERE delivery_key=?',
                     (status, time.time(), error, identity))
    return status


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    run = sub.add_parser('run')
    run.add_argument('--job', required=True, choices=sorted(JOB_MAX_AGE))
    run.add_argument('--timeout', type=int, default=1800)
    run.add_argument('command', nargs=argparse.REMAINDER)
    rec = sub.add_parser('receipt')
    rec.add_argument('operation', choices=['check', 'written', 'skip'])
    rec.add_argument('message_id')
    rec.add_argument('--note')
    rec.add_argument('--reason', choices=['duplicate', 'no-learning'])
    report = sub.add_parser('report')
    report.add_argument('--days', type=float, default=1)
    report.add_argument('--json', action='store_true')
    report.add_argument('--output', type=Path)
    report.add_argument('--send', action='store_true')
    report.add_argument('--channel', choices=['slack', 'discord'])
    report.add_argument('--delivery-key')
    report.add_argument('--retry-failed', action='store_true')
    args = parser.parse_args(argv)
    if args.action == 'receipt':
        try:
            if args.operation == 'check':
                value = receipt(args.message_id)
            elif args.operation == 'written':
                value = record_receipt(args.message_id, note=args.note)
            else:
                value = record_receipt(args.message_id, skip_reason=args.reason)
        except (OSError, ValueError) as exc:
            print(type(exc).__name__ + ': receipt not recorded', file=sys.stderr)
            return 2
        print(json.dumps(value, ensure_ascii=False))
        return 0
    if args.action == 'run':
        command = args.command[1:] if args.command[:1] == ['--'] else args.command
        if not command or args.timeout <= 0:
            parser.error('a command and a positive timeout are required')
        return run_job(args.job, command, args.timeout)
    if args.days <= 0:
        parser.error('--days must be positive')
    if args.send and not args.channel:
        parser.error('--send requires --channel')
    data = build_report(args.days)
    rendered = json.dumps(data, ensure_ascii=False, indent=2) + '\n' if args.json else markdown(data)
    if args.output:
        atomic_write(args.output, rendered)
    else:
        print(rendered, end='')
    if args.send:
        env_key = 'HARNESS_' + args.channel.upper() + '_WEBHOOK_URL'
        url = os.environ.get(env_key, '')
        if not url:
            print(env_key + ' is not configured', file=sys.stderr)
            return 2
        try:
            result = deliver(args.channel, url, markdown(data),
                args.delivery_key or 'daily:' + dt.datetime.now().astimezone().date().isoformat(),
                retry_failed=args.retry_failed)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print('delivery: ' + result)
        return 0 if result in ('sent', 'already-sent') else 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
