#!/usr/bin/env python3
"""Hook client for Claude Code and Codex: forwards the hook event to the ИИ-Офис hub.

Usage in hook config: /usr/bin/python3 ~/ai-office/hub/office_hook.py claude|codex
                      /usr/bin/python3 ~/ai-office/hub/office_hook.py antigravity <Event>

Never gets in the agent's way: any error (hub down, bad input) exits 0 with no output, which
leaves the agent's normal flow unchanged. Only a PermissionRequest answered in the office
prints a decision.
"""
import json
import os
import sys
import urllib.request

URL = os.environ.get('AI_OFFICE_URL', 'http://127.0.0.1:4747')
AGY_ARGS = {'CommandLine': 'command', 'TargetFile': 'file_path', 'AbsolutePath': 'file_path', 'File': 'file_path',
            'SearchPath': 'path', 'DirectoryPath': 'path', 'Query': 'query', 'Pattern': 'pattern', 'Url': 'url'}
KEEP = ('command', 'description', 'file_path', 'notebook_path', 'path', 'pattern', 'query', 'url', 'cmd', 'subagent_type')


def project_of(cwd):
    d = os.path.abspath(cwd)
    while True:
        if os.path.exists(os.path.join(d, '.git')):
            return os.path.basename(d) or d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    if os.path.abspath(cwd) == os.path.expanduser('~'):
        return 'Дом'
    return os.path.basename(cwd.rstrip('/')) or cwd


def from_antigravity(p, event):
    """Antigravity (agy CLI, Gemini app, IDE) sends camelCase protojson: map it to the Claude field names."""
    ws = p.get('workspacePaths') or []
    call = p.get('toolCall') or {}
    args = call.get('args') or {}
    return {'hook_event_name': event, 'session_id': p.get('conversationId'), 'cwd': ws[0] if ws else '',
            'prompt': p.get('lastUserInput') or '', 'tool_name': call.get('name') or '',
            'tool_input': {AGY_ARGS[k]: v for k, v in args.items() if k in AGY_ARGS},
            'invocation': p.get('invocationNum'), 'stop_reason': p.get('terminationReason') or ''}


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else 'claude'
    raw = sys.stdin.read()
    p = json.loads(raw) if raw.strip() else {}
    if not isinstance(p, dict):
        return
    if src == 'antigravity':
        p = from_antigravity(p, sys.argv[2] if len(sys.argv) > 2 else '')
    cwd = p.get('cwd') or ('' if src == 'antigravity' else os.getcwd())
    p['cwd'] = cwd
    root = os.environ.get('CLAUDE_PROJECT_DIR') if src == 'claude' else None   # where the session started, not the current cd
    p['project'] = project_of(root or cwd) if (root or cwd) else 'Antigravity'
    p['host'] = os.uname().nodename.split('.')[0]
    for big in ('tool_response', 'transcript_path', 'permission_suggestions'):
        p.pop(big, None)
    ti = p.get('tool_input')
    if isinstance(ti, dict):
        p['tool_input'] = {k: (v[:300] if isinstance(v, str) else v) for k, v in ti.items() if k in KEEP}
    if isinstance(p.get('prompt'), str):
        p['prompt'] = p['prompt'][:400]
    perm = p.get('hook_event_name') == 'PermissionRequest'
    req = urllib.request.Request(URL + '/api/hook?src=' + src, data=json.dumps(p).encode('utf-8'),
                                 headers={'Content-Type': 'application/json', 'X-Office': '1'})
    with urllib.request.urlopen(req, timeout=135 if perm else 1.5) as r:
        out = json.loads(r.read() or b'{}')
    decision = out.get('decision')
    if perm and decision in ('allow', 'deny'):
        d = {'behavior': decision}
        if decision == 'deny':
            d['message'] = 'Отклонено в ИИ-Офисе'
        sys.stdout.write(json.dumps({'hookSpecificOutput': {'hookEventName': 'PermissionRequest', 'decision': d}}))


try:
    main()
except Exception:  # fail open: the office must never block or break an agent
    pass
if len(sys.argv) > 1 and sys.argv[1] == 'antigravity':
    sys.stdout.write('{}')   # Antigravity expects a JSON object back from every hook
sys.exit(0)
