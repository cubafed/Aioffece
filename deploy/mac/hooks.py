#!/usr/bin/env python3
"""Add or remove the ИИ-Офис hook in Claude Code and Codex configs.

  python3 hooks.py install     # idempotent, backs up each file first
  python3 hooks.py uninstall   # removes only our entries
  python3 hooks.py status

Our entries are recognised by the path of office_hook.py; other hooks (Vibe Island, caveman,
context-mode) are left untouched. Antigravity (agy, Gemini app, IDE) uses its own format: one named
entry «ai-office» in ~/.gemini/config/hooks.json.
"""
import json
import os
import shutil
import sys
import time

HOOK = os.path.expanduser('~/ai-office/hub/office_hook.py')
MARK = 'ai-office/hub/office_hook.py'
TARGETS = {
    'claude': (os.path.expanduser('~/.claude/settings.json'),
               ['SessionStart', 'UserPromptSubmit', 'PreToolUse', 'PostToolUse', 'PostToolUseFailure',
                'PermissionRequest', 'Notification', 'Stop', 'StopFailure', 'SessionEnd']),
    'codex': (os.path.expanduser('~/.codex/hooks.json'),
              ['SessionStart', 'UserPromptSubmit', 'PreToolUse', 'PostToolUse', 'PermissionRequest', 'Stop']),
}


AGY_PATH = os.path.expanduser('~/.gemini/config/hooks.json')
AGY_NAME = 'ai-office'


def agy_entry():
    h = lambda ev: {'type': 'command', 'command': '/usr/bin/python3 %s antigravity %s' % (HOOK, ev), 'timeout': 5}
    # No PreToolUse: Antigravity fails closed on it, the office only observes Gemini.
    return {'SessionStart': [h('SessionStart')], 'PreInvocation': [h('PreInvocation')],
            'PostToolUse': [{'matcher': '*', 'hooks': [h('PostToolUse')]}], 'Stop': [h('Stop')]}


def apply_agy(mode):
    if not os.path.isdir(os.path.expanduser('~/.gemini')):
        print('antigravity: not installed, skipped')
        return
    data = load(AGY_PATH)
    if mode == 'status':
        print('antigravity: %s in %s' % ('installed' if AGY_NAME in data else 'not installed', AGY_PATH))
        return
    if mode == 'uninstall' and AGY_NAME not in data:
        return
    data.pop(AGY_NAME, None)
    if mode == 'install':
        data[AGY_NAME] = agy_entry()
    os.makedirs(os.path.dirname(AGY_PATH), exist_ok=True)
    write(AGY_PATH, data)
    print('antigravity: %s (%s)' % ('installed' if mode == 'install' else 'removed', AGY_PATH))


def ours(group):
    return any(MARK in (h.get('command') or '') for h in group.get('hooks', []))


def entry(src, event):
    return {'hooks': [{'type': 'command', 'command': '/usr/bin/python3 %s %s' % (HOOK, src),
                       'timeout': 140 if event == 'PermissionRequest' else 5}]}


def load(path):
    if not os.path.exists(path):
        return {}
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def write(path, data):
    if os.path.exists(path):
        shutil.copy2(path, path + time.strftime('.bak-aioffice-%Y%m%d-%H%M%S'))
    tmp = path + '.tmp-aioffice'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write('\n')
    os.replace(tmp, path)


def apply(mode):
    for src, (path, events) in TARGETS.items():
        if not os.path.isdir(os.path.dirname(path)):
            print('%s: not installed, skipped' % src)
            continue
        data = load(path)
        hooks = data.setdefault('hooks', {})
        changed = False
        for ev in list(hooks):
            keep = [g for g in hooks[ev] if not ours(g)]
            if len(keep) != len(hooks[ev]):
                changed = True
                hooks[ev] = keep
            if not hooks[ev]:
                del hooks[ev]
        if mode == 'install':
            for ev in events:
                hooks.setdefault(ev, []).append(entry(src, ev))
            changed = True
        if mode == 'status':
            n = sum(ours(g) for gs in load(path).get('hooks', {}).values() for g in gs)
            print('%s: %d office hook entries in %s' % (src, n, path))
            continue
        if changed:
            write(path, data)
            print('%s: %s (%s)' % (src, 'installed' if mode == 'install' else 'removed', path))


if __name__ == '__main__':
    mode = sys.argv[1] if len(sys.argv) > 1 else 'status'
    if mode not in ('install', 'uninstall', 'status'):
        sys.exit(__doc__)
    apply(mode)
    apply_agy(mode)
