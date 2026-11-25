"""Official US DOT FMCSA Socrata REST client (Census, L&I Revocations, Active/Pending Insurance)."""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

CENSUS_URL = 'https://data.transportation.gov/resource/az4n-8mr2.json'
LI_CARRIER_URL = 'https://data.transportation.gov/resource/6eyk-hxee.json'
INSURANCE_URL = 'https://data.transportation.gov/resource/ypjt-5ydn.json'

VALID_STATES: frozenset[str] = frozenset({
    'AL', 'AK', 'AZ', 'AR', 'CA', 'CO', 'CT', 'DE', 'FL', 'GA',
    'HI', 'ID', 'IL', 'IN', 'IA', 'KS', 'KY', 'LA', 'ME', 'MD',
    'MA', 'MI', 'MN', 'MS', 'MO', 'MT', 'NE', 'NV', 'NH', 'NJ',
    'NM', 'NY', 'NC', 'ND', 'OH', 'OK', 'OR', 'PA', 'RI', 'SC',
    'SD', 'TN', 'TX', 'UT', 'VT', 'VA', 'WA', 'WV', 'WI', 'WY',
    'DC', 'PR', 'VI', 'GU',
})

CARGO_FIELDS: list[tuple[str, str]] = [
    ('crgo_genfreight', 'General Freight'),
    ('crgo_household', 'Household Goods'),
    ('crgo_metalsheet', 'Metal Sheets/Coils'),
    ('crgo_motoveh', 'Motor Vehicles'),
    ('crgo_drivetow', 'Drive/Tow Away'),
    ('crgo_logpole', 'Logs/Poles/Beams'),
    ('crgo_bldgmat', 'Building Materials'),
    ('crgo_mobilehome', 'Mobile Homes'),
    ('crgo_machlrg', 'Machinery/Large Objects'),
    ('crgo_produce', 'Fresh Produce'),
    ('crgo_liqgas', 'Liquids/Gases'),
    ('crgo_intermodal', 'Intermodal Containers'),
    ('crgo_passengers', 'Passengers'),
    ('crgo_oilfield', 'Oilfield Equipment'),
    ('crgo_livestock', 'Livestock'),
    ('crgo_grainfeed', 'Grain/Feed/Hay'),
    ('crgo_coalcoke', 'Coal/Coke'),
    ('crgo_meat', 'Meat'),
    ('crgo_garbage', 'Garbage/Refuse'),
    ('crgo_usmail', 'US Mail'),
    ('crgo_chem', 'Chemicals'),
    ('crgo_drybulk', 'Dry Bulk'),
    ('crgo_coldfood', 'Refrigerated Food'),
    ('crgo_beverages', 'Beverages'),
    ('crgo_paperprod', 'Paper Products'),
    ('crgo_utility', 'Utilities'),
    ('crgo_farmsupp', 'Agricultural/Farm Supplies'),
    ('crgo_construct', 'Construction'),
    ('crgo_waterwell', 'Water Well'),
]


def normalize_states(raw_states: list[str] | str | None) -> list[str]:
    """Validate and deduplicate 2-letter US state codes (prevents SoQL injection)."""
    if raw_states is None:
        return []
    items = [raw_states] if isinstance(raw_states, str) else list(raw_states)
    out: list[str] = []
    seen: set[str] = set()
    for entry in items:
        if not isinstance(entry, str):
            continue
        for token in entry.split(','):
            code = token.strip().upper()
            if not code:
                continue
            if code not in VALID_STATES:
                raise ValueError(f'Unsupported US state code: {code!r}')
            if code not in seen:
                seen.add(code)
                out.append(code)
    return out


def normalize_usdot(raw: str | int) -> str:
    """Return canonical unpadded numeric USDOT string (digits only)."""
    digits = re.sub(r'\D+', '', str(raw or ''))
    canonical = digits.lstrip('0')
    if not canonical or len(canonical) > 10:
        raise ValueError(f'Invalid USDOT number: {raw!r}')
    return canonical


def pad_usdot_8(dot: str) -> str:
    """Pad a canonical USDOT number to 8 digits for FMCSA L&I table joins."""
    return normalize_usdot(dot).zfill(8)


def normalize_docket_key(docket: str | None) -> str:
    """Normalize MC/MX/FF docket numbers across 6eyk-hxee and ypjt-5ydn formats."""
    s = re.sub(r'[^A-Za-z0-9]+', '', (docket or '').upper())
    m = re.match(r'^(MC|MX|FF)0*(\d+)$', s)
    if not m:
        return s
    return f'{m.group(1)}{m.group(2)}'


def docket_query_variants(docket: str | None) -> list[str]:
    """Return exact + zero-padded docket strings used by FMCSA insurance tables."""
    s = re.sub(r'[^A-Za-z0-9]+', '', (docket or '').upper())
    m = re.match(r'^(MC|MX|FF)0*(\d+)$', s)
    if not m:
        return [s] if s else []
    prefix, num = m.group(1), m.group(2)
    variants = {
        s,
        f'{prefix}{num}',
        f'{prefix}{num.zfill(6)}',
        f'{prefix}{num.zfill(7)}',
        f'{prefix}{num.zfill(8)}',
    }
    return sorted(variants)


def _to_int(val: Any) -> int | None:
    if val is None or val == '':
        return None
    try:
        return int(float(str(val).strip()))
    except (TypeError, ValueError):
        return None


def _clean_str(val: Any) -> str:
    return str(val).strip() if val is not None else ''


def _format_yyyymmdd(raw: str | None) -> str:
    s = re.sub(r'\D+', '', str(raw or ''))
    if len(s) == 8:
        return f'{s[0:4]}-{s[4:6]}-{s[6:8]}'
    return _clean_str(raw)


def _format_phone(raw: str | None) -> str:
    s = _clean_str(raw)
    digits = re.sub(r'\D+', '', s)
    if len(digits) == 10:
        return f'({digits[:3]}) {digits[3:6]}-{digits[6:]}'
    return s


def extract_cargo_carried(row: dict[str, Any]) -> list[str]:
    return [label for key, label in CARGO_FIELDS if str(row.get(key, '')).strip().upper() == 'X']


def normalize_census_lead(row: dict[str, Any], lead_type: str = 'new_authority') -> dict[str, Any]:
    dot = _clean_str(row.get('dot_number')).lstrip('0') or _clean_str(row.get('dot_number'))
    add_date_iso = _format_yyyymmdd(row.get('add_date'))
    phone = _format_phone(row.get('phone') or row.get('cell_phone'))
    email = _clean_str(row.get('email_address')).lower()
    officer = _clean_str(row.get('company_officer_1')) or _clean_str(row.get('company_officer_2'))
    power_units = _to_int(row.get('power_units'))
    drivers = _to_int(row.get('total_drivers'))
    return {
        'leadId': f'new:{dot}:{add_date_iso or "unknown"}',
        'leadType': lead_type,
        'dotNumber': dot,
        'docketNumber': '',
        'legalName': _clean_str(row.get('legal_name')),
        'dbaName': _clean_str(row.get('dba_name')),
        'officerName': officer,
        'phone': phone,
        'email': email,
        'street': _clean_str(row.get('phy_street')),
        'city': _clean_str(row.get('phy_city')),
        'state': _clean_str(row.get('phy_state')).upper(),
        'zipCode': _clean_str(row.get('phy_zip')),
        'powerUnits': power_units,
        'truckUnits': _to_int(row.get('truck_units')),
        'drivers': drivers,
        'carrierOperation': _clean_str(row.get('carrier_operation')),
        'classification': _clean_str(row.get('classdef')),
        'hazmat': _clean_str(row.get('hm_ind')).upper() == 'Y',
        'cargoCarried': extract_cargo_carried(row),
        'addDate': add_date_iso,
        'mcs150Date': _format_yyyymmdd(row.get('mcs150_date')),
        'commonStatus': '',
        'contractStatus': '',
        'brokerStatus': '',
        'commonRevocationPending': False,
        'contractRevocationPending': False,
        'minBipdRequiredThousands': None,
        'bipdOnFileThousands': None,
        'insuranceCompany': '',
        'insurancePolicyNo': '',
        'insuranceEffectiveDate': '',
        'insuranceFormCode': '',
        'sourceUrl': f'https://safer.fmcsa.dot.gov/query.asp?searchtype=ANY&query_type=queryCarrierSnapshot&query_param=USDOT&query_string={dot}',
    }


def merge_revocation_lead(
    li_row: dict[str, Any],
    census_row: dict[str, Any] | None = None,
    ins_row: dict[str, Any] | None = None,
) -> dict[str, Any]:
    raw_dot = _clean_str(li_row.get('dot_number'))
    dot = raw_dot.lstrip('0') or raw_dot
    docket = _clean_str(li_row.get('docket_number'))
    base = normalize_census_lead(census_row, lead_type='pending_revocation') if census_row else {
        'leadId': '',
        'leadType': 'pending_revocation',
        'dotNumber': dot,
        'docketNumber': docket,
        'legalName': '',
        'dbaName': '',
        'officerName': '',
        'phone': '',
        'email': '',
        'street': '',
        'city': '',
        'state': '',
        'zipCode': '',
        'powerUnits': None,
        'truckUnits': None,
        'drivers': None,
        'carrierOperation': '',
        'classification': '',
        'hazmat': False,
        'cargoCarried': [],
        'addDate': '',
        'mcs150Date': '',
        'sourceUrl': f'https://safer.fmcsa.dot.gov/query.asp?searchtype=ANY&query_type=queryCarrierSnapshot&query_param=USDOT&query_string={dot}',
    }
    base['leadId'] = f'rev:{dot}:{docket or "nodocket"}'
    base['leadType'] = 'pending_revocation'
    base['dotNumber'] = dot
    base['docketNumber'] = docket
    base['legalName'] = base['legalName'] or _clean_str(li_row.get('legal_name'))
    base['dbaName'] = base['dbaName'] or _clean_str(li_row.get('dba_name'))
    base['phone'] = base['phone'] or _format_phone(li_row.get('bus_telno'))
    base['street'] = base['street'] or _clean_str(li_row.get('bus_street_po'))
    base['city'] = base['city'] or _clean_str(li_row.get('bus_city'))
    base['state'] = base['state'] or _clean_str(li_row.get('bus_state_code')).upper()
    base['zipCode'] = base['zipCode'] or _clean_str(li_row.get('bus_zip_code'))
    base['commonStatus'] = _clean_str(li_row.get('common_stat'))
    base['contractStatus'] = _clean_str(li_row.get('contract_stat'))
    base['brokerStatus'] = _clean_str(li_row.get('broker_stat'))
    base['commonRevocationPending'] = _clean_str(li_row.get('common_rev_pend')).upper() == 'Y'
    base['contractRevocationPending'] = _clean_str(li_row.get('contract_rev_pend')).upper() == 'Y'
    base['minBipdRequiredThousands'] = _to_int(li_row.get('min_cov_amount'))
    base['bipdOnFileThousands'] = _to_int(li_row.get('bipd_file'))
    if ins_row:
        base['insuranceCompany'] = _clean_str(ins_row.get('name_company'))
        base['insurancePolicyNo'] = _clean_str(ins_row.get('policy_no'))
        base['insuranceEffectiveDate'] = _clean_str(ins_row.get('effective_date'))
        base['insuranceFormCode'] = _clean_str(ins_row.get('ins_form_code'))
    else:
        base['insuranceCompany'] = base.get('insuranceCompany', '')
        base['insurancePolicyNo'] = base.get('insurancePolicyNo', '')
        base['insuranceEffectiveDate'] = base.get('insuranceEffectiveDate', '')
        base['insuranceFormCode'] = base.get('insuranceFormCode', '')
    return base


class FmcsaClient:
    """Async client for official US DOT FMCSA Socrata datasets."""

    def __init__(self, app_token: str | None = None, timeout: float = 20.0) -> None:
        token = app_token if app_token is not None else os.environ.get('SOCRATA_APP_TOKEN', '')
        headers = {'Accept': 'application/json', 'User-Agent': 'fmcsa-carrier-leads/1.0'}
        if token:
            headers['X-App-Token'] = token
        self._client = httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=True)

    async def close(self) -> None:
        await self._client.aclose()

    async def _query(self, url: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        resp = await self._client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []

    async def search_new_authorities(
        self,
        *,
        states: list[str] | str | None = None,
        days_back: int = 14,
        min_power_units: int | None = None,
        max_power_units: int | None = None,
        authorized_for_hire_only: bool = False,
        require_contact: bool = True,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        clean_states = normalize_states(states)
        days = max(1, min(int(days_back), 365))
        cap = max(1, min(int(limit), 1000))
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime('%Y%m%d')

        clauses = ["status_code='A'", f"add_date>='{cutoff}'"]
        if clean_states:
            quoted = ','.join(f"'{s}'" for s in clean_states)
            clauses.append(f'phy_state in ({quoted})')
        if authorized_for_hire_only:
            clauses.append("classdef like '%AUTHORIZED FOR HIRE%'")
        if require_contact:
            clauses.append('(phone IS NOT NULL OR email_address IS NOT NULL)')

        fetch_limit = min(cap * 3 if (min_power_units is not None or max_power_units is not None) else cap, 1500)
        rows = await self._query(
            CENSUS_URL,
            {
                '$where': ' AND '.join(clauses),
                '$order': 'add_date DESC',
                '$limit': str(fetch_limit),
            },
        )

        leads: list[dict[str, Any]] = []
        for row in rows:
            lead = normalize_census_lead(row, lead_type='new_authority')
            units = lead['powerUnits']
            if min_power_units is not None and (units is None or units < min_power_units):
                continue
            if max_power_units is not None and (units is None or units > max_power_units):
                continue
            if require_contact and not (lead['phone'] or lead['email']):
                continue
            leads.append(lead)
            if len(leads) >= cap:
                break
        return leads

    async def search_pending_revocations(
        self,
        *,
        states: list[str] | str | None = None,
        require_contact: bool = True,
        limit: int = 50,
        enrich: bool = True,
    ) -> list[dict[str, Any]]:
        clean_states = normalize_states(states)
        cap = max(1, min(int(limit), 500))

        clauses = ["(common_rev_pend='Y' OR contract_rev_pend='Y')"]
        if clean_states:
            quoted = ','.join(f"'{s}'" for s in clean_states)
            clauses.append(f'bus_state_code in ({quoted})')

        li_rows = await self._query(
            LI_CARRIER_URL,
            {
                '$where': ' AND '.join(clauses),
                '$limit': str(min(cap * 2 if require_contact else cap, 600)),
            },
        )
        if not li_rows:
            return []

        census_by_dot: dict[str, dict[str, Any]] = {}
        ins_by_docket: dict[str, dict[str, Any]] = {}

        if enrich:
            dots = list(dict.fromkeys(
                _clean_str(r.get('dot_number')).lstrip('0')
                for r in li_rows
                if _clean_str(r.get('dot_number')).lstrip('0').isdigit()
            ))
            # Batch-fetch census details in chunks of 40
            for idx in range(0, len(dots), 40):
                chunk = dots[idx:idx + 40]
                quoted_dots = ','.join(f"'{d}'" for d in chunk)
                c_rows = await self._query(
                    CENSUS_URL,
                    {'$where': f'dot_number in ({quoted_dots})', '$limit': str(len(chunk) + 5)},
                )
                for cr in c_rows:
                    key = _clean_str(cr.get('dot_number')).lstrip('0')
                    if key:
                        census_by_dot[key] = cr

            # Batch-fetch active/pending insurance filings by docket variants in chunks of 25
            dockets = [
                _clean_str(r.get('docket_number'))
                for r in li_rows
                if _clean_str(r.get('docket_number'))
            ]
            for idx in range(0, len(dockets), 20):
                chunk = dockets[idx:idx + 20]
                variants: list[str] = []
                for dk in chunk:
                    variants.extend(docket_query_variants(dk))
                if not variants:
                    continue
                quoted_dk = ','.join(f"'{v}'" for v in dict.fromkeys(variants))
                i_rows = await self._query(
                    INSURANCE_URL,
                    {'$where': f'prefix_docket_number in ({quoted_dk})', '$limit': '200'},
                )
                for ir in i_rows:
                    dkey = normalize_docket_key(ir.get('prefix_docket_number'))
                    if dkey and dkey not in ins_by_docket:
                        ins_by_docket[dkey] = ir

        leads: list[dict[str, Any]] = []
        for lr in li_rows:
            dot_key = _clean_str(lr.get('dot_number')).lstrip('0')
            docket_key = normalize_docket_key(lr.get('docket_number'))
            lead = merge_revocation_lead(
                lr,
                census_row=census_by_dot.get(dot_key),
                ins_row=ins_by_docket.get(docket_key),
            )
            if require_contact and not (lead['phone'] or lead['email']):
                continue
            leads.append(lead)
            if len(leads) >= cap:
                break
        return leads

    async def lookup_usdot(self, dot_number: str | int) -> dict[str, Any] | None:
        dot = normalize_usdot(dot_number)
        dot_padded = pad_usdot_8(dot)

        c_rows = await self._query(CENSUS_URL, {'dot_number': dot, '$limit': '5'})
        li_rows = await self._query(
            LI_CARRIER_URL,
            {'$where': f"dot_number in ('{dot}','{dot_padded}')", '$limit': '5'},
        )
        if not c_rows and not li_rows:
            return None

        census_row = c_rows[0] if c_rows else None
        li_row = li_rows[0] if li_rows else None
        ins_rows: list[dict[str, Any]] = []
        if li_row and _clean_str(li_row.get('docket_number')):
            variants = docket_query_variants(li_row.get('docket_number'))
            quoted = ','.join(f"'{v}'" for v in variants)
            ins_rows = await self._query(
                INSURANCE_URL,
                {'$where': f'prefix_docket_number in ({quoted})', '$limit': '20'},
            )

        if li_row:
            profile = merge_revocation_lead(li_row, census_row=census_row, ins_row=ins_rows[0] if ins_rows else None)
            profile['leadType'] = 'pending_revocation' if (
                profile['commonRevocationPending'] or profile['contractRevocationPending']
            ) else 'usdot_lookup'
            profile['leadId'] = f'dot:{dot}'
        else:
            profile = normalize_census_lead(census_row or {}, lead_type='usdot_lookup')
            profile['leadId'] = f'dot:{dot}'

        profile['insuranceFilings'] = [
            {
                'docketNumber': _clean_str(r.get('prefix_docket_number')),
                'formCode': _clean_str(r.get('ins_form_code')),
                'typeCode': _clean_str(r.get('ins_type_code')),
                'classCode': _clean_str(r.get('ins_class_code')),
                'company': _clean_str(r.get('name_company')),
                'policyNo': _clean_str(r.get('policy_no')),
                'effectiveDate': _clean_str(r.get('effective_date')),
                'maxCoverageThousands': _to_int(r.get('max_cov_amount')),
                'underlyingLimitThousands': _to_int(r.get('underl_lim_amount')),
            }
            for r in ins_rows
        ]
        return profile
