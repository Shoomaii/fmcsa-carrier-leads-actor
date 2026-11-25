"""Unit, Standby HTTP/MCP, and Live US DOT API tests for FMCSA Motor Carrier Leads."""

from __future__ import annotations

import asyncio
import pytest
from aiohttp.test_utils import TestClient, TestServer

from src.fmcsa import (
    FmcsaClient,
    docket_query_variants,
    merge_revocation_lead,
    normalize_census_lead,
    normalize_docket_key,
    normalize_states,
    normalize_usdot,
    pad_usdot_8,
)
from src.server import create_app


def test_normalize_states_valid_and_injection_blocked():
    assert normalize_states(['tx', 'CA, fl', 'TX']) == ['TX', 'CA', 'FL']
    assert normalize_states(None) == []
    with pytest.raises(ValueError):
        normalize_states(["TX' OR '1'='1"])


def test_normalize_usdot_and_padding():
    assert normalize_usdot('002247505') == '2247505'
    assert pad_usdot_8('2247505') == '02247505'
    with pytest.raises(ValueError):
        normalize_usdot('abc')


def test_docket_normalization_and_variants():
    assert normalize_docket_key('MC00123456') == 'MC123456'
    assert normalize_docket_key('mc-123456') == 'MC123456'
    variants = docket_query_variants('MC123456')
    assert 'MC123456' in variants
    assert 'MC0123456' in variants
    assert 'MC00123456' in variants


def test_normalize_census_and_merge_revocation():
    census = {
        'dot_number': '4399001',
        'legal_name': 'LONE STAR FREIGHT LLC',
        'company_officer_1': 'JANE DOE',
        'phone': '5125550199',
        'email_address': 'DISPATCH@LONESTAR.COM',
        'phy_city': 'AUSTIN',
        'phy_state': 'TX',
        'phy_zip': '78701',
        'power_units': '4',
        'total_drivers': '5',
        'add_date': '20261124',
        'hm_ind': 'N',
        'crgo_genfreight': 'X',
    }
    lead = normalize_census_lead(census)
    assert lead['dotNumber'] == '4399001'
    assert lead['phone'] == '(512) 555-0199'
    assert lead['email'] == 'dispatch@lonestar.com'
    assert lead['addDate'] == '2026-11-24'
    assert lead['powerUnits'] == 4
    assert lead['cargoCarried'] == ['General Freight']

    li = {
        'dot_number': '04399001',
        'docket_number': 'MC1688000',
        'legal_name': 'LONE STAR FREIGHT LLC',
        'common_rev_pend': 'Y',
        'contract_rev_pend': 'N',
        'min_cov_amount': '750',
        'bipd_file': '1000',
    }
    ins = {
        'name_company': 'PROGRESSIVE COMMERCIAL',
        'policy_no': 'CA-998877',
        'effective_date': '05/01/2026',
        'ins_form_code': '91X',
    }
    merged = merge_revocation_lead(li, census_row=census, ins_row=ins)
    assert merged['leadType'] == 'pending_revocation'
    assert merged['commonRevocationPending'] is True
    assert merged['email'] == 'dispatch@lonestar.com'
    assert merged['insuranceCompany'] == 'PROGRESSIVE COMMERCIAL'
    assert merged['insurancePolicyNo'] == 'CA-998877'


class _FakeFmcsaClient:
    async def close(self) -> None:
        return None

    async def search_new_authorities(self, **kwargs):
        return [{
            'leadId': 'new:4399001:2026-11-24',
            'leadType': 'new_authority',
            'dotNumber': '4399001',
            'legalName': 'LONE STAR FREIGHT LLC',
            'state': 'TX',
            'phone': '(512) 555-0199',
        }]

    async def search_pending_revocations(self, **kwargs):
        return [{
            'leadId': 'rev:4399001:MC1688000',
            'leadType': 'pending_revocation',
            'dotNumber': '4399001',
            'docketNumber': 'MC1688000',
            'legalName': 'LONE STAR FREIGHT LLC',
            'insuranceCompany': 'PROGRESSIVE COMMERCIAL',
        }]

    async def lookup_usdot(self, dot_number: str):
        if dot_number == '4399001':
            return {'leadId': 'dot:4399001', 'dotNumber': '4399001', 'legalName': 'LONE STAR FREIGHT LLC'}
        return None


def test_standby_http_and_mcp_endpoints():
    charges: list[tuple[str, int, str | None]] = []

    async def fake_charge(event: str, count: int, req_id: str | None) -> None:
        charges.append((event, count, req_id))

    async def run():
        app = create_app(client_factory=_FakeFmcsaClient, charger=fake_charge)
        async with TestClient(TestServer(app)) as client:
            r = await client.get('/health')
            assert r.status == 200
            assert (await r.json())['status'] == 'ok'

            r = await client.get('/new-authorities?states=TX&daysBack=7', headers={'x-actor-request-id': 'req-1'})
            assert r.status == 200
            body = await r.json()
            assert body['count'] == 1
            assert body['leads'][0]['dotNumber'] == '4399001'

            r = await client.get('/pending-revocations?states=TX')
            assert r.status == 200
            assert (await r.json())['leads'][0]['insuranceCompany'] == 'PROGRESSIVE COMMERCIAL'

            r = await client.get('/usdot/4399001')
            assert r.status == 200
            assert (await r.json())['found'] is True

            r = await client.get('/usdot/99999999')
            assert r.status == 404

            r = await client.get('/new-authorities?states=TX&minPowerUnits=50&maxPowerUnits=10')
            assert r.status == 400

            r = await client.head('/health')
            assert r.status == 200

            # MCP initialize + tools/list + tools/call
            init_resp = await client.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}})
            assert init_resp.status == 200
            assert (await init_resp.json())['result']['serverInfo']['name'] == 'fmcsa-carrier-leads'

            list_resp = await client.post('/mcp', json={'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}})
            tool_names = [t['name'] for t in (await list_resp.json())['result']['tools']]
            assert tool_names == ['search_new_carriers', 'search_pending_revocations', 'lookup_usdot']

            call_resp = await client.post('/mcp', json={
                'jsonrpc': '2.0',
                'id': 3,
                'method': 'tools/call',
                'params': {'name': 'search_new_carriers', 'arguments': {'states': ['TX'], 'daysBack': 7}},
            })
            call_body = await call_resp.json()
            assert call_body['result']['isError'] is False
            assert call_body['result']['structuredContent']['count'] == 1

            bad_call = await client.post('/mcp', json={
                'jsonrpc': '2.0',
                'id': 4,
                'method': 'tools/call',
                'params': {'name': 'search_new_carriers', 'arguments': {'states': ['TX'], 'minPowerUnits': 20, 'maxPowerUnits': 5}},
            })
            assert (await bad_call.json())['result']['isError'] is True

    asyncio.run(run())
    assert len(charges) == 4


def test_live_us_dot_fmcsa_api():
    """Live integration test against official data.transportation.gov Socrata APIs."""
    async def run():
        client = FmcsaClient()
        try:
            new_leads = await client.search_new_authorities(states=['TX'], days_back=30, limit=5)
            assert len(new_leads) >= 1, 'Expected live newly registered TX carriers from US DOT'
            assert new_leads[0]['dotNumber'].isdigit()
            assert new_leads[0]['legalName']

            rev_leads = await client.search_pending_revocations(states=['TX'], limit=3, enrich=True)
            assert len(rev_leads) >= 1, 'Expected live pending-revocation TX carriers from US DOT'
            assert rev_leads[0]['commonRevocationPending'] or rev_leads[0]['contractRevocationPending']

            profile = await client.lookup_usdot(rev_leads[0]['dotNumber'])
            assert profile is not None
            assert profile['dotNumber'] == rev_leads[0]['dotNumber']
        finally:
            await client.close()

    asyncio.run(run())
