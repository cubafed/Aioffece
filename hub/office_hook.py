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


def root_of(cwd):
    """The git root above cwd, else cwd itself. A linked worktree counts as its main repository."""
    d = os.path.abspath(cwd)
    while True:
        g = os.path.join(d, '.git')
        if os.path.isfile(g):
            try:
                with open(g) as f:
                    line = f.read().strip()
            except OSError:
                return d
            return line[7:].strip().split('/.git/worktrees/')[0] if line.startswith('gitdir:') and '/.git/worktrees/' in line else d
        if os.path.isdir(g):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return os.path.abspath(cwd)
        d = parent


def project_of(cwd):
    root = root_of(cwd)
    if root == os.path.expanduser('~'):
        return 'Дом'
    return os.path.basename(root.rstrip('/')) or root


def last_reply(path):
    """Text of the last assistant turn in a Claude transcript, read from the end of the file."""
    size = os.path.getsize(path)
    with open(path, 'rb') as f:
        f.seek(max(0, size - 400000))
        lines = f.read().decode('utf-8', 'replace').splitlines()
    texts = []
    for line in reversed(lines):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        msg = e.get('message') or {}
        content = msg.get('content')
        if e.get('type') == 'user':
            if isinstance(content, str) or any(isinstance(b, dict) and b.get('type') == 'text' for b in content or []):
                break                      # the prompt that started this turn
            continue                       # tool results
        if e.get('type') == 'assistant':
            for b in reversed(content if isinstance(content, list) else []):
                if isinstance(b, dict) and b.get('type') == 'text' and b.get('text', '').strip():
                    texts.append(b['text'].strip())
    return '\n\n'.join(reversed(texts))


def from_antigravity(p, event):
    """Antigravity (agy CLI, Gemini app, IDE) sends camelCase protojson: map it to the Claude field names."""
    ws = p.get('workspacePaths') or []
    call = p.get('toolCall') or {}
    args = call.get('args') or {}
    return {'hook_event_name': event, 'session_id': p.get('conversationId'), 'cwd': ws[0] if ws else '',
            'prompt': p.get('lastUserInput') or '', 'tool_name': call.get('name') or '',
            'tool_input': {AGY_ARGS[k]: v for k, v in args.items() if k in AGY_ARGS},
            'invocation': p.get('invocationNum'), 'stop_reason': p.get('terminationReason') or ''}


CURSOR_EVENTS = {'beforeSubmitPrompt': 'UserPromptSubmit', 'afterShellExecution': 'PreToolUse', 'afterFileEdit': 'PreToolUse',
                 'afterMCPExecution': 'PreToolUse', 'afterAgentResponse': 'AgentReply', 'stop': 'Stop', 'sessionStart': 'SessionStart', 'sessionEnd': 'SessionEnd'}


def from_cursor(p):
    """Cursor (and Cursor-based Grok Bot) hooks → the Claude field names."""
    ev = p.get('hook_event_name') or ''
    roots = p.get('workspace_roots') or []
    out = {'hook_event_name': CURSOR_EVENTS.get(ev, ev), 'session_id': p.get('conversation_id'), 'cwd': roots[0] if roots else '',
           'prompt': p.get('prompt') or ''}
    if ev == 'afterShellExecution':
        out.update(tool_name='Bash', tool_input={'command': p.get('command') or ''})
    elif ev == 'afterFileEdit':
        out.update(tool_name='Edit', tool_input={'file_path': p.get('file_path') or ''})
    elif ev == 'afterMCPExecution':
        out.update(tool_name='mcp__%s__' % (p.get('tool_name') or 'tool'), tool_input={})
    elif ev == 'afterAgentResponse':
        out['reply'] = p.get('text') or ''
    elif ev == 'stop' and p.get('status') == 'error':
        out['hook_event_name'] = 'StopFailure'
    return out


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else 'claude'
    raw = sys.stdin.read()
    p = json.loads(raw) if raw.strip() else {}
    if not isinstance(p, dict):
        return
    if src == 'antigravity':
        p = from_antigravity(p, sys.argv[2] if len(sys.argv) > 2 else '')
    elif src == 'cursor':
        p = from_cursor(p)
    cwd = p.get('cwd') or ('' if src in ('antigravity', 'cursor') else os.getcwd())
    p['cwd'] = cwd
    start = os.environ.get('CLAUDE_PROJECT_DIR') if src == 'claude' else None   # where the session started, not the current cd
    p['project'] = project_of(start or cwd) if (start or cwd) else ('Antigravity' if src == 'antigravity' else 'Cursor')
    p['root'] = root_of(start or cwd) if (start or cwd) else ''
    if p.get('hook_event_name') == 'Stop':
        reply = p.get('last_assistant_message')
        if not reply and src == 'claude' and p.get('transcript_path') and os.path.isfile(p['transcript_path']):
            reply = last_reply(p['transcript_path'])
        if isinstance(reply, str) and reply.strip():
            p['reply'] = reply.strip()[-4000:]
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
    with urllib.request.urlopen(req, timeout=890 if perm else 2) as r:
        out = json.loads(r.read() or b'{}')
    decision = out.get('decision')
    if perm and decision in ('allow', 'deny'):
        d = {'behavior': decision}
        if decision == 'deny':
            d['message'] = 'Отклонено в ИИ-Офисе'
        sys.stdout.write(json.dumps({'hookSpecificOutput': {'hookEventName': 'PermissionRequest', 'decision': d}}))


if __name__ == '__main__':
    try:
        main()
    except Exception:  # fail open: the office must never block or break an agent
        pass
    if len(sys.argv) > 1 and sys.argv[1] == 'antigravity':
        sys.stdout.write('{}')   # Antigravity expects a JSON object back from every hook
    if len(sys.argv) > 2 and sys.argv[1] == 'cursor' and sys.argv[2] == 'beforeSubmitPrompt':
        sys.stdout.write('{"continue": true}')   # Cursor waits for this answer before sending the prompt
    sys.exit(0)
