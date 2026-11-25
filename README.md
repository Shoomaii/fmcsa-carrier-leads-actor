# US DOT FMCSA Motor Carrier Leads & Pending Insurance Revocations

Live official US DOT FMCSA lead finder, **Apify Store Actor**, **Streamable HTTP MCP Server (`/mcp`)**, and **Cloudflare Worker** free lookup app backed directly by official US Department of Transportation Open Data (`data.transportation.gov`) — zero scraping, zero brittle HTML selectors, and zero API key required.

## Official Data Sources Joined Automatically
1. **FMCSA Company Census File (`az4n-8mr2`)** — Newly registered active motor carriers (`add_date`), legal & DBA names, company officer names, phone numbers, email addresses, power units, driver counts, hazmat flags, and cargo classifications.
2. **FMCSA Licensing & Insurance Carrier All With History (`6eyk-hxee`)** — Active & pending authority revocations (`common_rev_pend='Y'` or `contract_rev_pend='Y'`), docket numbers (`MC`/`MX`/`FF`), and minimum vs. on-file BIPD insurance coverage.
3. **FMCSA Active/Pending Insurance (`ypjt-5ydn`)** — Active/pending insurance carrier name, policy number, form code (`91X`, `BMC-34`, etc.), effective date, and max coverage limits.

## Built For High-Value B2B Buyers
- **Commercial Trucking Insurance Agents:** Reach newly registered trucking companies the day they receive a USDOT# (before they lock in annual BIPD/cargo policies) and rescue fleets facing pending authority revocation.
- **Freight Factoring Brokers & ELD / Dispatch SaaS:** Filter by state, fleet size (`minPowerUnits` / `maxPowerUnits`), and `authorizedForHireOnly` with `onlyNew` delta tracking so scheduled runs never deliver duplicate leads.

---

## Quick Deploy (Zero Upfront Cost)

### 1. Deploy to Apify Store + MCP Registry (Pay-Per-Event)
- Connect this GitHub repo in **Apify Console → Actors → Create from GitHub**.
- Enable **Standby Mode** (`.actor/actor.json` already configures `usesStandbyMode: true` and `webServerMcpPath: "/mcp"`).
- Set Pay-Per-Event pricing on event `lead` (recommended: **`$0.005 / lead`** — `$5` per 1,000 enriched carrier leads).
- MCP Tools exposed at `POST /mcp`:
  - `search_new_carriers`
  - `search_pending_revocations`
  - `lookup_usdot`

### 2. Deploy Free Web Lookup + Lead Magnet on Cloudflare Workers
```bash
npx wrangler deploy
```
*(Optional: set a free Socrata App Token with `npx wrangler secret put SOCRATA_APP_TOKEN` for higher production rate limits.)*

---

## Monetization & Daily Digest
- **Apify Store / MCP:** `$0.005 / lead` pay-per-event (`lead` event) for agencies, brokers, and AI sales agents.
- **Managed Daily State Digest (`$49/mo` via Payhip / Stripe):** Scheduled daily Apify run with `onlyNew: true` delivering fresh state-filtered New Authorities + Pending Revocations directly to commercial insurance agents' inbox or Google Sheet every morning at 7 AM local time.

## Run Tests Locally
```bash
pytest -v
```
