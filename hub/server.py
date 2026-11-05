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
import signal
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
import usage                                    # noqa: E402
from office_hook import project_of, root_of     # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, 'web')
DATA = os.path.join(ROOT, 'data')
STATE_FILE = os.path.join(DATA, 'state.json')
HOST = os.environ.get('OFFICE_HOST', '127.0.0.1')
PORT = int(os.environ.get('OFFICE_PORT', '4747'))
ALLOWED_HOSTS = {'127.0.0.1', 'localhost'}

NAMES = {'claude': 'Клод', 'codex': 'Кодекс', 'grok': 'Грок', 'gemini': 'Джемини', 'antigravity': 'Джемини', 'cursor': 'Курсор'}
LOOK = {'antigravity': 'gemini'}   # which character the office draws for a source
LOCAL_HOST = os.uname().nodename.split('.')[0]
KINDS = ('claude', 'codex', 'antigravity')   # agents the office can run itself
CHAT_PROJECT = 'Чаты'                         # plain conversations live here, not on a floor
MODEL_RU = {'claude': 'Claude', 'codex': 'GPT', 'antigravity': 'Gemini'}
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
settings = {'approveWait': 60, 'budget': 90, 'agentProxy': '', 'ntfyOn': False, 'ntfyTopic': '', 'ntfyServer': 'https://ntfy.sh'}
ntfy_state = {'state': 'off', 'last': None, 'since': '1h'}
weather = {}      # Moscow, from open-meteo: temp, code, day
briefing = {}     # morning summary: text, day
clients = []      # one queue per open SSE stream
ALIAS = {}        # "src:session" of an office-run CLI session -> its office agent id
RUNS = {}         # office agent id -> {proc, queue, stopped}
BROKEN = {}       # "host:kind" -> why runs there cannot start (not logged in, region) — remembered across restarts
AUTH_FAIL = ('Not logged in', 'Please run /login', 'Authentication required', 'not eligible', 'not currently available in your location')
REMOTE = {}       # host -> {last, kinds, dirs, jobs: Queue} — runners on other machines (hub/runner.py)
RJOBS = {}        # job id -> RemoteProc
boards = {}       # project -> {goal, status, report, lead, tasks, room, seq, wakes}
MAX_WAKES = 40    # agents waking each other per mission before the office waits for a human
dirty = threading.Event()


def short(s, n=80):
    s = ' '.join(re.sub(r'</?[a-zA-Z][\w-]*(?:\s[^>]*)?>', ' ', str(s or '')).split())   # drop <pasted_content …> style tags
    return s if len(s) <= n else s[:n - 1] + '…'


def human_prompt(raw):
    """Codex sends answers to its own questions as a JSON prompt ([{"questionItemId": …, "answers": …}]): show the answers."""
    t = str(raw or '').strip()
    if not t.startswith(('[{', '{')):
        return raw
    try:
        data = json.loads(t)
    except ValueError:
        return raw
    found = []

    def walk(x, key=''):
        if isinstance(x, dict):
            for k, v in x.items():
                walk(v, k)
        elif isinstance(x, list):
            for v in x:
                walk(v, key)
        elif isinstance(x, str) and key.lower() in ('answer', 'answers', 'text', 'label', 'value', 'content'):
            found.append(x)
    walk(data)
    return 'Ответ на вопрос: ' + ', '.join(found) if found else 'Ответ на вопрос агента'


def base(p):
    p = str(p or '').rstrip('/')
    return os.path.basename(p) or p


def ask_detail(tool, inp):
    """The exact thing an agent asks permission for: the full command, the file, the URL."""
    inp = inp if isinstance(inp, dict) else {}
    for k in ('command', 'cmd', 'file_path', 'notebook_path', 'path', 'url', 'query', 'pattern'):
        v = inp.get(k)
        if v:
            return ' '.join(map(str, v)) if isinstance(v, list) else str(v)[:300]
    return ''


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


JOURNAL = []      # log lines waiting to be appended to data/journal/<day>.jsonl on the next save


def add_log(who, text, status, project=''):
    log.insert(0, [hhmm(), who, short(text, 120), status])
    del log[60:]
    proj = project or next((a['project'] for a in agents.values() if a['name'] == who), '')
    JOURNAL.append({'t': time.time(), 'who': who, 'text': short(text, 300), 'status': status, 'project': proj})


def seed_journal():
    """First run: today's journal starts from the activity feed the hub already has."""
    path = os.path.join(DATA, 'journal', time.strftime('%Y-%m-%d') + '.jsonl')
    if os.path.exists(path):
        return
    now, day0 = time.localtime(), time.mktime(time.strptime(time.strftime('%Y-%m-%d'), '%Y-%m-%d'))
    for hm, who, text, status in reversed(log):
        try:
            h, m = map(int, hm.split(':'))
        except ValueError:
            continue
        if (h, m) <= (now.tm_hour, now.tm_min):
            proj = next((a['project'] for a in agents.values() if a['name'] == who), '')
            JOURNAL.append({'t': day0 + h * 3600 + m * 60, 'who': who, 'text': text, 'status': status, 'project': proj})


def digest(day):
    """One day of the office from the journal: per project what was started, finished, stuck, asked and merged."""
    path = os.path.join(DATA, 'journal', day + '.jsonl')
    rows = []
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass
    with lock:
        rows += [r for r in JOURNAL if time.strftime('%Y-%m-%d', time.localtime(r['t'])) == day]
    out, hours = {}, [0] * 24
    for r in rows:
        proj = r.get('project') or ''
        txt, who = r.get('text') or '', r.get('who') or ''
        m = re.search(r'(?:этаж[ау]?|цель этажу|ветку \S+ на этаже)\s+(\S+)', txt) if not proj else None
        proj = proj or (m.group(1) if m else 'Офис')
        p = out.setdefault(proj, {'started': [], 'done': 0, 'stuck': [], 'asked': 0, 'merged': [], 'goals': [], 'agents': set()})
        if who not in ('Ты', 'Офис'):
            p['agents'].add(who)
        if txt.startswith('новая задача: '):
            p['started'].append({'who': who, 'text': txt[14:], 't': r['t']})
            hours[time.localtime(r['t']).tm_hour] += 1
        elif txt.startswith('закончил') or txt.startswith('Готово'):
            p['done'] += 1
        elif txt.startswith('застрял'):
            p['stuck'].append({'who': who, 'text': txt, 't': r['t']})
        elif txt.startswith('ждёт разрешения'):
            p['asked'] += 1
        elif 'слил' in txt or 'слита' in txt:
            p['merged'].append(txt)
        elif 'цель' in txt:
            p['goals'].append(txt)
    for p in out.values():
        p['agents'] = sorted(p['agents'])
    return {'day': day, 'projects': out, 'hours': hours}


def chat_add(a, who, text):
    text = str(text or '').strip()
    if text:
        chat = a.setdefault('chat', [])
        chat.append({'who': who, 'text': text[:6000], 't': hhmm()})
        del chat[:-300]


def last_bot(a):
    """The agent's latest answer, one paragraph, for the console view."""
    for m in reversed(a.get('chat') or []):
        if m.get('who') == 'bot':
            return short(m.get('text'), 240)
    return ''


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
    if a['status'] != status and status == 'error':
        a['errors'] = a.get('errors', 0) + 1
    if status == 'working' and time.localtime().tm_hour < 6:
        a['night'] = True
    if a['status'] != status:
        a['status'] = status
        a['since'] = time.time()
    if task is not None:
        a['task'] = short(task, 110)


def new_agent(key, src, sid, p, name=None):
    proj = p.get('project') or base(p.get('cwd')) or 'Без проекта'
    ensure_project(proj)
    nm = name or NAMES.get(src, src.capitalize()) + '·' + proj
    taken = {x['name'] for x in agents.values()}
    if nm in taken and not name:              # first free number, not a count: «Клод·Дом 2» may still be around when 1 has left
        nm += ' ' + str(next(n for n in range(2, 999) if nm + ' ' + str(n) not in taken))
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
        if p.get('root') and p.get('host'):
            set_place(a['project'], p['host'], p['root'])
        tool = p.get('tool_name') or ''
        if ev in ('PreToolUse', 'PostToolUse', 'PostToolUseFailure', 'UserPromptSubmit', 'Stop', 'SessionStart') and a['approval']:
            close_approval(a, 'elsewhere')   # the prompt was answered in the terminal / Vibe Island
        ap = None
        if ev == 'SessionStart':
            set_status(a, 'resting', 'Сессия открыта')
        elif ev == 'UserPromptSubmit' and str(p.get('prompt') or '').lstrip().startswith(('<task-notification', '<system-reminder')):
            set_status(a, 'working')      # background notification, not a new task from the user
        elif ev == 'UserPromptSubmit' and a.get('managed'):
            set_status(a, 'working')      # the office already knows the task it sent; the hook sees it with the role prompt glued on
        elif ev == 'UserPromptSubmit':
            a['prompt'] = short(human_prompt(p.get('prompt')), 110)
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
            ap = {'id': uuid.uuid4().hex, 'agent': key, 'decision': None, 'live': True, 'event': threading.Event(),
                  'ask': {'tool': tool, 'text': ask_detail(tool, p.get('tool_input'))}}
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
        elif ev == 'AgentReply':
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


class RemoteProc:
    """A run on another machine's runner (hub/runner.py). Looks like a Popen to pump()."""

    def __init__(self, host, agent):
        self.id, self.host, self.agent = uuid.uuid4().hex, host, agent
        self.q, self.rc, self.done = queue.Queue(), None, threading.Event()
        self.stdout = iter(self.q.get, None)

    def feed(self, lines):
        for line in lines:
            self.q.put((str(line).rstrip('\n') + '\n').encode('utf-8'))

    def finish(self, rc):
        if not self.done.is_set():
            self.rc = rc
            self.done.set()
            self.q.put(None)

    def poll(self):
        return self.rc

    def wait(self):
        self.done.wait()
        return self.rc

    def terminate(self):
        rn = REMOTE.get(self.host)
        if rn:
            rn['jobs'].put({'stop': self.id})


def runner_online(host):
    rn = REMOTE.get(host)
    return rn if rn and time.time() - rn.get('last', 0) < 60 else None


def host_kinds(host):
    if host == LOCAL_HOST:
        kinds = [k for k in KINDS if runners.binary(k)]
    else:
        rn = runner_online(host)
        kinds = list(rn['kinds']) if rn else []
    return [k for k in kinds if host + ':' + k not in BROKEN]


def continuable(a):
    """'' when a terminal session can go on inside the office, otherwise the reason it cannot."""
    if a.get('managed'):
        return ''
    if a['src'] not in ('claude', 'codex') or not a.get('session') or a.get('chatOnly'):
        return 'эту сессию можно продолжить только там, где она идёт'
    host = a.get('host') or LOCAL_HOST
    if a['src'] not in host_kinds(host):
        return '%s на %s сейчас недоступен офису' % (a['src'], host)
    return ''


def continue_in_office(a, text):
    """Answer a terminal session from the office. Claude forks the session (the terminal one stays untouched);
    Codex has no fork, so it resumes the same thread, and only while the terminal is idle."""
    why = continuable(a)
    if why:
        raise RuntimeError(why)
    b = agents.get(a.get('cont') or '')
    if b and b.get('managed'):
        send_to(b, text)
        return b
    if a['src'] == 'codex' and a['status'] == 'working':
        raise RuntimeError('Codex сейчас работает в своём окне: дождись, пока он закончит')
    host = a.get('host') or LOCAL_HOST
    b = hire(a['src'], a['cwd'] or (projects.get(a['project']) or {}).get('path'), '', name=a['name'] + ' · офис',
             project=a['project'], host=host)
    b['managed'].update(sid=a['session'], fork=a['src'] == 'claude')
    a['cont'] = b['id']
    send_to(b, text)
    return b


def start_run(a, text):
    m = a['managed']
    sid, new_sid, fork, system = m.get('sid'), None, bool(m.get('fork')), m.get('system') or ''
    if a['src'] == 'claude' and not sid:
        new_sid = str(uuid.uuid4())
        link_session(a, new_sid)        # known before start, so its hooks never create a duplicate
    host = m.get('host') or LOCAL_HOST
    if host != LOCAL_HOST:
        rn = runner_online(host)
        if not rn:
            raise RuntimeError('исполнитель на %s не на связи' % host)
        proc = RemoteProc(host, a['id'])
        RJOBS[proc.id] = proc
        rn['jobs'].put({'id': proc.id, 'agent': a['id'], 'kind': a['src'], 'text': text, 'sid': None if new_sid else sid,
                        'new_sid': new_sid, 'fork': fork, 'system': system, 'cwd': m['path'], 'worktree': m.pop('worktree_of', None),
                        'chat': bool(m.get('chat'))})
    else:
        cmd = runners.command(a['src'], text, None if new_sid else sid, new_sid, fork=fork, system=system, chat=bool(m.get('chat')), cwd=m['path'], agent=a['id'])
        env = dict(os.environ, OFFICE_AGENT_ID=a['id'], PATH='/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:' + os.path.expanduser('~/.local/bin'))
        if settings.get('agentProxy'):        # e.g. the VPN's local proxy, for CLIs that ignore macOS VPN routing
            for k in ('HTTPS_PROXY', 'HTTP_PROXY', 'ALL_PROXY', 'https_proxy', 'http_proxy', 'all_proxy'):
                env[k] = settings['agentProxy']
            env['NO_PROXY'] = env['no_proxy'] = '127.0.0.1,localhost'
        proc = subprocess.Popen(cmd, cwd=m['path'], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, env=env)
    RUNS[a['id']] = {'proc': proc, 'queue': [], 'stopped': False}
    a['prompt'] = short(re.sub(r'^\[([^\]]+?) пишет тебе в ИИ-Офисе\]:\s*', r'\1: ', text), 110)   # «Мастер: …» instead of the raw envelope
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
        where = (a['managed'].get('host') or LOCAL_HOST) + ':' + a['src']
        tail = '\n'.join(junk) + ''.join(m['text'] for m in (a.get('chat') or [])[-3:])
        if any(x in tail for x in AUTH_FAIL):
            BROKEN[where] = 'не залогинен' if 'location' not in tail and 'eligible' not in tail else 'недоступен в этом регионе'
        elif a['status'] == 'done' and rc == 0:
            BROKEN.pop(where, None)
        if run.get('stopped'):
            set_status(a, 'resting', 'Остановлен')
            chat_add(a, 'sys', 'Остановлен')
        elif rc != 0 and a['status'] != 'error':
            set_status(a, 'error', 'Процесс завершился с кодом %s' % rc)
            chat_add(a, 'sys', '\n'.join(junk[-6:]) or 'Процесс завершился с кодом %s' % rc)
            add_log(a['name'], 'застрял: процесс упал', 'error')
        elif a['status'] == 'working':
            set_status(a, 'done', 'Готово: ' + (a['prompt'] or 'ответил'))
        commit_worker(a)
        if (a.get('mission') or {}).get('role') == 'worker' and not run.get('stopped'):
            last = next((m['text'] for m in reversed(a.get('chat') or []) if m['who'] == 'bot'), '')
            lead = agents.get(a['mission'].get('lead') or '')
            if lead:
                deliver(lead, '%s закончил ход (%s). Его ответ: %s' % (a['name'], ST_RU.get(a['status'], a['status']), short(last, 700)), 'Офис')
        queue = run.get('queue') or []
        if queue and a['status'] != 'error' and not run.get('stopped'):
            start_run(a, queue.pop(0))
            RUNS[key]['queue'] = queue
        mark_dirty()


def commit_worker(a):
    m = a.get('managed') or {}
    if not m.get('branch') or (m.get('host') or LOCAL_HOST) != LOCAL_HOST or not os.path.isdir(m.get('path') or ''):
        return
    if not git(m['path'], 'status', '--porcelain').stdout.strip():
        return
    git(m['path'], 'add', '-A')
    r = git(m['path'], '-c', 'user.name=ИИ-Офис · ' + a['name'], '-c', 'user.email=office@localhost', 'commit', '-q', '-m', '%s: %s' % (a['name'], a['prompt'] or 'работа'))
    if r.returncode == 0:
        chat_add(a, 'sys', 'Офис закоммитил изменения в ветку ' + m['branch'])


def send_to(a, text):
    chat_add(a, 'me', text)
    run = RUNS.get(a['id'])
    if run and run['proc'].poll() is None:
        run['queue'].append(text)
        chat_add(a, 'sys', 'В очереди: агент закончит текущее и возьмёт это')
    else:
        start_run(a, text)
    mark_dirty()


def hire(kind, path, task, name=None, project=None, system='', mission=None, worktree=False, host=None):
    host = host or LOCAL_HOST
    local = host == LOCAL_HOST
    proj = project or (project_of(path) if local else os.path.basename(path.rstrip('/')))
    key = 'office:' + uuid.uuid4().hex[:12]
    work, branch = runners.make_worktree(root_of(path), key) if (worktree and local) else (path, None)
    a = new_agent(key, kind, key, {'project': proj, 'cwd': work, 'host': host}, name)
    a['managed'] = {'path': work, 'system': system, 'branch': branch, 'host': host}
    if worktree and not local:
        a['managed']['worktree_of'] = path     # the remote runner makes the checkout before the first run
    if mission:
        a['mission'] = mission
    a['task'] = 'Ждёт задачу'
    if not project:
        set_place(proj, host, root_of(path) if local else path)
    if task:
        send_to(a, task)
    mark_dirty()
    return a


# ---------- missions: a floor works on one goal together ----------

ST_RU = {'working': 'работает', 'waiting': 'ждёт', 'done': 'готово', 'resting': 'отдыхает', 'error': 'ошибка'}
TASK_STATUSES = ('todo', 'doing', 'review', 'done', 'blocked')

LEAD_PROMPT = '''Ты — Диспетчер этажа «{proj}» в ИИ-Офисе. Цель команды: {goal}

У тебя есть инструменты office_* (MCP-сервер office). Порядок работы:
1. Изучи репозиторий и разбей цель на 2–6 конкретных задач (office_add_task).
2. Найми исполнителей через office_hire. Доступные сейчас: {team}. Не больше трёх. Каждый работает в своей git-ветке и сам напишет тебе, когда закончит.
3. Следи за доской (office_board) и сообщениями (office_inbox), отвечай через office_send.
4. Работу исполнителей проверяй (git log, git diff по их веткам). Готовую задачу переводи в review — сливает ветки человек в офисе. Сам не сливай.
5. Когда всё готово к слиянию, отчитайся через office_report. Ничего не пушь в удалённый репозиторий.
Отвечай по-русски, коротко.'''

WORKER_PROMPT = '''Ты — {name}, исполнитель на этаже «{proj}» в ИИ-Офисе. Работаешь в своей копии репозитория{branch}. Руководитель — {lead}.
Инструменты office_*: office_board (доска), office_take_task и office_update_task (статусы задач), office_send (написать руководителю или коллеге), office_inbox.
Когда закончишь: закоммить изменения в свою ветку и поставь задаче статус review. Не сливай ветки и не пушь в удалённый репозиторий. Отвечай по-русски, коротко.'''


def board_of(proj):
    return boards.setdefault(proj, {'goal': '', 'status': '', 'report': '', 'lead': None, 'tasks': [], 'room': [], 'seq': 0, 'wakes': 0})


def room_add(b, frm, to, text):
    b['room'].append({'from': frm, 'to': to, 'text': short(text, 2000), 't': hhmm(), 'read': []})
    del b['room'][:-200]


def deliver(t, text, sender):
    """Wake an office agent with a message from a colleague (queued if it is busy)."""
    if not t.get('managed'):
        return
    b = board_of(t['project'])
    lim = cached_limits().get(LOOK.get(t['src'], t['src'])) or {}
    used = max((lim.get('five') or {}).get('pct') or 0, (lim.get('week') or {}).get('pct') or 0)
    if settings.get('budget', 90) < 100 and used >= settings.get('budget', 90):
        room_add(b, 'Офис', 'человек', 'Пауза: лимит %s %d%% (бюджет %d%%). %s не стал будить %s.' % (lim.get('label', t['src']), used, settings['budget'], sender, t['name']))
        add_log('Офис', 'пауза по бюджету: ' + t['name'], 'waiting')
        return
    b['wakes'] += 1
    if b['wakes'] > MAX_WAKES:
        if b['wakes'] == MAX_WAKES + 1:
            room_add(b, 'Офис', 'человек', 'Агенты будили друг друга %d раз. Жду тебя: напиши команде, чтобы продолжить.' % MAX_WAKES)
            add_log('Офис', 'этаж %s ждёт человека' % t['project'], 'waiting')
        return
    msg = '[%s пишет тебе в ИИ-Офисе]: %s' % (sender, text)
    chat_add(t, 'peer', '%s: %s' % (sender, text))
    run = RUNS.get(t['id'])
    if run and run['proc'].poll() is None:
        run['queue'].append(msg)
    else:
        start_run(t, msg)


def find_agent(proj, name):
    name = (name or '').strip().lower()
    return next((x for x in agents.values() if x['project'] == proj and x['name'].lower() in (name, name.replace('@', ''))), None) or \
        next((x for x in agents.values() if x['project'] == proj and name and name in x['name'].lower()), None)


def who_called(body):
    a = agents.get(str(body.get('agent') or ''))
    if a:
        return a
    proj, src = project_of(str(body.get('cwd') or os.path.expanduser('~'))), body.get('src')
    cands = sorted((x for x in agents.values() if x['project'] == proj and x['src'] == src), key=lambda x: -x['last'])
    return cands[0] if cands else None


def mcp_call(body):
    a = who_called(body)
    if not a:
        return 'Не нашёл тебя в ИИ-Офисе: открой сессию в папке проекта.', True
    proj, me, args, tool = a['project'], a['name'], body.get('args') or {}, body.get('tool')
    b = board_of(proj)
    task_by = lambda i: next((t for t in b['tasks'] if str(t['id']) == str(i).lstrip('#')), None)
    if tool == 'office_team':
        rows = ['%s (%s) — %s: %s' % (x['name'], NAMES.get(x['src'], x['src']), ST_RU.get(x['status'], x['status']), x['task'])
                for x in agents.values() if x['project'] == proj]
        return 'Этаж %s:\n%s' % (proj, '\n'.join(rows)), False
    if tool == 'office_board':
        rows = ['#%s [%s] %s — %s%s' % (t['id'], t['status'], t['title'], t['owner'] or 'никто', (' · ' + t['note']) if t.get('note') else '') for t in b['tasks']]
        return 'Цель: %s\n%s' % (b['goal'] or 'не задана', '\n'.join(rows) or 'Задач пока нет'), False
    if tool == 'office_add_task':
        title = short(args.get('title'), 140)
        if not title:
            return 'Нужен title', True
        b['seq'] += 1
        t = {'id': b['seq'], 'title': title, 'details': short(args.get('details'), 1000), 'status': 'todo', 'owner': short(args.get('assignee'), 60), 'note': '', 'by': me}
        b['tasks'].append(t)
        add_log(me, 'добавил задачу #%d: %s' % (t['id'], title), 'working')
        return 'Задача #%d добавлена' % t['id'], False
    if tool == 'office_take_task':
        t = task_by(args.get('id'))
        if not t:
            return 'Нет задачи %s' % args.get('id'), True
        t.update(owner=me, status='doing')
        return 'Задача #%s теперь твоя' % t['id'], False
    if tool == 'office_update_task':
        t, st = task_by(args.get('id')), str(args.get('status') or '').lower()
        if not t or st not in TASK_STATUSES:
            return 'Нужны существующий id и статус из: %s' % ', '.join(TASK_STATUSES), True
        t.update(status=st, note=short(args.get('note'), 300) or t.get('note', ''))
        add_log(me, 'задача #%s → %s' % (t['id'], st), 'done' if st == 'done' else 'working')
        return 'Готово', False
    if tool == 'office_send':
        to, text = str(args.get('to') or '').strip(), str(args.get('text') or '').strip()
        if not to or not text:
            return 'Нужны to и text', True
        a['msgs'] = a.get('msgs', 0) + 1
        if to.lower() in ('человек', 'ты', 'user', 'владелец', 'human'):
            room_add(b, me, 'человек', text)
            add_log(me, 'пишет тебе: ' + short(text, 60), 'waiting')
            return 'Сообщение человеку оставлено в офисе', False
        targets = [x for x in agents.values() if x['project'] == proj and x is not a and x.get('managed')] if to.lower() in ('all', 'все', 'всем') else [find_agent(proj, to)]
        targets = [x for x in targets if x]
        if not targets:
            return 'Не нашёл «%s» на этаже. office_team покажет, кто здесь.' % to, True
        room_add(b, me, to, text)
        for x in targets:
            deliver(x, text, me)
        return 'Отправлено: %s' % ', '.join(x['name'] for x in targets), False
    if tool == 'office_inbox':
        mine = [m for m in b['room'] if m['from'] != me and me not in m['read'] and (m['to'] in (me, 'all', 'все', 'всем') or m['to'].lower() in me.lower())]
        for m in mine:
            m['read'].append(me)
        return '\n'.join('%s %s: %s' % (m['t'], m['from'], m['text']) for m in mine) or 'Новых сообщений нет', False
    if tool == 'office_hire':
        kind = {'gemini': 'antigravity', 'gpt': 'codex'}.get(str(args.get('kind') or 'claude').lower(), str(args.get('kind') or 'claude').lower())
        if kind == 'grok':
            bots = [x for x in agents.values() if x['src'] == 'grok']
            want = str(args.get('name') or '').lower()
            bot = next((x for x in bots if want and want in x['name'].lower()), None) or (sorted(bots, key=lambda x: x['status'] == 'working')[0] if bots else None)
            if not bot:
                return 'Grok-ботов в офисе нет', True
            bot['mission'] = {'role': 'worker', 'lead': b['lead'] if b['lead'] in agents else a['id']}
            try:
                grok_send(bot, str(args.get('task') or ''), me)
            except Exception as e:
                return 'Не смог отправить Grok-боту: %s' % e, True
            return 'Задача отправлена Grok-боту %s. Он отвечает не сразу: проверяет входящие по расписанию.' % bot['name'], False
        host, path = place_of(proj)
        if not path:
            return 'Не знаю, где лежит проект %s' % proj, True
        if kind not in KINDS or kind not in host_kinds(host):
            others = host_kinds(host)
            return '%s здесь недоступен%s.%s' % (kind, (': ' + BROKEN[host + ':' + kind]) if host + ':' + kind in BROKEN else '',
                                                (' Найми kind="%s".' % others[0]) if others else ''), True
        busy = sum(1 for x in agents.values() if x['project'] == proj and x.get('mission') and x['mission'].get('role') == 'worker')
        if busy >= 4:
            return 'На этаже уже 4 исполнителя: дай задачу им или отпусти кого-то', True
        lead = b['lead'] if b['lead'] in agents else a['id']
        nm = short(args.get('name'), 40) or '%s·%s %d' % (NAMES[kind], proj, busy + 1)
        w = hire(kind, path, '', name=nm, project=proj, mission={'role': 'worker', 'lead': lead}, worktree=True, host=host)
        w['managed']['system'] = WORKER_PROMPT.format(name=nm, proj=proj, lead=agents[lead]['name'],
                                                      branch=(' (ветка %s)' % w['managed']['branch']) if w['managed'].get('branch') else '')
        send_to(w, str(args.get('task') or 'Посмотри доску office_board и возьми задачу.'))
        add_log(me, 'нанял %s' % nm, 'working')
        return 'Нанят %s%s. Он напишет, когда закончит.' % (nm, (' в ветке ' + w['managed']['branch']) if w['managed'].get('branch') else ''), False
    if tool == 'office_report':
        b.update(status='done', report=short(args.get('text'), 3000))
        room_add(b, me, 'человек', 'Отчёт: ' + b['report'])
        add_log(me, 'отчитался по цели этажа %s' % proj, 'done')
        return 'Отчёт сохранён', False
    return 'Неизвестный инструмент %s' % tool, True


TEAM_RU = {'claude': 'kind="claude" — Клод, сильнее всех в реализации и рефакторинге',
           'codex': 'kind="codex" — GPT (Кодекс), хорош в тестах, ревью и аккуратных правках',
           'antigravity': 'kind="gemini" — Джемини, документация, исследование, большие объёмы текста'}


def team_text(host):
    team = [TEAM_RU[k] for k in host_kinds(host) if k in TEAM_RU]
    bots = [x['name'] for x in agents.values() if x['src'] == 'grok']
    if bots and settings.get('ntfyOn'):
        team.append('kind="grok" — Grok-боты (%s): тренды и посты в X, поиск в вебе; отвечают не сразу' % ', '.join(bots))
    return '; '.join(team) or 'только ты сам'


def git(root, *args):
    return subprocess.run(['git', '-C', root, *args], capture_output=True, text=True, timeout=60)


def review_branches(proj):
    """Branches of this floor's workers: what the human can review and merge."""
    return [{'branch': a['managed']['branch'], 'worker': a['name'], 'id': a['id'], 'status': a['status']}
            for a in agents.values() if a['project'] == proj and a.get('managed') and a['managed'].get('branch')]


def review(proj, branch):
    host, root = place_of(proj)
    if host != LOCAL_HOST or not root:
        raise RuntimeError('ревью веток работает для проектов на этом Маке')
    if branch not in [b['branch'] for b in review_branches(proj)]:
        raise RuntimeError('нет такой ветки исполнителя')
    base = git(root, 'rev-parse', '--abbrev-ref', 'HEAD').stdout.strip() or 'main'
    diff = git(root, 'diff', base + '...' + branch).stdout
    return {'base': base, 'branch': branch, 'commits': git(root, 'log', '--oneline', '-n', '50', base + '..' + branch).stdout,
            'stat': git(root, 'diff', '--stat', base + '...' + branch).stdout,
            'diff': diff[:300000], 'truncated': len(diff) > 300000}


def merge_branch(proj, branch):
    info = review(proj, branch)          # validates project, host and branch
    root = place_of(proj)[1]
    if info['base'] == branch:
        raise RuntimeError('папка проекта сейчас на ветке исполнителя, слияние не имеет смысла')
    if not info['commits'].strip():
        raise RuntimeError('в ветке нет изменений, сливать нечего')
    if any(a['id'] in RUNS for a in agents.values() if a.get('managed') and a['managed'].get('branch') == branch):
        raise RuntimeError('исполнитель ещё работает, дождись, пока он закончит')
    r = git(root, 'merge', '--no-ff', branch, '-m', 'Слияние %s из ИИ-Офиса' % branch)
    if r.returncode != 0:
        files = git(root, 'diff', '--name-only', '--diff-filter=U').stdout.split()
        git(root, 'merge', '--abort')
        raise RuntimeError('конфликт, слияние отменено%s' % ((': ' + ', '.join(files[:8])) if files else ': ' + short(r.stderr or r.stdout, 200)))
    w = next((a for a in agents.values() if a.get('managed') and a['managed'].get('branch') == branch), None)
    b = board_of(proj)
    if w:
        if w['managed']['path'] != root:
            git(root, 'worktree', 'remove', '--force', w['managed']['path'])
        git(root, 'branch', '-d', branch)
        w['managed'].update(path=root, branch=None)
        w['cwd'] = root
        for t in b['tasks']:
            if t['owner'] == w['name'] and t['status'] in ('review', 'doing'):
                t['status'] = 'done'
    room_add(b, 'Ты', 'all', 'Слил ветку %s в %s' % (branch, info['base']))
    add_log('Ты', 'слил ветку %s' % branch, 'done', proj)
    lead = agents.get(b['lead'] or '')
    if lead and lead is not w:
        deliver(lead, 'Человек слил ветку %s (%s) в %s.' % (branch, w['name'] if w else '?', info['base']), 'Ты')


def return_branch(proj, branch, note):
    review(proj, branch)
    w = next((a for a in agents.values() if a.get('managed') and a['managed'].get('branch') == branch), None)
    if not w:
        raise RuntimeError('исполнитель этой ветки ушёл из офиса')
    for t in board_of(proj)['tasks']:
        if t['owner'] == w['name'] and t['status'] == 'review':
            t['status'] = 'doing'
    room_add(board_of(proj), 'Ты', w['name'], 'Ревью: ' + note)
    deliver(w, 'Ревью от человека по твоей ветке %s: %s' % (branch, note), 'Ты')


def start_mission(proj, goal, kind='claude'):
    host, path = place_of(proj)
    if not path:
        raise RuntimeError('у этажа %s нет папки ни на Маке, ни на подключённом сервере' % proj)
    if len(goal.split()) < 2:
        raise RuntimeError('опиши цель хотя бы парой слов')
    b = board_of(proj)
    lead = agents.get(b['lead'] or '')
    if not (lead and lead.get('managed')) and (kind not in ('claude', 'codex') or kind not in host_kinds(host)):
        raise RuntimeError('%s недоступен там, где лежит проект (%s)' % (kind, host))   # checked before the board turns active
    b.update(goal=short(goal, 1000), status='active', report='', wakes=0)
    if lead and lead.get('managed'):
        deliver(lead, 'Новая цель команды: ' + goal, 'Ты')
        return lead
    lead = hire(kind, path, '', name='Диспетчер·' + proj, project=proj, mission={'role': 'lead'}, host=host,
                system=LEAD_PROMPT.format(proj=proj, goal=goal, team=team_text(host)))
    b['lead'] = lead['id']
    send_to(lead, 'Цель: %s\nНачни: разбей на задачи и найми исполнителей.' % goal)
    add_log('Ты', 'поставил цель этажу %s' % proj, 'working')
    return lead


def set_place(proj, host, path):
    if '/.office-worktrees/' in str(path):      # an agent's own checkout is never the project folder
        return
    p = projects[proj]
    if host == LOCAL_HOST:
        p['path'] = path
    else:
        p['remote'] = {'host': host, 'path': path}


def place_of(proj):
    """(host, path) where agents of this project can run: this Mac first, else a connected runner."""
    p = projects.get(proj) or {}
    if p.get('path') and os.path.isdir(p['path']):
        return LOCAL_HOST, p['path']
    r = p.get('remote') or {}
    if r.get('host') and runner_online(r['host']):
        return r['host'], r['path']
    return None, None


_limits = {'at': 0, 'val': {}}
_usage = {'at': 0, 'val': {}}


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
        if task and task != a.get('lastLine'):
            chat_add(a, 'bot', '%s · %s' % (ST_RU.get(status, status), task))
            a['lastLine'] = task
        was = a['status']
        set_status(a, status, task or a['task'])
        lead = agents.get((a.get('mission') or {}).get('lead') or '')
        if lead and status in ('done', 'error') and was != status:
            deliver(lead, '%s (Grok Bot) %s: %s' % (a['name'], ST_RU[status], task), 'Офис')
        mark_dirty()


def grok_send(bot, text, sender='Ты'):
    """Message for a Grok bot: published to the inbox channel (topic + '-in') the bots check."""
    with lock:
        topic, server = settings['ntfyTopic'], settings['ntfyServer'].rstrip('/')
        chat_add(bot, 'me' if sender == 'Ты' else 'peer', text if sender == 'Ты' else '%s: %s' % (sender, text))
        mark_dirty()
    if not topic:
        raise RuntimeError('канал Grok не включён')
    body = ('Кому: %s | от: %s | %s' % (bot['name'], sender, text)).encode('utf-8')
    req = urllib.request.Request('%s/%s-in' % (server, topic), data=body, headers={'User-Agent': 'ai-office-hub', 'Title': 'ИИ-Офис'})
    urllib.request.urlopen(req, timeout=15).close()


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


def badges(a):
    out = []
    if a.get('night'):
        out.append('Ночная смена')
    if a['steps'] >= 100:
        out.append('Сотня шагов')
    if a['tasks'] >= 5 and not a.get('errors'):
        out.append('Без единой ошибки')
    if a.get('msgs', 0) >= 3:
        out.append('Командный игрок')
    if (a.get('mission') or {}).get('role') == 'lead':
        out.append('Диспетчер')
    return out


WEATHER_URL = 'https://api.open-meteo.com/v1/forecast?latitude=55.7558&longitude=37.6173&current=temperature_2m,weather_code,is_day'


def weather_loop():
    while True:
        try:
            with urllib.request.urlopen(urllib.request.Request(WEATHER_URL, headers={'User-Agent': 'ai-office-hub'}), timeout=20) as r:
                cur = json.loads(r.read()).get('current') or {}
            weather.update(temp=cur.get('temperature_2m'), code=cur.get('weather_code'), day=bool(cur.get('is_day')), at=time.time())
            mark_dirty()
        except Exception as e:   # offline or VPN down: keep the last value
            print('weather failed:', e, flush=True)
        time.sleep(1800)


def make_briefing():
    """Morning stand-up: what everyone did since the previous one."""
    rows, total_tasks, total_steps, errors = [], 0, 0, 0
    for a in agents.values():
        dt, ds, de = a['tasks'] - a.get('bTasks', 0), a['steps'] - a.get('bSteps', 0), a.get('errors', 0) - a.get('bErrors', 0)
        a.update(bTasks=a['tasks'], bSteps=a['steps'], bErrors=a.get('errors', 0))
        total_tasks, total_steps, errors = total_tasks + dt, total_steps + ds, errors + de
        if dt or ds:
            rows.append((dt, ds, a['name']))
    rows.sort(reverse=True)
    waiting = [a['name'] for a in agents.values() if a['status'] == 'waiting']
    if not rows:
        text = 'Планёрка: за сутки в офисе было тихо. Агентов на этажах: %d.' % len(agents)
    else:
        text = 'Планёрка: за сутки %d агентов взяли %d задач и сделали %d шагов. Больше всех — %s (%d задач). Ошибок: %d.%s' % (
            len(rows), total_tasks, total_steps, rows[0][2], rows[0][0], errors, (' Ждут тебя: ' + ', '.join(waiting) + '.') if waiting else '')
    briefing.update(text=text, day=time.strftime('%Y-%m-%d'))
    add_log('Офис', text, 'done')


_integr = {'at': 0, 'val': {}}


def integrations():
    if time.time() - _integr['at'] > 30:
        val = {}
        for k, path in (('claude', '~/.claude/settings.json'), ('codex', '~/.codex/hooks.json'), ('gemini', '~/.gemini/config/hooks.json'), ('cursor', '~/.cursor/hooks.json')):
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
        projs = sorted((p for p in projects.values() if p['name'] != CHAT_PROJECT and (p['name'] in live_projects or time.time() - p['last'] < PROJECT_TTL)),
                       key=lambda p: p['first'])
        pub = []
        for a in agents.values():
            ap = approvals.get(a['approval'] or '')
            pub.append({'id': a['id'], 'name': a['name'], 'kind': LOOK.get(a['src'], a['src']), 'status': a['status'], 'task': a['task'],
                        'project': a['project'], 'since': a['since'], 'steps': a['steps'], 'tasks': a['tasks'],
                        'events': a['events'], 'hist': a['hist'],
                        'approval': ap['id'] if ap and ap['live'] and not ap['decision'] else None,
                        'ask': ap.get('ask') if ap and ap['live'] and not ap['decision'] else None,
                        'where': (a['host'] + ' · ' if a['host'] else '') + a['cwd'].replace(os.path.expanduser('~'), '~'),
                        'managed': bool(a.get('managed')), 'running': a['id'] in RUNS, 'chatLen': len(a.get('chat') or []),
                        'forkable': continuable(a) == '',
                        'xp': a['tasks'] * 10 + a['steps'], 'badges': badges(a), 'errors': a.get('errors', 0),
                        'chatOnly': bool(a.get('chatOnly')), 'last': a['last'], 'reply': last_bot(a), 'scope': a.get('scope', ''), 'look': a.get('look') or {}})
        pub.sort(key=lambda x: x['name'])
        ntfy = {'on': settings['ntfyOn'], 'state': ntfy_state['state'], 'last': ntfy_state['last'],
                'url': settings['ntfyServer'].rstrip('/') + '/' + settings['ntfyTopic'] if settings['ntfyTopic'] else ''}
        return {'now': time.time(), 'viewers': len(clients), 'settings': {'approveWait': settings['approveWait'], 'agentProxy': settings['agentProxy'], 'budget': settings.get('budget', 90)},
                'integrations': integrations(), 'ntfy': ntfy,
                'projects': [{'name': p['name'], 'color': p['color'], 'path': p.get('path') or (p.get('remote') or {}).get('path', ''),
                              'host': LOCAL_HOST if p.get('path') else (p.get('remote') or {}).get('host', '')} for p in projs],
                'agents': pub, 'log': log[:40], 'limits': cached_limits(), 'weather': weather, 'briefing': briefing,
                'boards': {k: {'goal': v['goal'], 'status': v['status'], 'report': v['report'], 'lead': v['lead'],
                               'tasks': [{x: t[x] for x in ('id', 'title', 'status', 'owner', 'note')} for t in v['tasks']],
                               'room': [{x: m[x] for x in ('from', 'to', 'text', 't')} for m in v['room'][-20:]],
                               'branches': review_branches(k), 'local': place_of(k)[0] == LOCAL_HOST}
                           for k, v in boards.items() if k in {p['name'] for p in projs}},
                'kinds': host_kinds(LOCAL_HOST), 'broken': BROKEN, 'localHost': LOCAL_HOST,
                'hosts': {h: host_kinds(h) for h in [LOCAL_HOST] + [h for h in REMOTE if runner_online(h)]},
                'web': int(os.path.getmtime(os.path.join(WEB, 'index.html')))}


def save():
    with lock:
        data = {'agents': agents, 'projects': projects, 'log': log, 'settings': settings, 'boards': boards, 'briefing': briefing, 'broken': BROKEN}
        txt = json.dumps(data, ensure_ascii=False)
        lines, JOURNAL[:] = list(JOURNAL), []
    os.makedirs(os.path.join(DATA, 'journal'), exist_ok=True)
    for r in lines:
        with open(os.path.join(DATA, 'journal', time.strftime('%Y-%m-%d', time.localtime(r['t'])) + '.jsonl'), 'a', encoding='utf-8') as f:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')
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
    boards.update(data.get('boards', {}))
    briefing.update(data.get('briefing', {}))
    BROKEN.update(data.get('broken', {}))
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
        if time.time() - last_save > 5 or not dirty.is_set():
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
            if time.localtime().tm_hour >= 9 and settings.get('briefDay') != time.strftime('%Y-%m-%d'):
                settings['briefDay'] = time.strftime('%Y-%m-%d')
                make_briefing()
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
            for jid, proc in list(RJOBS.items()):
                if not runner_online(proc.host) and time.time() - (REMOTE.get(proc.host) or {}).get('last', 0) > 120:
                    proc.feed(['исполнитель на %s пропал со связи' % proc.host])
                    proc.finish(-2)
                    RJOBS.pop(jid, None)
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
        if path == '/api/review':
            q = parse_qs(urlparse(self.path).query)
            try:
                with lock:
                    return self._json(200, review((q.get('project') or [''])[0], (q.get('branch') or [''])[0]))
            except (RuntimeError, OSError, subprocess.SubprocessError) as e:
                return self._json(400, {'error': str(e)})
        if path == '/api/digest':
            day = (parse_qs(urlparse(self.path).query).get('day') or [time.strftime('%Y-%m-%d')])[0]
            if not re.match(r'^\d{4}-\d{2}-\d{2}$', day):
                return self._json(400, {'error': 'день в формате ГГГГ-ММ-ДД'})
            d = digest(day)
            try:
                tok = usage.scan_day(day)
            except Exception:
                tok = {}
            with lock:
                for rn in REMOTE.values():
                    for k, n in ((rn.get('usage') or {}).get('days', {}).get(day) or {}).items():
                        tok[k] = tok.get(k, 0) + n
            d['tokens'] = tok
            return self._json(200, d)
        if path == '/api/usage':
            if time.time() - _usage['at'] > 300:
                try:
                    _usage.update(val=usage.scan(), at=time.time())
                except Exception as e:
                    print('usage failed:', e, flush=True)
            with lock:
                parts = {LOCAL_HOST: _usage['val']}
                parts.update({h: rn.get('usage') for h, rn in REMOTE.items() if rn.get('usage')})
            return self._json(200, {'total': usage.merge(*parts.values()), 'hosts': list(parts)})
        if path == '/api/dirs':
            dirs = [dict(d, host=LOCAL_HOST) for d in runners.scan_dirs()]
            with lock:
                for h, rn in REMOTE.items():
                    if runner_online(h):
                        dirs += [dict(d, host=h) for d in rn.get('dirs') or []]
            return self._json(200, {'dirs': dirs})
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
        if url.path == '/api/settings' and 'budget' in body:
            if body['budget'] not in (70, 80, 90, 100):
                return self._json(400, {'error': 'бюджет: 70, 80, 90 или 100'})
            with lock:
                settings['budget'] = body['budget']
            mark_dirty()
            return self._json(200, {'ok': True})
        if url.path == '/api/stopall':
            with lock:
                n = 0
                for run in RUNS.values():
                    if run['proc'].poll() is None:
                        run['stopped'] = True
                        run['proc'].terminate()
                        n += 1
                for b in boards.values():
                    b['wakes'] = MAX_WAKES + 1          # missions wait for a human message
                add_log('Ты', 'остановил всех агентов офиса (%d)' % n, 'resting')
                mark_dirty()
            return self._json(200, {'stopped': n})
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
        if url.path == '/api/runner/poll':
            host = str(body.get('host') or '')[:40]
            if not host or host == LOCAL_HOST:
                return self._json(400, {'error': 'bad host'})
            with lock:
                fresh = host in REMOTE and not runner_online(host)    # back after a real gap, not just the hub restarting
                rn = REMOTE.setdefault(host, {'jobs': queue.Queue()})
                rn.update(last=time.time(), kinds=[k for k in body.get('kinds') or [] if k in KINDS], dirs=(body.get('dirs') or [])[:80])
                if isinstance(body.get('usage'), dict):
                    rn['usage'] = body['usage']
                if fresh:
                    add_log('Офис', 'на связи исполнитель на ' + host, 'done')
                    mark_dirty()
            try:
                return self._json(200, {'job': rn['jobs'].get(timeout=20)})
            except queue.Empty:
                return self._json(200, {})
        if url.path in ('/api/runner/lines', '/api/runner/done'):
            with lock:
                proc = RJOBS.get(str(body.get('job') or ''))
                if proc and body.get('meta'):
                    a = agents.get(proc.agent)
                    if a and a.get('managed'):
                        a['managed'].update(path=body['meta'].get('cwd') or a['managed']['path'], branch=body['meta'].get('branch'))
                        a['cwd'] = a['managed']['path']
            if not proc:
                return self._json(404, {'error': 'unknown job'})
            if url.path == '/api/runner/lines':
                proc.feed(body.get('lines') or [])
            else:
                proc.finish(int(body.get('rc') or 0))
                with lock:
                    RJOBS.pop(proc.id, None)
            return self._json(200, {'ok': True})
        if url.path == '/api/look':
            name, color, hat = str(body.get('name') or '').strip()[:30], str(body.get('color') or ''), str(body.get('hat') or '')
            if color and not re.match(r'^#[0-9a-fA-F]{6}$', color):
                return self._json(400, {'error': 'цвет в виде #rrggbb'})
            if hat not in ('', 'crown', 'cap', 'tophat', 'ears', 'phones'):
                return self._json(400, {'error': 'неизвестная шапка'})
            with lock:
                a = agents.get(str(body.get('id') or ''))
                if not a:
                    return self._json(404, {'error': 'нет такого агента'})
                a['look'] = {'color': color, 'hat': hat}
                if name:
                    a['name'] = name
                mark_dirty()
            return self._json(200, {'ok': True})
        if url.path == '/api/clienterror':
            print('[page error]', time.strftime('%H:%M:%S'), str(body.get('msg') or '')[:3000], flush=True)
            return self._json(200, {'ok': True})
        if url.path == '/api/mcp':
            with lock:
                text, err = mcp_call(body)
                mark_dirty()
            return self._json(200, {'text': text, 'error': err})
        if url.path in ('/api/merge', '/api/return'):
            try:
                with lock:
                    if url.path == '/api/merge':
                        merge_branch(str(body.get('project') or ''), str(body.get('branch') or ''))
                    else:
                        note = str(body.get('text') or '').strip()
                        if not note:
                            return self._json(400, {'error': 'напиши, что поправить'})
                        return_branch(str(body.get('project') or ''), str(body.get('branch') or ''), note[:2000])
                    mark_dirty()
            except (RuntimeError, OSError, subprocess.SubprocessError) as e:
                return self._json(409, {'error': str(e)})
            return self._json(200, {'ok': True})
        if url.path == '/api/mission' and body.get('cancel'):
            with lock:
                b = boards.get(str(body.get('project') or ''))
                if b:
                    run = RUNS.get(b['lead'] or '')
                    if run:
                        run['stopped'] = True
                        run['proc'].terminate()
                    b.update(goal='', status='', report='', wakes=0)
                    mark_dirty()
            return self._json(200, {'ok': True})
        if url.path in ('/api/mission', '/api/board', '/api/room'):
            proj, text = str(body.get('project') or ''), str(body.get('text') or '').strip()[:2000]
            if not proj or not text:
                return self._json(400, {'error': 'нужны этаж и текст'})
            try:
                with lock:
                    b = board_of(proj)
                    if url.path == '/api/mission':
                        start_mission(proj, text, str(body.get('kind') or 'claude'))
                    elif url.path == '/api/board':
                        b['seq'] += 1
                        b['tasks'].append({'id': b['seq'], 'title': short(text, 140), 'details': '', 'status': 'todo', 'owner': '', 'note': '', 'by': 'Ты'})
                    else:
                        room_add(b, 'Ты', 'all', text)
                        b['wakes'] = 0                    # a human message lets the team run again
                        lead = agents.get(b['lead'] or '')
                        if lead:
                            deliver(lead, text, 'Ты')
                    mark_dirty()
            except (RuntimeError, OSError) as e:
                return self._json(400, {'error': str(e)})
            return self._json(200, {'ok': True})
        if url.path == '/api/grok/send':
            with lock:
                bot = agents.get(str(body.get('id') or ''))
            if not bot or bot['src'] != 'grok' or not str(body.get('text') or '').strip():
                return self._json(400, {'error': 'нужны Grok-бот и текст'})
            try:
                grok_send(bot, str(body['text']).strip()[:2000])
            except Exception as e:
                return self._json(502, {'error': 'не отправилось: %s' % e})
            return self._json(200, {'ok': True})
        if url.path in ('/api/hire', '/api/send', '/api/stop', '/api/fork', '/api/chatnew'):
            return self._office_run(url.path, body)
        if url.path == '/api/forgetproject':
            with lock:
                name = str(body.get('project') or '')
                busy = any(a['project'] == name for a in agents.values())
                if name in projects and not busy:
                    projects[name]['last'] = 0          # an empty floor leaves the tower now instead of after PROJECT_TTL
                    add_log('Ты', 'убрал этаж ' + name, 'resting')
                    mark_dirty()
            return self._json(200 if name in projects and not busy else 409, {'ok': not busy} if not busy else {'error': 'на этаже ещё есть агенты'})
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
                kind, host = body.get('kind'), str(body.get('host') or LOCAL_HOST)
                if kind not in host_kinds(host):
                    return self._json(400, {'error': 'этот агент недоступен на %s' % host})
                if host == LOCAL_HOST:
                    where = os.path.realpath(os.path.expanduser(str(body.get('path') or '')))
                    if not os.path.isdir(where) or not where.startswith(os.path.expanduser('~') + os.sep):
                        return self._json(400, {'error': 'папка должна существовать и лежать в домашней папке'})
                else:
                    where = str(body.get('path') or '')
                    if not where.startswith('/'):
                        return self._json(400, {'error': 'нужен полный путь к папке на ' + host})
                return self._json(200, {'id': hire(kind, where, text, host=host)['id']})
            if path == '/api/chatnew':
                kind = body.get('kind')
                host = str(body.get('host') or '') or next((h for h in [LOCAL_HOST] + list(REMOTE) if kind in host_kinds(h)), '')
                if not host or kind not in host_kinds(host):
                    return self._json(400, {'error': 'эта модель сейчас недоступна'})
                if not text:
                    return self._json(400, {'error': 'пустое сообщение'})
                where = str(body.get('path') or '').strip() or '~/.ai-office-chats'
                if host == LOCAL_HOST:
                    where = os.path.realpath(os.path.expanduser(where))
                    os.makedirs(os.path.expanduser('~/.ai-office-chats'), exist_ok=True)
                    if not os.path.isdir(where) or not where.startswith(os.path.expanduser('~') + os.sep):
                        return self._json(400, {'error': 'папка должна существовать и лежать в домашней папке'})
                c = hire(kind, where, '', name='%s · %s' % (MODEL_RU.get(kind, kind), short(text, 36)), project=CHAT_PROJECT, host=host)
                c.update(chatOnly=True, scope=str(body.get('path') or ''))
                c['managed']['chat'] = True
                send_to(c, text)
                return self._json(200, {'id': c['id']})
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
            if a.get('managed'):
                send_to(a, text)
                return self._json(200, {'ok': True, 'id': a['id']})
            b = continue_in_office(a, text)
            return self._json(200, {'ok': True, 'id': b['id']})
    except (RuntimeError, OSError) as e:
        return self._json(500, {'error': str(e)})


Handler._office_run = _office_run


def main():
    load()
    seed_journal()
    threading.Thread(target=broadcaster, daemon=True).start()
    threading.Thread(target=janitor, daemon=True).start()
    threading.Thread(target=ntfy_loop, daemon=True).start()
    threading.Thread(target=weather_loop, daemon=True).start()
    def stop(*_):                  # launchd restarts send SIGTERM: keep the last changes
        try:
            save()
        finally:
            os._exit(0)
    signal.signal(signal.SIGTERM, stop)
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    print('ИИ-Офис hub on http://%s:%d' % (HOST, PORT), flush=True)
    try:
        srv.serve_forever()
    finally:
        save()


if __name__ == '__main__':
    main()
