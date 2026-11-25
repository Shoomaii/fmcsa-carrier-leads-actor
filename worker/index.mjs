/**
 * Cloudflare Worker — Free FMCSA New Carrier & Pending Revocation Lookup Proxy
 * Proxies official US DOT Socrata feeds (data.transportation.gov) with strict SoQL
 * allowlisting, edge caching, and a 10-lead free preview cap.
 */

const CENSUS_URL = 'https://data.transportation.gov/resource/az4n-8mr2.json';
const LI_URL = 'https://data.transportation.gov/resource/6eyk-hxee.json';
const INS_URL = 'https://data.transportation.gov/resource/ypjt-5ydn.json';

const VALID_STATES = new Set([
  'AL','AK','AZ','AR','CA','CO','CT','DE','FL','GA',
  'HI','ID','IL','IN','IA','KS','KY','LA','ME','MD',
  'MA','MI','MN','MS','MO','MT','NE','NV','NH','NJ',
  'NM','NY','NC','ND','OH','OK','OR','PA','RI','SC',
  'SD','TN','TX','UT','VT','VA','WA','WV','WI','WY',
  'DC','PR','VI','GU',
]);

const FREE_PREVIEW_CAP = 10;

function json(data, status = 200, extraHeaders = {}) {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      'content-type': 'application/json; charset=utf-8',
      'cache-control': status === 200 ? 'public, max-age=300' : 'no-store',
      'access-control-allow-origin': '*',
      ...extraHeaders,
    },
  });
}

function cleanState(raw) {
  const s = String(raw || '').trim().toUpperCase();
  if (!s || s === 'ALL') return '';
  if (!VALID_STATES.has(s)) throw new Error(`Unsupported US state code: ${s}`);
  return s;
}

function cleanUsdot(raw) {
  const digits = String(raw || '').replace(/\D+/g, '').replace(/^0+/, '');
  if (!digits || digits.length > 10) throw new Error('Enter a valid numeric USDOT number.');
  return digits;
}

function fmtDate(raw) {
  const d = String(raw || '').replace(/\D+/g, '');
  return d.length === 8 ? `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6, 8)}` : String(raw || '').trim();
}

function fmtPhone(raw) {
  const s = String(raw || '').trim();
  const d = s.replace(/\D+/g, '');
  return d.length === 10 ? `(${d.slice(0, 3)}) ${d.slice(3, 6)}-${d.slice(6)}` : s;
}

function normDocketKey(docket) {
  const s = String(docket || '').toUpperCase().replace(/[^A-Z0-9]+/g, '');
  const m = s.match(/^(MC|MX|FF)0*(\d+)$/);
  return m ? `${m[1]}${m[2]}` : s;
}

function docketVariants(docket) {
  const s = String(docket || '').toUpperCase().replace(/[^A-Z0-9]+/g, '');
  const m = s.match(/^(MC|MX|FF)0*(\d+)$/);
  if (!m) return s ? [s] : [];
  const [, p, n] = m;
  return [...new Set([s, `${p}${n}`, `${p}${n.padStart(6, '0')}`, `${p}${n.padStart(7, '0')}`, `${p}${n.padStart(8, '0')}`])];
}

async function socrataFetch(url, params, env) {
  const u = new URL(url);
  for (const [k, v] of Object.entries(params)) u.searchParams.set(k, String(v));
  const headers = { Accept: 'application/json', 'User-Agent': 'fmcsa-carrier-leads-worker/1.0' };
  if (env && env.SOCRATA_APP_TOKEN) headers['X-App-Token'] = env.SOCRATA_APP_TOKEN;
  const resp = await fetch(u.toString(), { headers, cf: { cacheTtl: 300, cacheEverything: true } });
  if (!resp.ok) throw new Error(`FMCSA upstream returned HTTP ${resp.status}`);
  const data = await resp.json();
  return Array.isArray(data) ? data : [];
}

function normalizeCensus(r, leadType = 'new_authority') {
  const dot = String(r.dot_number || '').replace(/^0+/, '') || String(r.dot_number || '').trim();
  return {
    leadType,
    dotNumber: dot,
    docketNumber: '',
    legalName: String(r.legal_name || '').trim(),
    dbaName: String(r.dba_name || '').trim(),
    officerName: String(r.company_officer_1 || r.company_officer_2 || '').trim(),
    phone: fmtPhone(r.phone || r.cell_phone),
    email: String(r.email_address || '').trim().toLowerCase(),
    city: String(r.phy_city || '').trim(),
    state: String(r.phy_state || '').trim().toUpperCase(),
    zipCode: String(r.phy_zip || '').trim(),
    powerUnits: r.power_units !== undefined && r.power_units !== '' ? Number(r.power_units) : null,
    drivers: r.total_drivers !== undefined && r.total_drivers !== '' ? Number(r.total_drivers) : null,
    classification: String(r.classdef || '').trim(),
    hazmat: String(r.hm_ind || '').toUpperCase() === 'Y',
    addDate: fmtDate(r.add_date),
    commonRevocationPending: false,
    contractRevocationPending: false,
    insuranceCompany: '',
    insurancePolicyNo: '',
    insuranceEffectiveDate: '',
    sourceUrl: `https://safer.fmcsa.dot.gov/query.asp?searchtype=ANY&query_type=queryCarrierSnapshot&query_param=USDOT&query_string=${dot}`,
  };
}

function mergeRevocation(li, census, ins) {
  const dot = String(li.dot_number || '').replace(/^0+/, '') || String(li.dot_number || '').trim();
  const base = census ? normalizeCensus(census, 'pending_revocation') : {
    leadType: 'pending_revocation',
    dotNumber: dot,
    docketNumber: '',
    legalName: '',
    dbaName: '',
    officerName: '',
    phone: '',
    email: '',
    city: '',
    state: '',
    zipCode: '',
    powerUnits: null,
    drivers: null,
    classification: '',
    hazmat: false,
    addDate: '',
    sourceUrl: `https://safer.fmcsa.dot.gov/query.asp?searchtype=ANY&query_type=queryCarrierSnapshot&query_param=USDOT&query_string=${dot}`,
  };
  return {
    ...base,
    leadType: 'pending_revocation',
    dotNumber: dot,
    docketNumber: String(li.docket_number || '').trim(),
    legalName: base.legalName || String(li.legal_name || '').trim(),
    dbaName: base.dbaName || String(li.dba_name || '').trim(),
    phone: base.phone || fmtPhone(li.bus_telno),
    city: base.city || String(li.bus_city || '').trim(),
    state: base.state || String(li.bus_state_code || '').trim().toUpperCase(),
    zipCode: base.zipCode || String(li.bus_zip_code || '').trim(),
    commonStatus: String(li.common_stat || '').trim(),
    contractStatus: String(li.contract_stat || '').trim(),
    commonRevocationPending: String(li.common_rev_pend || '').toUpperCase() === 'Y',
    contractRevocationPending: String(li.contract_rev_pend || '').toUpperCase() === 'Y',
    minBipdRequiredThousands: li.min_cov_amount ? Number(li.min_cov_amount) : null,
    bipdOnFileThousands: li.bipd_file ? Number(li.bipd_file) : null,
    insuranceCompany: ins ? String(ins.name_company || '').trim() : '',
    insurancePolicyNo: ins ? String(ins.policy_no || '').trim() : '',
    insuranceEffectiveDate: ins ? String(ins.effective_date || '').trim() : '',
  };
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: { 'access-control-allow-origin': '*', 'access-control-allow-methods': 'GET,OPTIONS' } });
    }

    try {
      if (url.pathname === '/api/new-authorities') {
        const state = cleanState(url.searchParams.get('state') || 'TX');
        const rawDays = url.searchParams.get('daysBack');
        const parsedDays = rawDays !== null && rawDays !== '' ? Number(rawDays) : 14;
        if (!Number.isInteger(parsedDays) || parsedDays < 1 || parsedDays > 365) {
          throw new Error('daysBack must be an integer between 1 and 365.');
        }
        const days = Math.min(parsedDays, 90);

        const parseOptionalInt = (name) => {
          const raw = url.searchParams.get(name);
          if (raw === null || raw === '') return null;
          const n = Number(raw);
          if (!Number.isInteger(n) || n < 0) throw new Error(`${name} must be a non-negative integer.`);
          return n;
        };
        const minUnits = parseOptionalInt('minPowerUnits');
        const maxUnits = parseOptionalInt('maxPowerUnits');
        if (minUnits !== null && maxUnits !== null && minUnits > maxUnits) {
          throw new Error('minPowerUnits cannot be greater than maxPowerUnits.');
        }
        const forHire = url.searchParams.get('forHire') === 'true';

        const cutoffDate = new Date(Date.now() - days * 86400 * 1000);
        const cutoff = cutoffDate.toISOString().slice(0, 10).replace(/-/g, '');

        const clauses = ["status_code='A'", `add_date>='${cutoff}'`, '(phone IS NOT NULL OR email_address IS NOT NULL)'];
        if (state) clauses.push(`phy_state='${state}'`);
        if (forHire) clauses.push("classdef like '%AUTHORIZED FOR HIRE%'");

        const hasFleetFilter = minUnits !== null || maxUnits !== null;
        const rows = await socrataFetch(CENSUS_URL, {
          $where: clauses.join(' AND '),
          $order: 'add_date DESC',
          $limit: hasFleetFilter ? '250' : '50',
        }, env);

        const filtered = rows
          .map((r) => normalizeCensus(r, 'new_authority'))
          .filter((r) => (minUnits === null || (r.powerUnits !== null && r.powerUnits >= minUnits)))
          .filter((r) => (maxUnits === null || (r.powerUnits !== null && r.powerUnits <= maxUnits)));

        return json({
          mode: 'new_authorities',
          state: state || 'ALL',
          daysBack: days,
          totalMatchedInBatch: filtered.length,
          previewCap: FREE_PREVIEW_CAP,
          leads: filtered.slice(0, FREE_PREVIEW_CAP),
        });
      }

      if (url.pathname === '/api/pending-revocations') {
        const state = cleanState(url.searchParams.get('state') || 'TX');
        const clauses = ["(common_rev_pend='Y' OR contract_rev_pend='Y')"];
        if (state) clauses.push(`bus_state_code='${state}'`);

        const liRows = await socrataFetch(LI_URL, {
          $where: clauses.join(' AND '),
          $limit: '20',
        }, env);

        const dots = [...new Set(liRows.map((r) => String(r.dot_number || '').replace(/^0+/, '')).filter((d) => /^\d+$/.test(d)))];
        const variants = [...new Set(liRows.flatMap((r) => docketVariants(r.docket_number)))];
        const variantChunks = [];
        for (let i = 0; i < variants.length; i += 40) variantChunks.push(variants.slice(i, i + 40));

        const [censusRows, ...insChunks] = await Promise.all([
          dots.length ? socrataFetch(CENSUS_URL, { $where: `dot_number in (${dots.map((d) => `'${d}'`).join(',')})`, $limit: '40' }, env) : [],
          ...variantChunks.map((chunk) =>
            socrataFetch(INS_URL, { $where: `prefix_docket_number in (${chunk.map((v) => `'${v}'`).join(',')})`, $limit: '100' }, env)
          ),
        ]);
        const insRows = insChunks.flat();

        const censusMap = new Map(censusRows.map((r) => [String(r.dot_number || '').replace(/^0+/, ''), r]));
        const insMap = new Map();
        for (const ir of insRows) {
          const k = normDocketKey(ir.prefix_docket_number);
          if (k && !insMap.has(k)) insMap.set(k, ir);
        }

        const leads = liRows
          .map((lr) => mergeRevocation(lr, censusMap.get(String(lr.dot_number || '').replace(/^0+/, '')), insMap.get(normDocketKey(lr.docket_number))))
          .filter((r) => r.phone || r.email);

        return json({
          mode: 'pending_revocations',
          state: state || 'ALL',
          totalMatchedInBatch: leads.length,
          previewCap: FREE_PREVIEW_CAP,
          leads: leads.slice(0, FREE_PREVIEW_CAP),
        });
      }

      if (url.pathname.startsWith('/api/usdot/')) {
        const dot = cleanUsdot(url.pathname.slice('/api/usdot/'.length));
        const padded = dot.padStart(8, '0');
        const [cRows, liRows] = await Promise.all([
          socrataFetch(CENSUS_URL, { dot_number: dot, $limit: '3' }, env),
          socrataFetch(LI_URL, { $where: `dot_number in ('${dot}','${padded}')`, $limit: '3' }, env),
        ]);
        if (!cRows.length && !liRows.length) {
          return json({ found: false, dotNumber: dot, carrier: null }, 404);
        }
        let insRows = [];
        if (liRows[0] && liRows[0].docket_number) {
          const vs = docketVariants(liRows[0].docket_number);
          if (vs.length) {
            insRows = await socrataFetch(INS_URL, { $where: `prefix_docket_number in (${vs.map((v) => `'${v}'`).join(',')})`, $limit: '20' }, env);
          }
        }
        const carrier = liRows[0]
          ? mergeRevocation(liRows[0], cRows[0], insRows[0])
          : normalizeCensus(cRows[0], 'usdot_lookup');
        carrier.insuranceFilings = insRows.map((r) => ({
          docketNumber: String(r.prefix_docket_number || '').trim(),
          formCode: String(r.ins_form_code || '').trim(),
          company: String(r.name_company || '').trim(),
          policyNo: String(r.policy_no || '').trim(),
          effectiveDate: String(r.effective_date || '').trim(),
          maxCoverageThousands: r.max_cov_amount ? Number(r.max_cov_amount) : null,
        }));
        return json({ found: true, dotNumber: dot, carrier });
      }
    } catch (err) {
      return json({ error: err.message || 'Lookup failed' }, 400);
    }

    if (env && env.ASSETS) {
      return env.ASSETS.fetch(request);
    }
    return new Response('Not Found', { status: 404 });
  },
};
