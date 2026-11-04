#!/usr/bin/env python3
"""MCP server «office» (stdio): gives an agent the floor's shared board, team chat and hiring.

Each tool call is forwarded to the hub (POST /api/mcp). The hub knows who is calling from
OFFICE_AGENT_ID (set for office-run agents) or, for a terminal session, from the project folder.

Usage: /usr/bin/python3 ~/ai-office/hub/office_mcp.py claude|codex
"""
import json
import os
import sys
import urllib.request

URL = os.environ.get('AI_OFFICE_URL', 'http://127.0.0.1:4747')
SRC = sys.argv[1] if len(sys.argv) > 1 else 'claude'

S = lambda **p: {'type': 'object', 'properties': p, 'required': [k for k, v in p.items() if not v.pop('optional', False)]}
STR = lambda d, optional=False: {'type': 'string', 'description': d, 'optional': optional}
TOOLS = [
    ('office_team', 'Кто работает на этом этаже (проекте): имена, статусы, текущие задачи.', S()),
    ('office_board', 'Цель команды и доска задач этажа: id, статус, исполнитель.', S()),
    ('office_add_task', 'Добавить задачу на доску этажа.', S(title=STR('Коротко, что сделать'), details=STR('Подробности', True), assignee=STR('Имя исполнителя', True))),
    ('office_take_task', 'Взять задачу себе (статус doing).', S(id=STR('id задачи'))),
    ('office_update_task', 'Сменить статус задачи: todo, doing, review, done, blocked.', S(id=STR('id задачи'), status=STR('новый статус'), note=STR('Что сделано или что мешает', True))),
    ('office_send', 'Написать коллеге по имени, «all» — всей команде, «человек» — владельцу офиса.', S(to=STR('Кому'), text=STR('Текст'))),
    ('office_inbox', 'Новые сообщения для тебя.', S()),
    ('office_hire', 'Нанять исполнителя на этот этаж. Он работает в своей git-ветке.', S(kind=STR('claude или codex'), task=STR('Задача для него'), name=STR('Имя', True))),
    ('office_report', 'Итоговый отчёт по цели команды: что сделано, что осталось.', S(text=STR('Отчёт'))),
]


def hub(tool, args):
    body = {'tool': tool, 'args': args, 'src': SRC, 'agent': os.environ.get('OFFICE_AGENT_ID', ''), 'cwd': os.getcwd()}
    req = urllib.request.Request(URL + '/api/mcp', data=json.dumps(body).encode('utf-8'),
                                 headers={'Content-Type': 'application/json', 'X-Office': '1'})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            out = json.loads(r.read() or b'{}')
        return out.get('text', ''), bool(out.get('error'))
    except Exception as e:  # hub down: tell the model instead of crashing the session
        return 'ИИ-Офис недоступен: %s' % e, True


def reply(mid, result=None, error=None):
    msg = {'jsonrpc': '2.0', 'id': mid}
    msg.update({'error': error} if error else {'result': result})
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + '\n')
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        try:
            req = json.loads(line)
        except ValueError:
            continue
        mid, method, params = req.get('id'), req.get('method'), req.get('params') or {}
        if mid is None:
            continue                                  # notifications need no answer
        if method == 'initialize':
            reply(mid, {'protocolVersion': params.get('protocolVersion') or '2025-06-18', 'capabilities': {'tools': {}},
                        'serverInfo': {'name': 'ai-office', 'version': '1'}})
        elif method == 'tools/list':
            reply(mid, {'tools': [{'name': n, 'description': d, 'inputSchema': s} for n, d, s in TOOLS]})
        elif method == 'tools/call':
            text, err = hub(params.get('name'), params.get('arguments') or {})
            reply(mid, {'content': [{'type': 'text', 'text': text}], 'isError': err})
        elif method == 'ping':
            reply(mid, {})
        else:
            reply(mid, error={'code': -32601, 'message': 'unknown method ' + str(method)})


if __name__ == '__main__':
    main()
