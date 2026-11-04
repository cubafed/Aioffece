#!/usr/bin/env python3
"""Add or remove the ИИ-Офис hook in Claude Code and Codex configs.

  python3 hooks.py install     # idempotent, backs up each file first
  python3 hooks.py uninstall   # removes only our entries
  python3 hooks.py status

Our entries are recognised by the path of office_hook.py; other hooks (Vibe Island, caveman,
context-mode) are left untouched.
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
