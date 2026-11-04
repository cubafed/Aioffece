#!/usr/bin/env python3
"""Runs office agents on this machine for the hub on the Mac.

The hub is reached at AI_OFFICE_URL (on the Linux box: the ssh tunnel to the Mac, 127.0.0.1:4747).
Loop: long-poll /api/runner/poll for a job → run the agent CLI here (runners.command) → stream its
output back to /api/runner/lines → report the exit code to /api/runner/done.
"""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import runners  # noqa: E402
import usage    # noqa: E402

URL = os.environ.get('AI_OFFICE_URL', 'http://127.0.0.1:4747')
HOST = os.uname().nodename.split('.')[0]
HOME = os.path.expanduser('~')
PROCS = {}


def post(path, body, timeout=10):
    req = urllib.request.Request(URL + path, data=json.dumps(body).encode('utf-8'),
                                 headers={'Content-Type': 'application/json', 'X-Office': '1'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b'{}')


def run_job(job):
    jid, buf, last = job['id'], [], [time.time()]

    def flush(force=False):
        if buf and (force or len(buf) >= 20 or time.time() - last[0] > 0.3):
            try:
                post('/api/runner/lines', {'job': jid, 'lines': buf[:]})
            except Exception as e:
                print('lines failed:', e, flush=True)
            del buf[:]
            last[0] = time.time()

    rc = 1
    try:
        cwd = os.path.realpath(os.path.expanduser(job['cwd']))
        if job.get('chat') and cwd == os.path.join(HOME, '.ai-office-chats'):
            os.makedirs(cwd, exist_ok=True)       # scratch folder for chats without a project
        if not cwd.startswith(HOME + os.sep) or not os.path.isdir(cwd):
            raise RuntimeError('на %s нет папки %s' % (HOST, job['cwd']))
        if job.get('worktree'):
            cwd, branch = runners.make_worktree(cwd, job['agent'])
            post('/api/runner/lines', {'job': jid, 'lines': [], 'meta': {'cwd': cwd, 'branch': branch}})
        cmd = runners.command(job['kind'], job['text'], job.get('sid'), job.get('new_sid'), fork=bool(job.get('fork')),
                              system=job.get('system') or '', chat=bool(job.get('chat')), cwd=cwd, agent=job['agent'])
        env = dict(os.environ, OFFICE_AGENT_ID=job['agent'], AI_OFFICE_URL=URL,
                   PATH=os.path.expanduser('~/.local/bin') + ':/usr/local/bin:/usr/bin:/bin')
        p = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
        PROCS[jid] = p
        for raw in p.stdout:
            buf.append(raw.decode('utf-8', 'replace').rstrip('\n'))
            flush()
        rc = p.wait()
        if job.get('branch_commit') or '/.office-worktrees/' in cwd:      # same as the hub does on the Mac: commit the worker's changes
            g = lambda *a: subprocess.run(['git', '-C', cwd, *a], capture_output=True, text=True, timeout=60)
            if g('status', '--porcelain').stdout.strip():
                g('add', '-A')
                if g('-c', 'user.name=ИИ-Офис', '-c', 'user.email=office@localhost', 'commit', '-q', '-m', (job.get('text') or 'работа')[:72]).returncode == 0:
                    buf.append('Офис закоммитил изменения в ветку исполнителя')
    except Exception as e:
        buf.append(str(e))
    finally:
        PROCS.pop(jid, None)
        flush(force=True)
        try:
            post('/api/runner/done', {'job': jid, 'rc': rc})
        except Exception as e:
            print('done failed:', e, flush=True)


def main():
    kinds = [k for k in runners.KINDS_RUNNABLE if runners.binary(k)]
    print('ИИ-Офис runner on %s: %s → %s' % (HOST, ', '.join(kinds), URL), flush=True)
    dirs, dirs_at, used, used_at = [], 0, None, 0
    while True:
        if time.time() - dirs_at > 300:
            dirs, dirs_at = runners.scan_dirs(), time.time()
        if time.time() - used_at > 600:          # token usage from this box's agent logs, for the office history
            try:
                used = usage.scan()
            except Exception as e:
                print('usage failed:', e, flush=True)
            used_at = time.time()
        try:
            out = post('/api/runner/poll', {'host': HOST, 'kinds': kinds, 'dirs': dirs, 'usage': used}, timeout=35)
        except Exception as e:        # tunnel down or hub restarting
            print('poll failed:', e, flush=True)
            time.sleep(5)
            continue
        job = out.get('job')
        if not job:
            continue
        if job.get('stop'):
            p = PROCS.get(job['stop'])
            if p:
                p.terminate()
            continue
        threading.Thread(target=run_job, args=(job,), daemon=True).start()


if __name__ == '__main__':
    main()
