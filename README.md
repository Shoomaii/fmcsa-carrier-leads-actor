# US DOT FMCSA Motor Carrier Leads & Pending Insurance Revocations

Live official US DOT FMCSA lead finder, **Apify Store Actor**, **Streamable HTTP MCP Server (`/mcp`)**, and **Cloudflare Worker** free lookup app backed directly by official US Department of Transportation Open Data (`data.transportation.gov`) — zero scraping, zero brittle HTML selectors, and zero API key required.

## Official Data Sources Joined Automatically
1. **FMCSA Company Census File (`az4n-8mr2`)** — Newly registered active **USDOT motor carriers** (`add_date`), legal & DBA names, company officer names, office & cell phone numbers, email addresses, power units, driver counts, hazmat flags, and cargo classifications. *(Note: Census `add_date` measures USDOT registrations, including private fleets unless `authorizedForHireOnly: true` is enabled — distinct from MC operating authority grants.)*
2. **FMCSA Licensing & Insurance Carrier All With History (`6eyk-hxee`)** — Active & pending authority revocations (`common_rev_pend='Y'` or `contract_rev_pend='Y'`), docket numbers (`MC`/`MX`/`FF`), and minimum vs. on-file BIPD insurance coverage.
3. **FMCSA Active/Pending Insurance (`ypjt-5ydn`)** — Active/pending insurance carrier name, policy number, form code (`91X`, `BMC-34`, etc.), effective date, and max coverage limits.

## Built For High-Value B2B Buyers
- **Commercial Trucking Insurance Agents:** Reach newly registered USDOT carriers right after registration and rescue fleets facing pending authority revocation.
- **Freight Factoring Brokers & ELD / Dispatch SaaS:** Filter by state, fleet size (`minPowerUnits` / `maxPowerUnits`), and `authorizedForHireOnly` with `onlyNew` delta tracking (persisting only confirmed delivered `charged_count` leads so zero- or partial-budget runs never skip undelivered carriers).

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
- **Managed Daily State Digest (`$49/mo`):** Scheduled daily Apify run with `onlyNew: true` delivering fresh state-filtered New USDOT Registrations + Pending Authority Revocations directly to commercial insurance agents' inbox or Google Sheet every weekday morning. Inquire via `jshoomaii@gmail.com` or the lookup page button.

## Run Tests Locally
```bash
pytest -v
```
