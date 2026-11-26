"""Apify Standby HTTP Server & Native MCP Server for FMCSA Motor Carrier Leads."""

from __future__ import annotations

import asyncio
import signal
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from aiohttp import web
from apify import Actor

from .fmcsa import FmcsaClient, normalize_states, normalize_usdot
from .mcp_rpc import create_mcp_handler

CHARGED_EVENT = 'lead'
CACHE_TTL_SECONDS = 300.0
DEFAULT_LIMIT = 25
HARD_MAX_LIMIT = 250


def _parse_bool(value: Any, default: bool = False) -> bool:
    if value is None or value == '':
        return default
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in ('1', 'true', 'yes', 'on'):
        return True
    if s in ('0', 'false', 'no', 'off'):
        return False
    raise ValueError(f'Invalid boolean value: {value!r}')


def _parse_int(
    value: Any,
    name: str,
    minimum: int = 0,
    maximum: int | None = None,
    default: int | None = None,
) -> int | None:
    if value is None or value == '':
        return default
    try:
        n = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'"{name}" must be an integer.') from exc
    if n < minimum:
        raise ValueError(f'"{name}" must be >= {minimum}.')
    return min(n, maximum) if maximum is not None else n


async def _default_charge(event_name: str, count: int, request_id: str | None) -> Any:
    if count <= 0:
        return None
    kwargs: dict[str, Any] = {'count': count}
    if request_id:
        kwargs['request_id'] = request_id
        kwargs['idempotency_key'] = f'{request_id}:{event_name}'
    return await Actor.charge(event_name, **kwargs)


MCP_TOOLS: list[dict[str, Any]] = [
    {
        'name': 'search_new_carriers',
        'description': 'Search newly registered active US DOT FMCSA motor carriers (owner name, phone, email, fleet size, cargo types) from official data.transportation.gov census feeds.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'states': {'type': 'array', 'items': {'type': 'string'}, 'description': '2-letter US state codes (e.g. ["TX", "CA"])'},
                'daysBack': {'type': 'integer', 'default': 14, 'minimum': 1, 'maximum': 365},
                'minPowerUnits': {'type': 'integer', 'minimum': 0},
                'maxPowerUnits': {'type': 'integer', 'minimum': 0},
                'authorizedForHireOnly': {'type': 'boolean', 'default': False},
                'requireContact': {'type': 'boolean', 'default': True},
                'limit': {'type': 'integer', 'default': 25, 'minimum': 1, 'maximum': 250},
            },
        },
    },
    {
        'name': 'search_pending_revocations',
        'description': 'Find US motor carriers with pending FMCSA common or contract authority revocation, enriched with census officer/contact info and active/pending insurance policy details.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'states': {'type': 'array', 'items': {'type': 'string'}, 'description': '2-letter US state codes (e.g. ["TX", "FL"])'},
                'requireContact': {'type': 'boolean', 'default': True},
                'limit': {'type': 'integer', 'default': 25, 'minimum': 1, 'maximum': 250},
            },
        },
    },
    {
        'name': 'lookup_usdot',
        'description': 'Look up a single USDOT number across FMCSA Census, L&I Authority, and Active/Pending Insurance filings.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'dotNumber': {'type': 'string', 'description': 'Numeric USDOT number (e.g. "2247505")'},
            },
            'required': ['dotNumber'],
        },
    },
]


def create_app(
    client_factory: Callable[[], FmcsaClient] | None = None,
    charger: Callable[[str, int, str | None], Awaitable[Any]] | None = None,
    cache_ttl: float = CACHE_TTL_SECONDS,
) -> web.Application:
    make_client = client_factory or FmcsaClient
    do_charge = charger or _default_charge

    app = web.Application()
    holder: list[FmcsaClient | None] = [None]
    cache: dict[str, tuple[float, Any]] = {}

    def get_client() -> FmcsaClient:
        if holder[0] is None:
            holder[0] = make_client()
        return holder[0]

    async def on_cleanup(_app: web.Application) -> None:
        if holder[0] is not None:
            await holder[0].close()

    app.on_cleanup.append(on_cleanup)

    def _get_cached(key: str) -> Any | None:
        hit = cache.get(key)
        if hit and (time.monotonic() - hit[0]) < cache_ttl:
            return hit[1]
        return None

    def _set_cached(key: str, val: Any) -> None:
        if len(cache) > 256:
            cache.clear()
        cache[key] = (time.monotonic(), val)

    async def _run_new_carriers(args: dict[str, Any], request_id: str | None) -> dict[str, Any]:
        states = normalize_states(args.get('states'))
        days_back = _parse_int(args.get('daysBack'), 'daysBack', minimum=1, maximum=365, default=14) or 14
        min_units = _parse_int(args.get('minPowerUnits'), 'minPowerUnits', minimum=0, default=None)
        max_units = _parse_int(args.get('maxPowerUnits'), 'maxPowerUnits', minimum=0, default=None)
        if min_units is not None and max_units is not None and min_units > max_units:
            raise ValueError('"minPowerUnits" cannot be greater than "maxPowerUnits".')
        for_hire = _parse_bool(args.get('authorizedForHireOnly'), default=False)
        req_contact = _parse_bool(args.get('requireContact'), default=True)
        limit = _parse_int(args.get('limit'), 'limit', minimum=1, maximum=HARD_MAX_LIMIT, default=DEFAULT_LIMIT) or DEFAULT_LIMIT

        cache_key = f'new:{",".join(sorted(states))}:{days_back}:{min_units}:{max_units}:{for_hire}:{req_contact}:{limit}'
        leads = _get_cached(cache_key)
        if leads is None:
            leads = await get_client().search_new_authorities(
                states=states,
                days_back=days_back,
                min_power_units=min_units,
                max_power_units=max_units,
                authorized_for_hire_only=for_hire,
                require_contact=req_contact,
                limit=limit,
            )
            _set_cached(cache_key, leads)

        if leads:
            await do_charge(CHARGED_EVENT, len(leads), request_id)
        return {
            'mode': 'new_usdot_registrations',
            'states': states,
            'daysBack': days_back,
            'count': len(leads),
            'fetchedAt': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            'leads': leads,
        }

    async def _run_pending_revocations(args: dict[str, Any], request_id: str | None) -> dict[str, Any]:
        states = normalize_states(args.get('states'))
        req_contact = _parse_bool(args.get('requireContact'), default=True)
        limit = _parse_int(args.get('limit'), 'limit', minimum=1, maximum=HARD_MAX_LIMIT, default=DEFAULT_LIMIT) or DEFAULT_LIMIT

        cache_key = f'rev:{",".join(sorted(states))}:{req_contact}:{limit}'
        leads = _get_cached(cache_key)
        if leads is None:
            leads = await get_client().search_pending_revocations(
                states=states,
                require_contact=req_contact,
                limit=limit,
            )
            _set_cached(cache_key, leads)

        if leads:
            await do_charge(CHARGED_EVENT, len(leads), request_id)
        return {
            'mode': 'pending_revocations',
            'states': states,
            'count': len(leads),
            'fetchedAt': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            'leads': leads,
        }

    async def _run_lookup_usdot(args: dict[str, Any], request_id: str | None) -> dict[str, Any]:
        dot = normalize_usdot(args.get('dotNumber') or '')
        cache_key = f'dot:{dot}'
        profile = _get_cached(cache_key)
        if profile is None:
            profile = await get_client().lookup_usdot(dot)
            _set_cached(cache_key, profile)
        if profile:
            await do_charge(CHARGED_EVENT, 1, request_id)
        return {
            'mode': 'usdot_lookup',
            'dotNumber': dot,
            'found': profile is not None,
            'fetchedAt': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            'carrier': profile,
        }

    async def handle_health(_request: web.Request) -> web.Response:
        return web.json_response({'status': 'ok', 'service': 'fmcsa-carrier-leads', 'version': '1.0.0'})

    async def handle_new_authorities(request: web.Request) -> web.Response:
        if request.headers.get('x-apify-container-server-readiness-probe'):
            return web.json_response({'status': 'ready'})
        q = request.rel_url.query
        try:
            payload = await _run_new_carriers(
                {
                    'states': q.get('states'),
                    'daysBack': q.get('daysBack'),
                    'minPowerUnits': q.get('minPowerUnits'),
                    'maxPowerUnits': q.get('maxPowerUnits'),
                    'authorizedForHireOnly': q.get('authorizedForHireOnly'),
                    'requireContact': q.get('requireContact'),
                    'limit': q.get('limit'),
                },
                request.headers.get('x-actor-request-id'),
            )
            return web.json_response(payload)
        except ValueError as exc:
            return web.json_response({'error': str(exc)}, status=400)

    async def handle_pending_revocations(request: web.Request) -> web.Response:
        q = request.rel_url.query
        try:
            payload = await _run_pending_revocations(
                {
                    'states': q.get('states'),
                    'requireContact': q.get('requireContact'),
                    'limit': q.get('limit'),
                },
                request.headers.get('x-actor-request-id'),
            )
            return web.json_response(payload)
        except ValueError as exc:
            return web.json_response({'error': str(exc)}, status=400)

    async def handle_usdot_lookup(request: web.Request) -> web.Response:
        dot = request.match_info['dot_number']
        try:
            payload = await _run_lookup_usdot({'dotNumber': dot}, request.headers.get('x-actor-request-id'))
            status = 200 if payload['found'] else 404
            return web.json_response(payload, status=status)
        except ValueError as exc:
            return web.json_response({'error': str(exc)}, status=400)

    mcp_handler = create_mcp_handler(
        server_name='fmcsa-carrier-leads',
        server_version='1.0.0',
        tools=MCP_TOOLS,
        handlers={
            'search_new_carriers': _run_new_carriers,
            'search_pending_revocations': _run_pending_revocations,
            'lookup_usdot': _run_lookup_usdot,
        },
    )

    app.router.add_get('/', handle_health)
    app.router.add_get('/health', handle_health)
    app.router.add_get('/new-registrations', handle_new_authorities)
    app.router.add_get('/new-authorities', handle_new_authorities)
    app.router.add_get('/pending-revocations', handle_pending_revocations)
    app.router.add_get('/usdot/{dot_number}', handle_usdot_lookup)
    app.router.add_route('*', '/mcp', mcp_handler)
    return app


async def run_server() -> None:
    app = create_app()
    port = int(Actor.configuration.web_server_port or 8080)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    Actor.log.info(f'FMCSA Carrier Leads Standby + MCP Server listening on port {port}')

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass
    await stop_event.wait()
    await runner.cleanup()
