#!/usr/bin/env python3
"""ИИ-Офис hub.

Receives agent events from Claude Code / Codex hooks (hub/office_hook.py), keeps the live
state of every session, serves the office UI and streams state to it over SSE.
Approvals: a PermissionRequest hook call is held open while someone watches the office,
so a click on «Одобрить» in the office answers the real permission prompt.
Cloud agents (Grok Bot) report one status line per step to a private ntfy topic the hub listens to.

Python 3.9 stdlib only (the Mac's /usr/bin/python3).
"""
import json
import os
import queue
import re
import secrets
import threading
import time
import urllib.request
import subprocess
import sys
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import limits                                   # noqa: E402
import runners                                  # noqa: E402
from office_hook import project_of, root_of     # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, 'web')
DATA = os.path.join(ROOT, 'data')
STATE_FILE = os.path.join(DATA, 'state.json')
HOST = os.environ.get('OFFICE_HOST', '127.0.0.1')
PORT = int(os.environ.get('OFFICE_PORT', '4747'))
ALLOWED_HOSTS = {'127.0.0.1', 'localhost'}

NAMES = {'claude': 'Клод', 'codex': 'Кодекс', 'grok': 'Грок', 'gemini': 'Джемини', 'antigravity': 'Джемини'}
LOOK = {'antigravity': 'gemini'}   # which character the office draws for a source
LOCAL_HOST = os.uname().nodename.split('.')[0]
KINDS = ('claude', 'codex', 'antigravity')   # agents the office can run itself
PALETTE = ['#22a55b', '#8b5cf6', '#f08c1a', '#3b82f6', '#14b8a6', '#e5484d', '#d946ef', '#0ea5e9', '#84cc16', '#f59e0b']
HIST_LEN, HIST_STEP = 14, 300          # activity sparkline: 14 buckets of 5 minutes
IDLE_AFTER, FORGET_AFTER = 15 * 60, 12 * 3600
PROJECT_TTL = 30 * 60
MAX_WAIT = 120

lock = threading.RLock()
agents = {}       # "src:session" -> agent
projects = {}     # name -> {name, color, first, last}
approvals = {}    # id -> {id, agent, decision, live, event}
log = []          # newest first: [hh:mm, who, text, status]
settings = {'approveWait': 60, 'agentProxy': '', 'ntfyOn': False, 'ntfyTopic': '', 'ntfyServer': 'https://ntfy.sh'}
ntfy_state = {'state': 'off', 'last': None, 'since': '1h'}
clients = []      # one queue per open SSE stream
ALIAS = {}        # "src:session" of an office-run CLI session -> its office agent id
RUNS = {}         # office agent id -> {proc, queue, stopped}
dirty = threading.Event()


def short(s, n=80):
    s = ' '.join(re.sub(r'</?[a-zA-Z][\w-]*(?:\s[^>]*)?>', ' ', str(s or '')).split())   # drop <pasted_content …> style tags
    return s if len(s) <= n else s[:n - 1] + '…'


def base(p):
    p = str(p or '').rstrip('/')
    return os.path.basename(p) or p


def describe(tool, inp):
    inp = inp if isinstance(inp, dict) else {}
    if tool in ('Bash', 'shell', 'local_shell', 'exec_command', 'shell_command', 'Shell', 'run_command'):
        cmd = inp.get('description') or inp.get('command') or inp.get('cmd') or ''
        if isinstance(cmd, list):
            cmd = ' '.join(map(str, cmd))
        return 'Терминал: ' + short(cmd, 70)
    if tool in ('Edit', 'Write', 'MultiEdit', 'NotebookEdit', 'apply_patch', 'write_to_file', 'replace_file_content', 'multi_replace_file_content'):
        f = inp.get('file_path') or inp.get('notebook_path') or inp.get('path')
        return 'Правит ' + base(f) if f else 'Правит код'
    if tool in ('Read', 'view_file', 'view_file_outline', 'view_code_item'):
        return 'Читает ' + base(inp.get('file_path'))
    if tool in ('Grep', 'Glob', 'grep_files', 'grep_search', 'find_by_name', 'list_dir', 'codebase_search'):
        return 'Ищет ' + short(inp.get('pattern') or inp.get('query') or base(inp.get('path')), 40)
    if tool.startswith('browser_'):
        return 'Браузер: ' + tool[8:].replace('_', ' ')
    if tool in ('WebFetch', 'WebSearch', 'read_url_content', 'search_web'):
        return 'Ищет в интернете: ' + short(inp.get('query') or inp.get('url'), 50)
    if tool in ('Task', 'Agent'):
        return 'Запустил субагента: ' + short(inp.get('description'), 50)
    if tool.startswith('mcp__'):
        return 'MCP: ' + short(tool.split('__')[1] if tool.count('__') >= 2 else tool, 40)
    return short(tool or 'Работает', 40)


def hhmm(ts=None):
    return time.strftime('%H:%M', time.localtime(ts or time.time()))


def add_log(who, text, status):
    log.insert(0, [hhmm(), who, short(text, 120), status])
    del log[60:]


def chat_add(a, who, text):
    text = str(text or '').strip()
    if text:
        chat = a.setdefault('chat', [])
        chat.append({'who': who, 'text': text[:6000], 't': hhmm()})
        del chat[:-300]


def mark_dirty():
    dirty.set()


def ensure_project(name):
    p = projects.get(name)
    if p is None:
        used = {x['color'] for x in projects.values()}
        color = next((c for c in PALETTE if c not in used), PALETTE[len(projects) % len(PALETTE)])
        p = projects[name] = {'name': name, 'color': color, 'first': time.time(), 'last': time.time()}
    p['last'] = time.time()
    return p


def set_status(a, status, task=None):
    if a['status'] != status:
        a['status'] = status
        a['since'] = time.time()
    if task is not None:
        a['task'] = short(task, 110)


def new_agent(key, src, sid, p, name=None):
    proj = p.get('project') or base(p.get('cwd')) or 'Без проекта'
    ensure_project(proj)
    nm = name or NAMES.get(src, src.capitalize()) + '·' + proj
    same = [x for x in agents.values() if x['name'].startswith(nm)]
    if same and not name:
        nm += ' ' + str(len(same) + 1)
    a = {'id': key, 'src': src, 'session': sid, 'name': nm, 'project': proj, 'cwd': p.get('cwd') or '',
         'host': p.get('host') or '', 'status': 'resting', 'task': 'Сессия открыта', 'prompt': '',
         'since': time.time(), 'started': time.time(), 'last': time.time(), 'steps': 0, 'tasks': 0, 'events': 0,
         'hist': [0] * HIST_LEN, 'approval': None}
    agents[key] = a
    add_log(nm, 'появился на этаже ' + proj, 'resting')
    return a


def close_approval(a, how):
    ap = approvals.get(a.get('approval') or '')
    if ap and not ap['decision']:
        ap['decision'] = how
        ap['event'].set()
    a['approval'] = None


def agy_event(p):
    """Antigravity's lifecycle → the Claude Code event the rest of the hub understands."""
    ev = p.get('hook_event_name') or ''
    if ev == 'PreInvocation':          # fires before every model call; the first one starts a turn
        return 'UserPromptSubmit' if (p.get('invocation') or 1) <= 1 else 'PostToolUse'
    if ev == 'PostToolUse':            # no PreToolUse hook (Antigravity blocks on it), so describe the tool here
        return 'PreToolUse'
    if ev == 'Stop':
        return 'StopFailure' if p.get('stop_reason') == 'ERROR' else 'Stop'
    return ev


def handle_hook(src, p):
    """Apply one hook event. Returns the approval dict for PermissionRequest, else None."""
    ev = agy_event(p) if src == 'antigravity' else (p.get('hook_event_name') or '')
    sid = str(p.get('session_id') or p.get('thread_id') or 'unknown')
    key = ALIAS.get(src + ':' + sid, src + ':' + sid)
    with lock:
        a = agents.get(key)
        if ev == 'SessionEnd':
            if a and a.get('managed'):
                return None
            if a:
                close_approval(a, 'gone')
                add_log(a['name'], 'закрыл сессию', 'resting')
                del agents[key]
                mark_dirty()
            return None
        if a is None:
            a = new_agent(key, src, sid, p)
        now = time.time()
        a['last'] = now
        a['events'] += 1
        ensure_project(a['project'])
        if p.get('root') and p.get('host') == LOCAL_HOST:
            projects[a['project']]['path'] = p['root']
        tool = p.get('tool_name') or ''
        if ev in ('PreToolUse', 'PostToolUse', 'PostToolUseFailure', 'UserPromptSubmit', 'Stop', 'SessionStart') and a['approval']:
            close_approval(a, 'elsewhere')   # the prompt was answered in the terminal / Vibe Island
        ap = None
        if ev == 'SessionStart':
            set_status(a, 'resting', 'Сессия открыта')
        elif ev == 'UserPromptSubmit' and str(p.get('prompt') or '').lstrip().startswith(('<task-notification', '<system-reminder')):
            set_status(a, 'working')      # background notification, not a new task from the user
        elif ev == 'UserPromptSubmit':
            a['prompt'] = short(p.get('prompt'), 110)
            a['tasks'] += 1
            set_status(a, 'working', a['prompt'] or 'Думает')
            add_log(a['name'], 'новая задача: ' + short(a['prompt'], 70), 'working')
            if not a.get('managed'):
                chat_add(a, 'user', p.get('prompt'))
        elif ev == 'PreToolUse':
            a['steps'] += 1
            a['hist'][-1] += 1
            set_status(a, 'working', describe(tool, p.get('tool_input')))
        elif ev in ('PostToolUse', 'PostToolUseFailure'):
            if a['status'] != 'working':
                set_status(a, 'working')
        elif ev == 'PermissionRequest':
            close_approval(a, 'replaced')
            ap = {'id': uuid.uuid4().hex, 'agent': key, 'decision': None, 'live': True, 'event': threading.Event()}
            approvals[ap['id']] = ap
            a['approval'] = ap['id']
            set_status(a, 'waiting', 'Разрешить? ' + describe(tool, p.get('tool_input')))
            add_log(a['name'], 'ждёт разрешения: ' + describe(tool, p.get('tool_input')), 'waiting')
        elif ev == 'Notification':
            nt = p.get('notification_type') or ''
            if nt == 'permission_prompt' and a['status'] != 'waiting':
                set_status(a, 'waiting', p.get('message') or 'Ждёт разрешения в терминале')
                add_log(a['name'], 'ждёт разрешения в терминале', 'waiting')
            elif nt == 'idle_prompt' and a['status'] == 'working':
                set_status(a, 'done')
        elif ev == 'Stop':
            set_status(a, 'done', 'Готово: ' + (a['prompt'] or 'ответил'))
            add_log(a['name'], 'закончил', 'done')
            if p.get('reply') and not a.get('managed'):
                chat_add(a, 'bot', p['reply'])
        elif ev == 'StopFailure':
            set_status(a, 'error', 'Ход прервался из-за ошибки API')
            add_log(a['name'], 'застрял: ошибка API', 'error')
        mark_dirty()
        return ap


def decide(ap_id, decision):
    with lock:
        ap = approvals.get(ap_id)
        if not ap or ap['decision'] or not ap['live']:
            return False
        ap['decision'] = decision
        a = agents.get(ap['agent'])
        if a:
            a['approval'] = None
            if decision == 'allow':
                set_status(a, 'working', 'Разрешено, продолжает')
            else:
                set_status(a, 'done', 'Запрос отклонён')
            add_log('Ты', ('одобрил: ' if decision == 'allow' else 'отклонил: ') + a['name'], 'done' if decision == 'allow' else 'resting')
        ap['event'].set()
        mark_dirty()
        return True


def wait_for_decision(ap):
    with lock:
        if (agents.get(ap['agent']) or {}).get('managed'):
            wait = 600       # an office-run agent has no terminal: only the office can answer it
        else:
            wait = min(MAX_WAIT, int(settings.get('approveWait') or 0)) if clients else 0
    if wait > 0:
        ap['event'].wait(wait)
    with lock:
        if ap['decision'] in ('allow', 'deny'):
            return ap['decision']
        ap['live'] = False        # nobody decided here: the terminal / Vibe Island takes over
        mark_dirty()
        return None


# ---------- office-run agents: the hub starts the CLI itself and the chat is its stream ----------

def link_session(a, sid):
    """Hooks of the CLI session report as src:sid; point them at this office agent."""
    a['managed']['sid'] = sid
    a['managed'].pop('fork', None)
    hk = a['src'] + ':' + sid
    ALIAS[hk] = a['id']
    dup = agents.pop(hk, None) if hk != a['id'] else None
    if dup:
        close_approval(dup, 'gone')


def start_run(a, text):
    m = a['managed']
    sid = m.get('sid')
    if a['src'] == 'claude' and not sid:
        new_sid = str(uuid.uuid4())
        cmd = runners.command('claude', text, None, new_sid)
        link_session(a, new_sid)        # known before start, so its hooks never create a duplicate
    else:
        cmd = runners.command(a['src'], text, sid, fork=bool(m.get('fork')))
    env = dict(os.environ, PATH='/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:' + os.path.expanduser('~/.local/bin'))
    if settings.get('agentProxy'):        # e.g. the VPN's local proxy, for CLIs that ignore macOS VPN routing
        for k in ('HTTPS_PROXY', 'HTTP_PROXY', 'ALL_PROXY', 'https_proxy', 'http_proxy', 'all_proxy'):
            env[k] = settings['agentProxy']
        env['NO_PROXY'] = env['no_proxy'] = '127.0.0.1,localhost'
    proc = subprocess.Popen(cmd, cwd=m['path'], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, env=env)
    RUNS[a['id']] = {'proc': proc, 'queue': [], 'stopped': False}
    a['prompt'] = short(text, 110)
    a['tasks'] += 1
    set_status(a, 'working', a['prompt'])
    add_log(a['name'], 'новая задача: ' + short(text, 70), 'working')
    threading.Thread(target=pump, args=(a['id'], proc), daemon=True).start()


def pump(key, proc):
    with lock:
        kind = agents[key]['src']
    junk = []
    for raw in proc.stdout:
        line = raw.decode('utf-8', 'replace')
        evs = runners.parse(kind, line)
        if not evs:
            if line.strip() and not line.lstrip().startswith('{'):
                junk.append(line.strip())
            continue
        with lock:
            a = agents.get(key)
            if not a:
                continue
            for ev in evs:
                if ev[0] == 'session' and ev[1]:
                    link_session(a, str(ev[1]))
                elif ev[0] == 'text':
                    chat_add(a, 'bot', ev[1])
                elif ev[0] == 'tool':
                    d = describe(ev[1], ev[2])
                    chat_add(a, 'tool', d)
                    a['steps'] += 1
                    a['hist'][-1] += 1
                    if a['status'] != 'waiting':
                        set_status(a, 'working', d)
                elif ev[0] == 'done' and ev[1]:
                    set_status(a, 'done', 'Готово: ' + (a['prompt'] or 'ответил'))
                    add_log(a['name'], 'закончил', 'done')
                elif ev[0] == 'done':
                    set_status(a, 'error', 'Ошибка: ' + short(ev[2], 80))
                    chat_add(a, 'sys', ev[2] or 'Ошибка')
                    add_log(a['name'], 'застрял: ' + short(ev[2], 60), 'error')
            a['last'] = time.time()
            mark_dirty()
    rc = proc.wait()
    with lock:
        run = RUNS.pop(key, None) or {}
        a = agents.get(key)
        if not a:
            return
        if run.get('stopped'):
            set_status(a, 'resting', 'Остановлен')
            chat_add(a, 'sys', 'Остановлен')
        elif rc != 0 and a['status'] != 'error':
            set_status(a, 'error', 'Процесс завершился с кодом %s' % rc)
            chat_add(a, 'sys', '\n'.join(junk[-6:]) or 'Процесс завершился с кодом %s' % rc)
            add_log(a['name'], 'застрял: процесс упал', 'error')
        elif a['status'] == 'working':
            set_status(a, 'done', 'Готово: ' + (a['prompt'] or 'ответил'))
        queue = run.get('queue') or []
        if queue and a['status'] != 'error' and not run.get('stopped'):
            start_run(a, queue.pop(0))
            RUNS[key]['queue'] = queue
        mark_dirty()


def send_to(a, text):
    chat_add(a, 'me', text)
    run = RUNS.get(a['id'])
    if run and run['proc'].poll() is None:
        run['queue'].append(text)
        chat_add(a, 'sys', 'В очереди: агент закончит текущее и возьмёт это')
    else:
        start_run(a, text)
    mark_dirty()


def hire(kind, path, task, name=None):
    proj = project_of(path)
    key = 'office:' + uuid.uuid4().hex[:12]
    a = new_agent(key, kind, key, {'project': proj, 'cwd': path, 'host': 'офис'}, name)
    a['managed'] = {'path': path}
    a['task'] = 'Ждёт задачу'
    projects[proj]['path'] = root_of(path)
    if task:
        send_to(a, task)
    mark_dirty()
    return a


def scan_dirs():
    home, out, seen = os.path.expanduser('~'), [], set()
    for d in [home] + [os.path.join(home, x) for x in ('Projects', 'Developer', 'code', 'src', 'Documents', 'Desktop')]:
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for n in names:
            p = os.path.join(d, n)
            if not n.startswith('.') and p not in seen and os.path.isdir(os.path.join(p, '.git')):
                seen.add(p)
                out.append({'name': n, 'path': p})
    return out[:80]


_limits = {'at': 0, 'val': {}}


def cached_limits():
    if time.time() - _limits['at'] > 60:
        try:
            val = limits.read()
        except Exception as e:   # a third-party database: never let it break the hub
            print('limits failed:', e, flush=True)
            val = _limits['val']
        _limits.update(at=time.time(), val=val)
    return _limits['val']


STATUS_WORDS = {'working': 'working', 'работает': 'working', 'в работе': 'working', 'waiting': 'waiting', 'ждёт': 'waiting',
                'ждет': 'waiting', 'done': 'done', 'готово': 'done', 'finished': 'done', 'resting': 'resting', 'idle': 'resting',
                'отдыхает': 'resting', 'error': 'error', 'stuck': 'error', 'застрял': 'error', 'ошибка': 'error'}


def parse_status(text):
    """One status line from a cloud bot: JSON {agent, floor, status, task} or «Имя | Этаж | статус | задача»."""
    text = (text or '').strip()
    try:
        d = json.loads(text)
        if isinstance(d, dict):
            return d.get('agent') or d.get('name'), d.get('floor') or d.get('team'), d.get('status'), d.get('task') or ''
    except ValueError:
        pass
    parts = [x.strip() for x in text.split('|')]
    if len(parts) >= 4:
        return parts[0], parts[1], parts[2], ' | '.join(parts[3:])
    if len(parts) == 3:
        return parts[0], 'Grok', parts[1], parts[2]
    return None


def handle_status_line(text):
    got = parse_status(text)
    if not got or not got[0]:
        return
    name, floor, status, task = (short(x, 110) for x in got)
    status = STATUS_WORDS.get(status.lower(), 'working')
    key = 'grok:' + name.lower()
    with lock:
        a = agents.get(key)
        if a is None:
            a = new_agent(key, 'grok', name, {'project': floor or 'Grok', 'host': 'Grok Bot'}, name=name)
        elif floor and floor != a['project']:
            a['project'] = floor
        ensure_project(a['project'])
        a['last'] = time.time()
        a['events'] += 1
        if status == 'working' and task and task != a['task']:
            a['steps'] += 1
            a['hist'][-1] += 1
        if a['status'] != status:
            add_log(a['name'], {'working': 'работает: ', 'waiting': 'ждёт тебя: ', 'done': 'готово: ', 'error': 'застрял: '}.get(status, 'отдыхает: ') + task, status)
            if status == 'working':
                a['tasks'] += 1
        set_status(a, status, task or a['task'])
        mark_dirty()


def ntfy_loop():
    """Follow the private ntfy topic (outbound long-poll, so it works behind NAT/VPN)."""
    while True:
        with lock:
            on, topic, server = settings['ntfyOn'], settings['ntfyTopic'], settings['ntfyServer'].rstrip('/')
        if not on or not topic:
            ntfy_state['state'] = 'off'
            time.sleep(3)
            continue
        url = '%s/%s/json?since=%s' % (server, topic, ntfy_state['since'])
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'ai-office-hub'})
            with urllib.request.urlopen(req, timeout=90) as r:     # ntfy sends a keepalive every ~45 s
                ntfy_state['state'] = 'connected'
                mark_dirty()
                for line in r:
                    msg = json.loads(line)
                    if msg.get('event') == 'message':
                        ntfy_state['since'] = msg.get('id') or ntfy_state['since']
                        ntfy_state['last'] = msg.get('time')
                        handle_status_line(msg.get('message'))
                    with lock:
                        if not settings['ntfyOn'] or settings['ntfyTopic'] != topic:
                            break
        except Exception as e:  # network down, VPN off, server error: retry
            ntfy_state['state'] = 'error: ' + short(e, 80)
            mark_dirty()
            time.sleep(10)


_integr = {'at': 0, 'val': {}}


def integrations():
    if time.time() - _integr['at'] > 30:
        val = {}
        for k, path in (('claude', '~/.claude/settings.json'), ('codex', '~/.codex/hooks.json'), ('gemini', '~/.gemini/config/hooks.json')):
            try:
                with open(os.path.expanduser(path), encoding='utf-8') as f:
                    val[k] = 'office_hook.py' in f.read()
            except OSError:
                val[k] = False
        _integr.update(at=time.time(), val=val)
    return _integr['val']


def snapshot():
    with lock:
        live_projects = {a['project'] for a in agents.values()}
        projs = sorted((p for p in projects.values() if p['name'] in live_projects or time.time() - p['last'] < PROJECT_TTL),
                       key=lambda p: p['first'])
        pub = []
        for a in agents.values():
            ap = approvals.get(a['approval'] or '')
            pub.append({'id': a['id'], 'name': a['name'], 'kind': LOOK.get(a['src'], a['src']), 'status': a['status'], 'task': a['task'],
                        'project': a['project'], 'since': a['since'], 'steps': a['steps'], 'tasks': a['tasks'],
                        'events': a['events'], 'hist': a['hist'],
                        'approval': ap['id'] if ap and ap['live'] and not ap['decision'] else None,
                        'where': (a['host'] + ' · ' if a['host'] else '') + a['cwd'].replace(os.path.expanduser('~'), '~'),
                        'managed': bool(a.get('managed')), 'running': a['id'] in RUNS, 'chatLen': len(a.get('chat') or []),
                        'forkable': a['src'] == 'claude' and not a.get('managed') and a.get('host') == LOCAL_HOST})
        pub.sort(key=lambda x: x['name'])
        ntfy = {'on': settings['ntfyOn'], 'state': ntfy_state['state'], 'last': ntfy_state['last'],
                'url': settings['ntfyServer'].rstrip('/') + '/' + settings['ntfyTopic'] if settings['ntfyTopic'] else ''}
        return {'now': time.time(), 'viewers': len(clients), 'settings': {'approveWait': settings['approveWait'], 'agentProxy': settings['agentProxy']},
                'integrations': integrations(), 'ntfy': ntfy,
                'projects': [{'name': p['name'], 'color': p['color'], 'path': p.get('path') or ''} for p in projs],
                'agents': pub, 'log': log[:40], 'limits': cached_limits(),
                'kinds': [k for k in KINDS if runners.binary(k)]}


def save():
    with lock:
        data = {'agents': agents, 'projects': projects, 'log': log, 'settings': settings}
        txt = json.dumps(data, ensure_ascii=False)
    os.makedirs(DATA, exist_ok=True)
    tmp = STATE_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(txt)
    os.replace(tmp, STATE_FILE)


def load():
    try:
        with open(STATE_FILE, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return
    agents.update(data.get('agents', {}))
    projects.update(data.get('projects', {}))
    log.extend(data.get('log', []))
    settings.update(data.get('settings', {}))
    for a in agents.values():
        a['approval'] = None          # held hook calls do not survive a restart
        if a['status'] == 'waiting' or (a.get('managed') and a['status'] == 'working'):
            set_status(a, 'resting', 'Хаб перезапускался, работа прервана')
        if a.get('managed') and a['managed'].get('sid'):
            ALIAS[a['src'] + ':' + a['managed']['sid']] = a['id']


def broadcaster():
    last_save = 0
    while True:
        dirty.wait()
        time.sleep(0.15)
        dirty.clear()
        msg = json.dumps(snapshot(), ensure_ascii=False)
        with lock:
            for q in list(clients):
                q.put(msg)
        if time.time() - last_save > 5:
            last_save = time.time()
            try:
                save()
            except OSError as e:
                print('save failed:', e, flush=True)


def janitor():
    last_roll = time.time()
    while True:
        time.sleep(30)
        now = time.time()
        with lock:
            if now - last_roll >= HIST_STEP:
                last_roll = now
                for a in agents.values():
                    a['hist'] = a['hist'][1:] + [0]
            for key, a in list(agents.items()):
                if now - a['last'] > FORGET_AFTER and not a.get('managed'):
                    close_approval(a, 'gone')
                    del agents[key]
                elif a['status'] == 'working' and now - a['last'] > IDLE_AFTER:
                    set_status(a, 'resting', 'Тихо больше 15 минут')
            for ap_id, ap in list(approvals.items()):
                if ap['decision'] and ap['event'].is_set() and not any(a['approval'] == ap_id for a in agents.values()):
                    del approvals[ap_id]
        mark_dirty()


class Handler(BaseHTTPRequestHandler):
    server_version = 'AIOffice/1'
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        pass

    def _host_ok(self):
        host = (self.headers.get('Host') or '').rsplit(':', 1)[0].strip('[]')
        return host in ALLOWED_HOSTS

    def _send(self, code, body=b'', ctype='application/json; charset=utf-8', extra=None):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode('utf-8'))

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, b'{"error":"bad host"}')
        path = urlparse(self.path).path
        if path == '/api/state':
            return self._json(200, snapshot())
        if path == '/api/stream':
            return self._stream()
        if path == '/api/chat':
            aid = (parse_qs(urlparse(self.path).query).get('id') or [''])[0]
            with lock:
                a = agents.get(aid)
                if not a:
                    return self._json(404, {'error': 'нет такого агента'})
                return self._json(200, {'id': aid, 'chat': list(a.get('chat') or [])})
        if path == '/api/dirs':
            return self._json(200, {'dirs': scan_dirs()})
        if path == '/':
            path = '/index.html'
        full = os.path.normpath(os.path.join(WEB, path.lstrip('/')))
        if not full.startswith(WEB + os.sep) or not os.path.isfile(full):
            return self._send(404, b'{"error":"not found"}')
        ctype = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
                 '.css': 'text/css; charset=utf-8', '.png': 'image/png', '.svg': 'image/svg+xml'}.get(os.path.splitext(full)[1], 'application/octet-stream')
        with open(full, 'rb') as f:
            self._send(200, f.read(), ctype)

    def _stream(self):
        q = queue.Queue()
        with lock:
            clients.append(q)
        mark_dirty()                    # viewer count changed
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'keep-alive')
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(('data: ' + json.dumps(snapshot(), ensure_ascii=False) + '\n\n').encode('utf-8'))
            self.wfile.flush()
            while True:
                try:
                    msg = q.get(timeout=15)
                    self.wfile.write(('data: ' + msg + '\n\n').encode('utf-8'))
                except queue.Empty:
                    self.wfile.write(b': ping\n\n')
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with lock:
                if q in clients:
                    clients.remove(q)
            mark_dirty()

    def do_POST(self):
        # Custom header + host check: a web page in some browser tab cannot forge these calls.
        if not self._host_ok() or self.headers.get('X-Office') != '1':
            return self._send(403, b'{"error":"forbidden"}')
        try:
            n = int(self.headers.get('Content-Length') or 0)
            body = json.loads(self.rfile.read(n) or b'{}') if n else {}
            if not isinstance(body, dict):
                raise ValueError('body must be an object')
        except ValueError:
            return self._send(400, b'{"error":"bad json"}')
        url = urlparse(self.path)
        if url.path == '/api/hook':
            src = (parse_qs(url.query).get('src') or ['claude'])[0][:20]
            ap = handle_hook(src, body)
            if ap is None:
                return self._json(200, {})
            return self._json(200, {'decision': wait_for_decision(ap)})
        if url.path == '/api/decide':
            if body.get('decision') not in ('allow', 'deny'):
                return self._json(400, {'error': 'decision must be allow or deny'})
            ok = decide(str(body.get('id') or ''), body['decision'])
            return self._json(200 if ok else 409, {'ok': ok} if ok else {'error': 'Это разрешение уже решено в терминале или Vibe Island'})
        if url.path == '/api/settings' and 'agentProxy' in body:
            px = str(body.get('agentProxy') or '').strip()
            if px and not px.startswith(('http://127.0.0.1:', 'socks5://127.0.0.1:', 'http://localhost:')):
                return self._json(400, {'error': 'прокси должен быть локальным: http://127.0.0.1:порт'})
            with lock:
                settings['agentProxy'] = px
            mark_dirty()
            return self._json(200, {'ok': True})
        if url.path == '/api/settings':
            w = body.get('approveWait')
            if not isinstance(w, int) or not 0 <= w <= MAX_WAIT:
                return self._json(400, {'error': 'approveWait: 0..%d' % MAX_WAIT})
            with lock:
                settings['approveWait'] = w
            mark_dirty()
            return self._json(200, {'ok': True})
        if url.path == '/api/ntfy':
            with lock:
                settings['ntfyOn'] = bool(body.get('on'))
                if settings['ntfyOn'] and (not settings['ntfyTopic'] or body.get('newTopic')):
                    settings['ntfyTopic'] = 'ai-office-' + secrets.token_hex(12)
                    ntfy_state['since'] = '1h'
            mark_dirty()
            return self._json(200, {'ok': True})
        if url.path in ('/api/hire', '/api/send', '/api/stop', '/api/fork'):
            return self._office_run(url.path, body)
        if url.path == '/api/forget':
            with lock:
                a = agents.pop(str(body.get('id') or ''), None)
                if a:
                    close_approval(a, 'gone')
                    add_log('Ты', 'убрал из офиса: ' + a['name'], 'resting')
            mark_dirty()
            return self._json(200 if a else 404, {'ok': bool(a)})
        return self._send(404, b'{"error":"not found"}')


def _office_run(self, path, body):
    text = str(body.get('text') or body.get('task') or '').strip()[:8000]
    try:
        with lock:
            if path == '/api/hire':
                kind, where = body.get('kind'), os.path.realpath(os.path.expanduser(str(body.get('path') or '')))
                if kind not in KINDS or not runners.binary(kind):
                    return self._json(400, {'error': 'этот агент не установлен на Маке'})
                if not os.path.isdir(where) or not where.startswith(os.path.expanduser('~') + os.sep):
                    return self._json(400, {'error': 'папка должна существовать и лежать в домашней папке'})
                return self._json(200, {'id': hire(kind, where, text)['id']})
            a = agents.get(str(body.get('id') or ''))
            if not a:
                return self._json(404, {'error': 'нет такого агента'})
            if path == '/api/stop':
                run = RUNS.get(a['id'])
                if run:
                    run['stopped'] = True
                    run['proc'].terminate()
                return self._json(200, {'ok': bool(run)})
            if not text:
                return self._json(400, {'error': 'пустое сообщение'})
            if path == '/api/send':
                if not a.get('managed'):
                    return self._json(400, {'error': 'эта сессия идёт в терминале, писать можно только туда'})
                send_to(a, text)
                return self._json(200, {'ok': True})
            if a['src'] != 'claude' or a.get('managed') or a.get('host') != LOCAL_HOST:
                return self._json(400, {'error': 'продолжить в офисе можно только сессию Claude на этом Маке'})
            path_ = (projects.get(a['project']) or {}).get('path') or a['cwd']
            b = hire('claude', path_, '', name=a['name'] + ' · офис')
            b['managed'].update(sid=a['session'], fork=True)
            send_to(b, text)
            return self._json(200, {'id': b['id']})
    except (RuntimeError, OSError) as e:
        return self._json(500, {'error': str(e)})


Handler._office_run = _office_run


def main():
    load()
    threading.Thread(target=broadcaster, daemon=True).start()
    threading.Thread(target=janitor, daemon=True).start()
    threading.Thread(target=ntfy_loop, daemon=True).start()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    print('ИИ-Офис hub on http://%s:%d' % (HOST, PORT), flush=True)
    try:
        srv.serve_forever()
    finally:
        save()


if __name__ == '__main__':
    main()
