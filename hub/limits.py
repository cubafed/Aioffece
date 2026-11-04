"""Plan limits (5-hour and weekly windows) for each provider.

Main source: Vibe Island's local usage database (read-only) — it already polls Anthropic, OpenAI/Codex,
Antigravity and Cursor. Fallback for Codex: the rate_limits that Codex writes into its own session logs.
"""
import glob
import json
import os
import sqlite3
import time

VIBE_DB = os.path.expanduser('~/.vibe-island/data/usage/usage.sqlite')
CODEX_SESSIONS = os.path.expanduser('~/.codex/sessions')
LABEL = {'anthropic': ('claude', 'Claude'), 'openai': ('codex', 'ChatGPT · Codex'),
         'antigravity': ('gemini', 'Gemini · Antigravity'), 'cursor': ('grok', 'Cursor · Grok Bot')}


def _window(w):
    if not isinstance(w, dict) or w.get('used_percentage') is None:
        return None
    return {'pct': round(float(w['used_percentage']), 1), 'resets': w.get('resets_at')}


def from_vibe_island():
    if not os.path.exists(VIBE_DB):
        return {}
    db = sqlite3.connect('file:%s?mode=ro' % VIBE_DB, uri=True, timeout=1)
    try:
        best = {}
        for prov, snap in db.execute('select provider, snapshot from usage_limit_records where closed_at is null'):
            try:
                s = json.loads(snap)
            except (TypeError, ValueError):
                continue
            if prov not in best or s.get('fetched_at', 0) > best[prov].get('fetched_at', 0):
                best[prov] = s
    finally:
        db.close()
    out = {}
    for prov, s in best.items():
        kind, label = LABEL.get(prov, (prov, prov))
        meta = s.get('metadata') or {}
        item = {'kind': kind, 'label': label, 'five': _window(s.get('five_hour')), 'week': _window(s.get('seven_day')),
                'fetched': s.get('fetched_at'), 'plan': meta.get('antigravity.plan') or meta.get('codex.planType') or ''}
        if prov == 'cursor' and meta.get('cursor.planLimitUSD'):
            used, cap = float(meta.get('cursor.planUsedUSD') or 0), float(meta['cursor.planLimitUSD'])
            item['month'] = {'pct': round(100 * used / cap, 1) if cap else 0, 'note': '$%.2f из $%.2f' % (used, cap)}
        out[kind] = item
    return out


def codex_fallback():
    files = sorted(glob.glob(os.path.join(CODEX_SESSIONS, '*/*/*/*.jsonl')), key=os.path.getmtime)[-3:]
    for path in reversed(files):
        with open(path, 'rb') as f:
            f.seek(max(0, os.path.getsize(path) - 400000))
            lines = f.read().decode('utf-8', 'replace').splitlines()
        for line in reversed(lines):
            if '"rate_limits"' not in line:
                continue
            try:
                rl = json.loads(line).get('payload', {}).get('rate_limits') or {}
            except ValueError:
                continue
            w = lambda x: {'pct': x.get('used_percent'), 'resets': x.get('resets_at')} if x else None
            return {'kind': 'codex', 'label': 'ChatGPT · Codex', 'five': w(rl.get('primary')), 'week': w(rl.get('secondary')),
                    'fetched': os.path.getmtime(path), 'plan': ''}
    return None


def read():
    try:
        out = from_vibe_island()
    except sqlite3.Error:
        out = {}
    if 'codex' not in out:
        fb = codex_fallback()
        if fb:
            out['codex'] = fb
    for v in out.values():
        v['stale'] = bool(v.get('fetched')) and time.time() - v['fetched'] > 3 * 3600
    return out
