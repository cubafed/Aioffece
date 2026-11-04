"""Token usage per day, model and project, counted from the agents' own logs on this machine.

Claude Code: ~/.claude/projects/<dir>/<session>.jsonl — every assistant message carries message.usage.
Codex: ~/.codex/sessions/YYYY/MM/DD/*.jsonl — token_count events carry last_token_usage.
Results are cached per file (mtime, size), so a rescan only reads files that changed.
"""
import glob
import json
import os
import time

DAYS = 14
_cache = {}   # path -> (mtime, size, {(day, project): tokens}, kind)


def _day(ts):
    return str(ts or '')[:10]


def _claude_file(path):
    out, seen = {}, set()
    enc = os.path.basename(os.path.dirname(path))                   # Claude names it after the path: /home/a/x -> -home-a-x
    home = os.path.expanduser('~').replace('/', '-').replace('.', '-')
    proj = 'Дом' if enc == home else enc[len(home) + 1:] if enc.startswith(home + '-') else enc.strip('-')
    with open(path, 'rb') as f:
        for raw in f:
            if b'"usage"' not in raw or b'"assistant"' not in raw:
                continue
            try:
                e = json.loads(raw)
            except ValueError:
                continue
            msg = e.get('message') or {}
            mid, u = msg.get('id'), msg.get('usage') or {}
            if not u or mid in seen:
                continue
            seen.add(mid)
            n = (u.get('input_tokens') or 0) + (u.get('output_tokens') or 0) + (u.get('cache_creation_input_tokens') or 0)
            key = (_day(e.get('timestamp')), proj)
            out[key] = out.get(key, 0) + n
    return out


def _codex_file(path):
    out, proj = {}, 'codex'
    with open(path, 'rb') as f:
        for i, raw in enumerate(f):
            if i == 0 and b'"cwd"' in raw:
                try:
                    proj = os.path.basename((json.loads(raw).get('payload') or {}).get('cwd', '').rstrip('/')) or proj
                except ValueError:
                    pass
            if b'"token_count"' not in raw:
                continue
            try:
                e = json.loads(raw)
            except ValueError:
                continue
            u = ((e.get('payload') or {}).get('info') or {}).get('last_token_usage') or {}
            n = (u.get('input_tokens') or 0) + (u.get('output_tokens') or 0)
            if n:
                key = (_day(e.get('timestamp')), proj)
                out[key] = out.get(key, 0) + n
    return out


def scan():
    """{'claude': {day: n}, 'codex': {day: n}, 'projects': {project: n}} for the last DAYS days."""
    since = time.time() - DAYS * 86400
    home = os.path.expanduser('~')
    files = [(p, 'claude') for p in glob.glob(os.path.join(home, '.claude/projects/*/*.jsonl'))] + \
            [(p, 'codex') for p in glob.glob(os.path.join(home, '.codex/sessions/*/*/*/*.jsonl'))]
    first = time.strftime('%Y-%m-%d', time.localtime(since))
    res = {'claude': {}, 'codex': {}, 'projects': {}}
    for path, kind in files:
        try:
            st = os.stat(path)
        except OSError:
            continue
        if st.st_mtime < since:
            continue
        hit = _cache.get(path)
        if not hit or hit[0] != st.st_mtime or hit[1] != st.st_size:
            try:
                data = _claude_file(path) if kind == 'claude' else _codex_file(path)
            except OSError:
                continue
            hit = _cache[path] = (st.st_mtime, st.st_size, data, kind)
        for (day, proj), n in hit[2].items():
            if day >= first:
                res[kind][day] = res[kind].get(day, 0) + n
                res['projects'][proj] = res['projects'].get(proj, 0) + n
    return res


def merge(*parts):
    out = {'claude': {}, 'codex': {}, 'projects': {}}
    for part in parts:
        for k in out:
            for key, n in (part or {}).get(k, {}).items():
                out[k][key] = out[k].get(key, 0) + n
    return out
