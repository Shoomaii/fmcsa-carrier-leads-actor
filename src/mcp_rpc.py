"""Lightweight Streamable HTTP MCP (JSON-RPC 2.0) server handler for Apify Server Actors."""

from __future__ import annotations

import json
import uuid
from typing import Any, Awaitable, Callable

from aiohttp import web

MCP_PROTOCOL_VERSION = '2025-06-18'

ToolHandler = Callable[[dict[str, Any], str | None], Awaitable[dict[str, Any]]]


def create_mcp_handler(
    *,
    server_name: str,
    server_version: str,
    tools: list[dict[str, Any]],
    handlers: dict[str, ToolHandler],
) -> Callable[[web.Request], Awaitable[web.Response]]:
    """Return an aiohttp handler implementing MCP Streamable HTTP JSON-RPC 2.0 on /mcp."""

    def _rpc_error(req_id: Any, code: int, message: str, status: int = 200) -> web.Response:
        return web.json_response(
            {'jsonrpc': '2.0', 'id': req_id, 'error': {'code': code, 'message': message}},
            status=status,
        )

    async def handle_mcp(request: web.Request) -> web.Response:
        if request.method == 'GET':
            return web.json_response({
                'name': server_name,
                'version': server_version,
                'protocol': 'mcp',
                'protocolVersion': MCP_PROTOCOL_VERSION,
                'tools': [t['name'] for t in tools],
            })
        if request.method == 'DELETE':
            return web.Response(status=204)

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return _rpc_error(None, -32700, 'Parse error: invalid JSON', status=400)

        if not isinstance(body, dict) or body.get('jsonrpc') != '2.0':
            return _rpc_error(body.get('id') if isinstance(body, dict) else None, -32600, 'Invalid JSON-RPC 2.0 request', status=400)

        method = body.get('method')
        req_id = body.get('id')
        params = body.get('params') or {}
        session_id = request.headers.get('mcp-session-id') or uuid.uuid4().hex
        headers = {'Mcp-Session-Id': session_id}

        if req_id is None and isinstance(method, str) and method.startswith('notifications/'):
            return web.Response(status=202, headers=headers)

        if method == 'initialize':
            return web.json_response({
                'jsonrpc': '2.0',
                'id': req_id,
                'result': {
                    'protocolVersion': MCP_PROTOCOL_VERSION,
                    'capabilities': {'tools': {'listChanged': False}},
                    'serverInfo': {'name': server_name, 'version': server_version},
                },
            }, headers=headers)

        if method == 'ping':
            return web.json_response({'jsonrpc': '2.0', 'id': req_id, 'result': {}}, headers=headers)

        if method == 'tools/list':
            return web.json_response({
                'jsonrpc': '2.0',
                'id': req_id,
                'result': {'tools': tools},
            }, headers=headers)

        if method == 'tools/call':
            tool_name = params.get('name')
            arguments = params.get('arguments') or {}
            if not isinstance(tool_name, str) or tool_name not in handlers:
                return _rpc_error(req_id, -32602, f'Unknown tool: {tool_name!r}')
            if not isinstance(arguments, dict):
                return _rpc_error(req_id, -32602, 'Tool arguments must be a JSON object.')
            request_id = request.headers.get('x-actor-request-id')
            try:
                result = await handlers[tool_name](arguments, request_id)
                return web.json_response({
                    'jsonrpc': '2.0',
                    'id': req_id,
                    'result': {
                        'content': [{'type': 'text', 'text': json.dumps(result, ensure_ascii=False)}],
                        'structuredContent': result,
                        'isError': False,
                    },
                }, headers=headers)
            except ValueError as exc:
                return web.json_response({
                    'jsonrpc': '2.0',
                    'id': req_id,
                    'result': {
                        'content': [{'type': 'text', 'text': str(exc)}],
                        'isError': True,
                    },
                }, headers=headers)
            except Exception as exc:  # noqa: BLE001
                return web.json_response({
                    'jsonrpc': '2.0',
                    'id': req_id,
                    'result': {
                        'content': [{'type': 'text', 'text': f'Tool error: {exc}'}],
                        'isError': True,
                    },
                }, headers=headers)

        return _rpc_error(req_id, -32601, f'Method not found: {method}')

    return handle_mcp
