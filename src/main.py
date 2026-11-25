"""Batch and Standby entry point for US DOT FMCSA Motor Carrier Leads Actor."""

from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import datetime, timezone

from apify import Actor

from .fmcsa import FmcsaClient, normalize_states
from .server import CHARGED_EVENT, run_server

SEEN_KEY = 'SEEN_LEAD_IDS'
MAX_SEEN_IDS = 100_000


def store_name(tracking_key: str | None) -> str:
    raw = (tracking_key or '').strip()
    if not raw:
        return 'fmcsa-leads-default'
    slug = re.sub(r'[^a-z0-9]+', '-', raw.lower()).strip('-')[:40] or 'list'
    digest = hashlib.sha256(raw.encode('utf-8')).hexdigest()[:8]
    return f'fmcsa-leads-{slug}-{digest}'


async def main() -> None:
    async with Actor:
        if Actor.configuration.meta_origin == 'STANDBY':
            await run_server()
            return

        inp = await Actor.get_input() or {}
        mode = str(inp.get('mode') or 'new_authorities').strip().lower()
        try:
            states = normalize_states(inp.get('states'))
            days_back = int(inp.get('daysBack') or 14)
            min_units = int(inp['minPowerUnits']) if inp.get('minPowerUnits') not in (None, '') else None
            max_units = int(inp['maxPowerUnits']) if inp.get('maxPowerUnits') not in (None, '') else None
            if min_units is not None and max_units is not None and min_units > max_units:
                raise ValueError('minPowerUnits cannot be greater than maxPowerUnits.')
            max_leads = max(1, min(int(inp.get('maxLeads') or 100), 1000))
        except (TypeError, ValueError) as exc:
            await Actor.fail(status_message=f'Invalid Actor input: {exc}')
            return

        for_hire_only = bool(inp.get('authorizedForHireOnly', False))
        require_contact = bool(inp.get('requireContact', True))
        only_new = bool(inp.get('onlyNew', False))

        seen_store = None
        seen: set[str] = set()
        if only_new:
            tkey = (inp.get('trackingKey') or '').strip() or 'default'
            seen_store = await Actor.open_key_value_store(name=store_name(tkey))
            seen = set(await seen_store.get_value(SEEN_KEY) or [])

        client = FmcsaClient()
        try:
            if mode == 'new_authorities':
                leads = await client.search_new_authorities(
                    states=states,
                    days_back=days_back,
                    min_power_units=min_units,
                    max_power_units=max_units,
                    authorized_for_hire_only=for_hire_only,
                    require_contact=require_contact,
                    limit=max_leads,
                )
            elif mode == 'pending_revocations':
                leads = await client.search_pending_revocations(
                    states=states,
                    require_contact=require_contact,
                    limit=max_leads,
                )
            elif mode == 'usdot_lookup':
                dots = [str(d).strip() for d in (inp.get('usdotNumbers') or []) if str(d).strip()]
                if not dots:
                    await Actor.fail(status_message='Provide at least one USDOT number in usdotNumbers.')
                    return
                leads = []
                for dot in dots[:max_leads]:
                    try:
                        profile = await client.lookup_usdot(dot)
                    except ValueError as exc:
                        await Actor.fail(status_message=f'Invalid USDOT number in usdotNumbers: {exc}')
                        return
                    if profile:
                        leads.append(profile)
            else:
                await Actor.fail(status_message=f'Unsupported mode: {mode!r}')
                return

            if only_new:
                leads = [r for r in leads if r['leadId'] not in seen]

            scraped_at = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
            output = [{**r, 'scrapedAt': scraped_at} for r in leads[:max_leads]]
            if output:
                res = await Actor.push_data(output, charged_event_name=CHARGED_EVENT)
                pushed_count = res.charged_count or len(output)
                if seen_store is not None:
                    old_order = list(await seen_store.get_value(SEEN_KEY) or [])
                    new_ids = [r['leadId'] for r in output[:pushed_count]]
                    merged = (old_order + [i for i in new_ids if i not in seen])[-MAX_SEEN_IDS:]
                    await seen_store.set_value(SEEN_KEY, merged)

            Actor.log.info(f'FMCSA mode={mode} states={states or ["ALL"]} saved={len(output)} leads.')
        finally:
            await client.close()


if __name__ == '__main__':
    asyncio.run(main())
