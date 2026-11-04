"""Office-run agents: build the CLI command for each kind and turn its JSON stream into chat events.

Events yielded by parse(): ('session', id) · ('text', str) · ('tool', name, input) · ('done', ok, note)
"""
import json
import os
import subprocess

HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'office_hook.py')
MCP = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'office_mcp.py')
KINDS_RUNNABLE = ('claude', 'codex', 'antigravity')
BIN = {
    'claude': ['/usr/local/bin/claude', os.path.expanduser('~/.local/bin/claude')],
    'codex': ['/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex', '/usr/local/bin/codex', os.path.expanduser('~/.local/bin/codex')],
    'antigravity': [os.path.expanduser('~/.gemini/bin/agy')],
}
# Read-only commands an office Claude may run without asking; everything else in Bash asks in the office.
SAFE_BASH = ['Bash(git status*)', 'Bash(git diff*)', 'Bash(git log*)', 'Bash(git show*)', 'Bash(ls*)', 'Bash(cat*)',
             'Bash(rg *)', 'Bash(grep *)', 'Bash(pwd)', 'Bash(head *)', 'Bash(tail *)', 'Bash(wc *)',
             'Bash(git add *)', 'Bash(git commit *)', 'mcp__office']   # commits stay in the agent's own branch


PROJECT_MARKS = ('.git', 'CLAUDE.md', 'AGENTS.md', '.claude', 'package.json', 'pyproject.toml', 'requirements.txt', 'Cargo.toml', 'go.mod')


def scan_dirs():
    """Projects (git repos or folders with project files) in the usual places under the home folder."""
    home, out, seen = os.path.expanduser('~'), [], set()
    for d in [home] + [os.path.join(home, x) for x in ('Projects', 'Developer', 'code', 'src', 'Documents', 'Desktop')]:
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for n in names:
            p = os.path.join(d, n)
            if not n.startswith('.') and p not in seen and any(os.path.exists(os.path.join(p, m)) for m in PROJECT_MARKS):
                seen.add(p)
                out.append({'name': n, 'path': p})
    return out[:80]


def make_worktree(root, key):
    """A separate checkout on its own branch, so agents of one project never edit the same files."""
    git = lambda *a: subprocess.run(['git', '-C', root, *a], capture_output=True, text=True, timeout=30)
    if not os.path.isdir(os.path.join(root, '.git')) or git('rev-parse', '--verify', 'HEAD').returncode:
        return root, None
    tag = key.split(':')[-1][:8]
    branch, wt = 'office/' + tag, os.path.join(os.path.dirname(root), '.office-worktrees', os.path.basename(root), tag)
    r = git('worktree', 'add', '-b', branch, wt)
    return (wt, branch) if r.returncode == 0 else (root, None)


def binary(kind):
    return next((p for p in BIN.get(kind, []) if os.path.isfile(p)), None)


def claude_settings():
    # Only the office hook: user-level hooks (Vibe Island, caveman) are skipped via --setting-sources,
    # so an office agent's permission request is answered in the office and nowhere else.
    return json.dumps({'hooks': {'PermissionRequest': [{'hooks': [
        {'type': 'command', 'command': '/usr/bin/python3 %s claude' % HOOK, 'timeout': 900}]}]}})


def command(kind, text, sid, new_sid=None, fork=False, system='', chat=False, cwd=None, agent=''):
    exe = binary(kind)
    if not exe:
        raise RuntimeError('%s не установлен на этом Маке' % kind)
    if chat:
        return chat_command(kind, exe, text, sid, new_sid)
    if kind == 'claude':
        cmd = [exe, '-p', text, '--output-format', 'stream-json', '--verbose', '--permission-mode', 'acceptEdits',
               '--setting-sources', 'project,local', '--settings', claude_settings(),
               '--mcp-config', json.dumps({'mcpServers': {'office': {'command': '/usr/bin/python3', 'args': [MCP, 'claude'], 'env': {'OFFICE_AGENT_ID': agent}}}}),
               '--allowedTools', *SAFE_BASH]
        if system:
            cmd += ['--append-system-prompt', system]
        if sid:
            cmd += ['--resume', sid] + (['--fork-session'] if fork else [])
        elif new_sid:
            cmd += ['--session-id', new_sid]
        return cmd
    if kind == 'codex':
        # approval_policy=never: an office Codex works sandboxed in its own checkout and never stops to ask
        # Codex hands MCP servers a clean environment, so the caller's office id goes in explicitly
        # office Codex runs without the user's hooks (Vibe Island etc.): the office reads its stream directly
        mcp = ['-c', 'features.hooks=false', '-c', 'mcp_servers.office.env={OFFICE_AGENT_ID="%s"}' % agent, '-c', 'mcp_servers.office.command="/usr/bin/python3"', '-c', 'mcp_servers.office.args=["%s","codex"]' % MCP, '-c', 'mcp_servers.office.default_tools_approval_mode="approve"', '-c', 'approval_policy="never"']
        if sid:
            return [exe, 'exec', 'resume', sid, *mcp, '--json', '--skip-git-repo-check', text]
        first = (system + '\n\n' + text) if system else text     # codex exec has no system-prompt flag
        return [exe, 'exec', *mcp, '--json', '--skip-git-repo-check', '--sandbox', 'workspace-write', first]
    cmd = [exe, '-p', text, '--output-format', 'stream-json', '--mode', 'accept-edits']
    return cmd + (['--conversation', sid] if sid else [])


def chat_command(kind, exe, text, sid, new_sid):
    """Plain conversation: the model may read the project folder but never edits or runs anything."""
    if kind == 'claude':
        cmd = [exe, '-p', text, '--output-format', 'stream-json', '--verbose', '--permission-mode', 'dontAsk',
               '--setting-sources', 'project,local', '--allowedTools', 'Read', 'Grep', 'Glob', 'LS', 'WebSearch', 'WebFetch']
        return cmd + (['--resume', sid] if sid else ['--session-id', new_sid] if new_sid else [])
    if kind == 'codex':
        ro = ['-c', 'approval_policy="never"', '-c', 'features.hooks=false']
        if sid:
            return [exe, 'exec', 'resume', sid, *ro, '--json', '--skip-git-repo-check', text]
        return [exe, 'exec', *ro, '--json', '--skip-git-repo-check', '--sandbox', 'read-only', text]
    return [exe, '-p', text, '--output-format', 'stream-json', '--mode', 'plan'] + (['--conversation', sid] if sid else [])


def _blocks(content):
    return content if isinstance(content, list) else [{'type': 'text', 'text': content}] if isinstance(content, str) else []


def parse(kind, line):
    try:
        ev = json.loads(line)
    except ValueError:
        return []          # plain-text noise ("Reading additional input…", errors): the hub keeps it for error reports
    if not isinstance(ev, dict):
        return []
    t = ev.get('type') or ''
    if kind == 'claude':
        if t == 'system' and ev.get('subtype') == 'init':
            return [('session', ev.get('session_id'))]
        if t == 'assistant':
            out = []
            for b in _blocks((ev.get('message') or {}).get('content')):
                if b.get('type') == 'text' and b.get('text', '').strip():
                    out.append(('text', b['text'].strip()))
                elif b.get('type') == 'tool_use':
                    out.append(('tool', b.get('name') or '', b.get('input') or {}))
            return out
        if t == 'result':
            return [('done', not ev.get('is_error'), ev.get('result') if ev.get('is_error') else '')]
        return []
    if kind == 'codex':
        if t == 'thread.started':
            return [('session', ev.get('thread_id'))]
        item = ev.get('item') or {}
        it = item.get('type')
        if t == 'item.completed' and it == 'agent_message':
            return [('text', item.get('text') or '')]
        if t == 'item.started' and it == 'command_execution':
            return [('tool', 'Bash', {'command': item.get('command') or ''})]
        if t == 'item.completed' and it == 'file_change':
            files = [c.get('path') for c in item.get('changes') or [] if c.get('path')]
            return [('tool', 'Edit', {'file_path': files[0] if files else ''})]
        if t == 'turn.completed':
            return [('done', True, '')]
        if t in ('turn.failed', 'error'):
            err = ev.get('error') or {}
            return [('done', False, err.get('message') if isinstance(err, dict) else str(err or ev.get('message') or 'ошибка'))]
        return []
    # Antigravity stream-json: tolerant, the shape is not documented publicly.
    out = []
    sid = ev.get('conversationId') or ev.get('conversation_id') or ev.get('session_id')
    if sid:
        out.append(('session', sid))
    call = ev.get('toolCall') or {}
    if call.get('name'):
        args = call.get('args') or {}
        out.append(('tool', call['name'], {'command': args.get('CommandLine'), 'file_path': args.get('TargetFile') or args.get('AbsolutePath')}))
    text = ev.get('text') or ev.get('content') or ev.get('result')
    if isinstance(text, str) and text.strip() and t not in ('user', 'user_message'):
        out.append(('text', text.strip()))
    if t in ('result', 'done', 'final') or ev.get('is_final'):
        out.append(('done', not ev.get('is_error') and not ev.get('error'), ev.get('error') or ''))
    return out
