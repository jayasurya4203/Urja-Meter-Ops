# Urja Meter Ops — read-only API layer

A clean, documented REST service in front of the legacy **Urja Meter Ops** portal
(`https://urja-ops.flockenergy.tech`). The portal exposes a server-rendered UI and
a handful of undocumented JSON endpoints. This service authenticates with it,
normalises its messy data, and publishes a stable contract with OpenAPI docs, a
browser dashboard, caching and tests.

The portal is treated as strictly read-only. No write, reset or administrative
operations are implemented or attempted.

**What it gives you**

- Every meter in one normalised dataset: **403 meters**, **40 distribution
  transformers**, 3 zones, 6 circles, 10 divisions, 14 subdivisions,
  18 substations and 28 feeders (counts observed at the time of writing).
- HTML/JS parsing for both meter-detail schemas, including SvelteKit's inline
  JavaScript payload.
- Cumulative register readings converted into typed readings and per-interval
  usage, with mixed 30-minute/daily granularity detected automatically.
- A reconstructed hierarchy with explicit data-quality flags and parent
  conflicts instead of silently invented structure.
- TTL caching with single-flight loads and stale-on-error fallback.
- Optional API-key protection, CSV/JSON exports, proximity search and a
  self-contained dashboard.

Start with **[PROTOCOL.md](PROTOCOL.md)** for the reconnaissance findings, the
**[OpenAPI document](openapi.json)** for the contract, and
**[REFLECTION.md](REFLECTION.md)** for the trade-offs and retrospective.

---

## Quick start

Requires **Python 3.11+** (developed and tested on 3.12).

```bash
python -m venv .venv

# POSIX
source .venv/bin/activate
# Windows (PowerShell)
# .venv\Scripts\Activate.ps1

pip install -r requirements.txt

# The sandbox credentials supplied with the exercise are already in the example.
cp .env.example .env

uvicorn app.main:app --reload --port 8000
```

Open:

| Surface | URL |
| --- | --- |
| Dashboard | <http://127.0.0.1:8000/> |
| Scalar API reference | <http://127.0.0.1:8000/reference> |
| Swagger UI | <http://127.0.0.1:8000/docs> |
| Raw OpenAPI 3.1 | <http://127.0.0.1:8000/openapi.json> |
| Liveness | <http://127.0.0.1:8000/health> |
| Readiness (loads the dataset) | <http://127.0.0.1:8000/health/ready> |

The first data request logs in, fetches the signed bulk export and the
transformer registry, and builds the dataset in roughly one second on this
machine. Subsequent requests are served from memory until the TTL expires.

### Run the tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

The suite uses `httpx.MockTransport` and a fake portal client, so it needs no
network access and never touches the real portal.

### Regenerate the committed OpenAPI file

```bash
python scripts/export_openapi.py
```

This imports the app without starting its lifespan; it does not contact the
portal.

---

## Deploy a public URL

`render.yaml` describes a Render blueprint: free web service, Python 3.12,
bound to `0.0.0.0:$PORT`, health check on `/health`.

1. Sign in to Render with GitHub and choose **New → Blueprint**, then pick this
   repository.
2. Render asks for `PORTAL_EMAIL` and `PORTAL_PASSWORD` (declared with
   `sync: false`). It generates `API_KEY` itself; copy the value from the
   service's Environment tab.
3. Open `https://<service>.onrender.com/` and paste the key when the dashboard
   asks. It is kept in `localStorage`.

The API key is mandatory in this setup because the URL is public and the
service proxies a portal holding real credentials. Remove `API_KEY` from the
environment only for a throwaway demo.

Free instances sleep after 15 minutes without traffic; the next request takes
about 50 seconds to wake.

For a temporary link with no account, tunnel the local server instead:

```bash
cloudflared tunnel --url http://localhost:8000
```

---

## Configuration

Configuration is read from the environment and `.env` via
`pydantic-settings` (see `.env.example`). The portal credentials are required
for any data endpoint.

| Variable | Default | Purpose |
| --- | --- | --- |
| `PORTAL_BASE_URL` | `https://urja-ops.flockenergy.tech` | Legacy portal root |
| `PORTAL_EMAIL` | — | operator@urja.local |
| `PORTAL_PASSWORD` | — | urja-ops-2026 |
| `PORTAL_TIMEZONE` | `Asia/Kolkata` | Timezone applied to portal timestamps |
| `DATASET_CACHE_TTL_SECONDS` | `300` | Bulk-export snapshot lifetime |
| `DETAIL_CACHE_TTL_SECONDS` | `120` | Per-meter SSR detail lifetime |
| `ENERGY_CACHE_TTL_SECONDS` | `120` | Per-meter consumption lifetime |
| `CACHE_MAX_ENTRIES` | `2048` | In-memory cache bound |
| `PORTAL_REQUEST_TIMEOUT_SECONDS` | `20` | Per-request timeout |
| `PORTAL_CONNECT_TIMEOUT_SECONDS` | `5` | Connection timeout |
| `PORTAL_MAX_RETRIES` | `2` | Retries for transport errors, 5xx and 429 |
| `SESSION_REFRESH_SKEW_SECONDS` | `90` | Proactive re-login margin |
| `API_KEY` | empty | If set, `/api/v1/*` requires `X-API-Key` |
| `MAX_CONSUMPTION_RANGE_DAYS` | `92` | Local validation limit for `from`/`to` |
| `DEFAULT_PAGE_SIZE` | `25` | Default list page size |
| `CORS_ORIGINS` | empty | Comma-separated allowed origins, off by default |
| `LOG_LEVEL` | `INFO` | Application log level |

Never commit `.env`; it is gitignored. The example file contains only the
sandbox credentials supplied with the take-home.

---

## API

All application endpoints live under `/api/v1`. Responses are JSON unless noted.
There is also `/health`, `/health/ready`, and the static documentation/dashboard
routes outside that prefix.

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/api/v1/meters` | List, search, filter, sort and paginate the dataset |
| `GET` | `/api/v1/meters/near` | Haversine search around a coordinate |
| `GET` | `/api/v1/meters/{id}` | One meter, preferring the live portal detail page |
| `GET` | `/api/v1/meters/{id}/consumption` | Register history with interval deltas |
| `GET` | `/api/v1/hierarchy` | Reconstructed zone → DT tree plus conflicts |
| `GET` | `/api/v1/transformers` | DT registry joined to live meter counts |
| `GET` | `/api/v1/stats/summary` | Dataset counts and freshness |
| `GET` | `/api/v1/export/meters` | Full clean dataset as JSON |
| `GET` | `/api/v1/export/meters.csv` | Full flat dataset as CSV |
| `GET` | `/api/v1/auth/status` | Local view of the portal session |
| `POST` | `/api/v1/auth/login` | Force a fresh portal login |
| `GET` | `/api/v1/` | Small machine-readable index |

### List meters

Query parameters (all optional):

| Parameter | Notes |
| --- | --- |
| `q` | Substring across meter ID, serial, make and hierarchy names |
| `status` | `Installed`, `Faulty`, `Decommissioned`; comma-separated allowed |
| `phase` | `single` or `three`; comma-separated allowed |
| `make`, `build` | e.g. `L&T`, `legacy`, `v2`; comma-separated allowed |
| `dt`, `feeder`, `zone` | Code **or** name; comma-separated allowed |
| `sort` | `meter_id`, `serial_number`, `make`, `phase_type`, `install_status`, `install_type`, `build`, `dt_code`, `zone`, `feeder`; prefix `-` for descending |
| `page` | 1-based, default 1 |
| `page_size` | 1–200, default 25 |

```bash
curl -s "http://127.0.0.1:8000/api/v1/meters?status=Faulty&page_size=2"
```

```json
{
  "items": [
    {
      "meter_id": "J100004",
      "serial_number": "SE65293",
      "make": "Genus",
      "phase_type": "single",
      "install_status": "Faulty",
      "install_type": "CT Operated",
      "build": "v2",
      "dt_code": "DT-005",
      "hierarchy": {
        "zone": {"level": "zone", "name": "Jaipur Zone 2", "code": "Z-02", "data_quality": []},
        "circle": {"level": "circle", "name": "Circle 5", "code": "C-05", "data_quality": []},
        "division": {"level": "division", "name": "Division 5", "code": "D-05", "data_quality": []},
        "subdivision": {"level": "subdivision", "name": "Subdivision 5", "code": "SD-05", "data_quality": []},
        "substation": {"level": "substation", "name": "Substation 5", "code": "SS-05", "data_quality": []},
        "feeder": {"level": "feeder", "name": "Feeder 5", "code": "F-005", "data_quality": []},
        "dt": {"level": "dt", "name": "Bani Park DT 5", "code": "DT-005", "data_quality": []}
      },
      "hierarchy_path": "Jaipur Zone 2 / Circle 5 / Division 5 / Subdivision 5 / Substation 5 / Feeder 5 / Bani Park DT 5",
      "location": {"latitude": 26.907425164505934, "longitude": 75.72503661059095}
    }
  ],
  "page": 1,
  "page_size": 2,
  "total": 90,
  "pages": 45
}
```

### Meter detail

```bash
curl -s "http://127.0.0.1:8000/api/v1/meters/J100001"
```

The detail path prefers the live SSR page, which uses one of two schemas depending
on the meter generation: a legacy `parameterName`/`parameterValue` table or a v2
`classData` JSON string. Hierarchy and coordinates are taken from the cached bulk
snapshot; if the live page is unavailable, the endpoint serves the snapshot with
`detail_source: "bulk_export_cached"` and a warning instead of failing.

### Consumption

```bash
# Portal default window (7 days ending at the latest available reading)
curl -s "http://127.0.0.1:8000/api/v1/meters/J100001/consumption"

# Explicit inclusive range
curl -s "http://127.0.0.1:8000/api/v1/meters/J100001/consumption?from=2026-06-01&to=2026-06-30"
```

`from` and `to` are inclusive ISO dates. Omitting both uses the portal's own
default window. Ranges are validated locally: reversed dates and ranges longer
than 92 days return `422` rather than being silently ignored by the portal.

The response contains:

- `readings[]` — each row has `kwh`/`kvah` **cumulative registers**, `volt_r`,
  and computed `kwh_delta`/`kvah_delta` usage values. The first delta is `null`.
- `granularity` — `30min`, `daily`, `irregular` or `empty`, detected from the
  median interval rather than assumed.
- `interval_minutes` — median spacing.
- `summary` — total interval usage, first/last registers and voltage stats.
- `anomalies` — register regressions, duplicate timestamps and unparsable rows.
- Timestamps are ISO-8601 with the configured timezone offset (IST by default).

### Hierarchy and transformers

```bash
curl -s "http://127.0.0.1:8000/api/v1/hierarchy"
curl -s "http://127.0.0.1:8000/api/v1/transformers?feeder=F-002"
```

The portal stores a flat path on each meter, not a normalised tree, and the paths
are inconsistent: blank codes/names, aliases, and nodes reported under multiple
parents. The service repairs what is unambiguous, uses the DT registry as the
canonical transformer name, chooses the most frequently reported parent
(deterministic alphabetical tie-break), and exposes the decisions:

- `data_quality` flags such as `code_derived`, `name_derived`, `name_alias`,
  `parent_conflict`, `orphan`.
- `conflicts[]` with every reported parent and the chosen parent.
- `node_counts` so clients can see how much structure exists at each level.

### Proximity and exports

```bash
curl -s "http://127.0.0.1:8000/api/v1/meters/near?latitude=26.85&longitude=75.8&radius_km=2"
curl -s -o meters.csv "http://127.0.0.1:8000/api/v1/export/meters.csv"
```

CSV fields beginning with `=`, `+`, `-`, `@`, tab or carriage return are prefixed
with a single quote to prevent spreadsheet formula injection.

### Optional API key

Set `API_KEY` in `.env` to require `X-API-Key` on every `/api/v1/*` request:

```bash
curl -s -H "X-API-Key: $API_KEY" "http://127.0.0.1:8000/api/v1/meters"
```

The static dashboard detects a `401`, prompts for the key, stores it in browser
local storage and retries. Health, docs and the dashboard itself are not gated.
For a public deployment, put the whole service behind a reverse proxy or a real
user-auth layer; this optional key is a coarse shared secret, not user identity.

---

## Response metadata and caching

Data responses include:

| Header | Meaning |
| --- | --- |
| `X-Cache` | `hit`, `miss` or `stale` |
| `X-Data-Fetched-At` | When the underlying source was read (UTC ISO-8601) |
| `Cache-Control` | `private, max-age=…` (client caches must not share secrets) |

The dataset snapshot is cached under one key; per-meter detail and consumption
are cached separately. A per-key striped lock provides single-flight behaviour so
concurrent misses do not stampede the portal. On a portal error, an expired entry
is served as `stale` rather than failing immediately; cache failures are logged.
`/health/ready` loads the dataset on demand and returns `503` with
`status: "unavailable"` if it cannot.

`/health` is deliberately liveness-oriented: it reports `ok` when credentials are
configured, and exposes the local session view separately. It does not call the
portal. This avoids restart-loops/alert noise when the upstream is briefly down.

---

## Error responses

All errors use the same envelope:

```json
{"error": "meter_not_found", "message": "Meter J999999 was not found"}
```

| Status | `error` | Cause |
| --- | --- | --- |
| `401` | `unauthorized` | Missing/invalid `X-API-Key` (only when `API_KEY` is set) |
| `404` | `meter_not_found` | Meter is not in the dataset or portal reports 404 |
| `422` | `validation_error` | Invalid query/path parameters; includes `details[]` |
| `502` | `portal_auth_failed` | Portal rejected credentials or the session cannot be established |
| `502` | `portal_protocol_error` | Portal returned an unexpected/undecodable payload |
| `502` | `portal_error` | Another upstream portal failure |
| `503` | `portal_unavailable` | Transport failure or repeated 5xx/429 |
| `500` | `internal_error` | Unexpected application error; details are logged, not returned |

The adapter retries transport errors, `5xx` and `429` with exponential backoff and
jitter, re-authenticates once on `401` or an SSR redirect to `/login`, and refreshes
the HMAC signing key once if the export signature is rejected.

---

## How it works

```
client ──HTTP──> FastAPI (/api/v1)
                    │
                    ├─ MeterService ── TTL cache (dataset/detail/energy)
                    │       │
                    │       ├─ normalise + index bulk export
                    │       ├─ canonicalise DT registry / rebuild hierarchy
                    │       └─ live per-meter detail + consumption
                    │
                    └─ UrjaPortalClient (cookie session, retries, HMAC)
                              │
                              └─ legacy portal (HTML + JSON)
```

1. **Authenticate once.** `POST /login` needs the `Origin` header to satisfy the
   portal's CSRF check. The session cookie and its `Max-Age` are kept in memory.
   A request whose cookie is close to expiry logs in again proactively.
2. **Fetch one complete snapshot.** `GET /portal/export?page=1` returns all meters
   in a single response and requires an HMAC-SHA256 signature over
   `GET\n/portal/export\npage=1\n<timestamp>`. The signing secret comes from
   `/portal/keys`, is cached briefly in memory and never exposed by this API.
3. **Normalise and index.** Numeric strings become numbers, blank hierarchy halves
   are repaired when unambiguous, the DT registry supplies canonical names and
   capacities, and the hierarchy and summary statistics are rebuilt.
4. **Serve cheaply.** List, filter, export, hierarchy and stats all run locally
   against the snapshot. Only details and consumption hit the portal per meter.
5. **Degrade gracefully.** Live detail failures fall back to the snapshot; stale
   cache entries are served on portal errors; data responses advertise freshness.

The full reconnaissance—login flow, every discovered endpoint, signed export,
both detail schemas, quirks and error envelopes—is in **[PROTOCOL.md](PROTOCOL.md)**.

---

## Project layout

```
app/
  main.py             FastAPI app, routes, error handlers, dashboard/reference routes
  client.py           Legacy portal adapter: session, retries, signed export, SSR parsing
  normalize.py        Pure normalisation: hierarchy repair, detail schemas, energy deltas
  hierarchy.py        Hierarchy reconstruction, DT canonicalisation, stats
  service.py          Dataset orchestration, filtering, caching, detail/consumption fallbacks
  cache.py            TTL cache with single-flight and stale fallback
  models.py           Public Pydantic v2 contract
  config.py           Environment settings
  static/index.html   Self-contained dashboard (vanilla HTML/CSS/JS, no build step)
scripts/
  export_openapi.py   Writes openapi.json without starting the app lifespan
tests/
  test_normalize.py   Pure parsing and consumption edge cases
  test_client.py      Auth, re-auth, retries, HMAC export, pagination, SSR parsing
  test_api.py         End-to-end API behavior through the real service
  test_cache.py       TTL, stale fallback and single-flight concurrency
PROTOCOL.md           Reconnaissance write-up
openapi.json          Committed OpenAPI 3.1 contract
render.yaml           Render blueprint for the public deployment
REFLECTION.md         Assumptions, difficult parts, mistakes, self-critique
```

---

## Assumptions

1. **The portal is read-only and will stay that way.** No state-changing calls are
   made. Authentication is only used to read data.
2. **The bulk export is the best source for the inventory.** It is complete,
   signed and includes hierarchy and location. The portal's search endpoint only
   matches ID/serial and is too narrow for the requested filters.
3. **The transformer registry is authoritative for DT names and capacities.**
   Meter rows sometimes use an old alias (for example, `DT-007` is
   `Old Malviya Nagar Xfmr` in one row and `Sanganer DT 7` everywhere else). The
   alias is preserved in `data_quality` rather than discarded.
4. **Hierarchy paths are per-meter claims, not an authoritative tree.** When a
   node is reported under several parents, the most frequent parent wins and the
   conflict is exposed.
5. **Energy timestamps are local to the configured zone (IST).** The portal sends
   timestamps without an offset, so `PORTAL_TIMEZONE` supplies one.
6. **Registers are cumulative, not interval counters.** Usage is a positive delta
   between consecutive readings; a negative delta is a regression/reset and is
   flagged instead of being converted into negative usage.
7. **The portal's default consumption window is acceptable.** Omitting dates uses
   the portal's 7-day window rather than inventing a default in this service.
8. **Data changes slowly enough for short TTLs.** A five-minute dataset snapshot
   is a deliberate availability/freshness trade-off; consumers can inspect
   `X-Data-Fetched-At`.
9. **One process, one tenant.** The cache and session live in process memory. This
   is sized for 403 meters and a single deployment, not a horizontally scaled
   multi-tenant service.
10. **Meter IDs are stable and unique.** They are used as the local index key and
    as the public path parameter.

## Design decisions and trade-offs

- **FastAPI + synchronous `httpx` client.** The legacy portal is conventional
  request/response. Sync routes run in FastAPI's threadpool, keeping the adapter
  straightforward and testable. An async rewrite adds complexity without a
  measured benefit at this scale; it is a natural next step under high concurrency.
- **Snapshot + local query instead of proxying portal search.** The portal's
  search can only match ID/serial, one page at a time. Pulling the signed export
  once enables status/make/hierarchy filters, sorting, global counts, CSV/JSON
  exports and proximity search that the portal cannot provide.
- **Pure normalisation layer.** `normalize.py` contains no HTTP or cache code, so
  both detail schemas, blank hierarchy halves, duplicate readings and mixed
  granularity are unit-tested without a portal. The adapter returns raw shapes;
  the models are the public contract.
- **Canonical DT registry, traceable aliases.** Resolving names through the
  registry fixes the obvious aliasing while keeping the original spelling in
  `data_quality` so a reviewer can audit the decision.
- **Most-frequent parent + explicit conflicts.** A tree cannot represent a DAG;
  choosing the most common parent is deterministic and avoids dropping meters.
  The endpoint returns all reported parents rather than hiding ambiguity.
- **Stale fallback only for portal errors.** If the loader fails with
  `PortalError` and an expired entry exists, the stale value is served with
  `X-Cache: stale`. Programming errors are not masked.
- **Server-owned portal session.** Clients never send portal credentials to this
  API. The service logs in and keeps the cookie in memory. This avoids turning the
  new API into a credential-forwarding proxy.
- **Optional shared API key.** It is useful for demos or an internal deployment,
  implemented with `secrets.compare_digest` (via `hmac.compare_digest`). It is not
  a substitute for user authentication, per-tenant authorization or rate limiting.
- **No build step for the UI.** `app/static/index.html` is plain HTML/CSS/JS with
  no external assets or CDN dependency, so the dashboard works offline and cannot
  break because a frontend toolchain changed. It deliberately uses a local
  scatterplot rather than a paid map tile service.
- **Explicit error mapping.** Upstream ambiguity is translated into stable
  public errors: not found, auth failure, protocol failure, unavailable, validation
  error. Internal details are logged, not leaked to clients.

## Intentional omissions

- **No persistent database.** The dataset is rebuilt from the portal on a TTL.
  This is appropriate for 403 meters; a warehouse or national rollout needs an
  indexed store and incremental sync.
- **No background refresh.** Loads are on demand. A scheduled warm-up would
  remove the first-request latency but adds a worker lifecycle.
- **No write operations or administrative tools.** The brief and the portal are
  read-only.
- **No per-user auth / multi-tenancy / RBAC.** The optional API key is a single
  shared secret.
- **No built-in rate limiting**, metrics exporter, tracing or request IDs. Those
  belong at the edge or in an observability layer.
- **No basemap or geocoding.** Proximity is Haversine math; the dashboard is a
  coordinate scatterplot.
- **No historical archive.** Consumption is limited by what the portal reports
  (the available window observed during reconnaissance was 2026-06-01 to
  2026-06-30, with a default 7-day view). This service does not fabricate history.
- **No Dockerfile, CI pipeline or deployment manifests.** The README run path is
  complete; packaging and CI are straightforward follow-ups.

## If I had another day

1. **Add an indexed local store (SQLite first).** Persist snapshots and meter
   history, then move filtering and proximity to SQL with FTS and a spatial
   index. That turns the current in-memory design into something that scales
   beyond a demo without changing the public contract.
2. **Background snapshot refresh with ETag/conditional GET.** Serve every read
   from a warm snapshot and expose ETags on JSON responses.
3. **Metrics and tracing.** Request latency, upstream latency, cache hit/stale
   ratios, login failures and export signature rejections, plus request IDs in
   logs.
4. **More integration coverage.** An opt-in, read-only live smoke test against the
   portal in CI (gated by credentials), plus load tests for concurrent cold
   requests and cache single-flight.
5. Production packaging. Dockerfile, healthcheck, pinned runtime, CI for tests
   + lint + OpenAPI drift, and a deployment example behind a reverse proxy.
6. Richer analytics. Per-DT and per-feeder consumption aggregates, anomaly
   trends, and a proper charting component on the dashboard.
7. Stronger auth story. Pluggable API keys with rotation/expiry, per-key rate
   limits, and optional OIDC for the dashboard.

## Deliverables

| Deliverable | Location |
| --- | --- |
| Source code | [`app/`](app/) |
| Reconnaissance & protocol notes | [`PROTOCOL.md`](PROTOCOL.md) |
| OpenAPI 3.1 contract | [`openapi.json`](openapi.json) (also `/openapi.json`) |
| Interactive docs | `/docs` (Swagger UI), `/reference` (Scalar) |
| Dashboard | [`app/static/index.html`](app/static/index.html), served at `/` |
| Tests | [`tests/`](tests/) |
| Assumptions, decisions, omissions | this README |
| Reflection answers | [`REFLECTION.md`](REFLECTION.md) |

## Data and credit

The portal and its data belong to Flock Energy and the exercise. The supplied
sandbox credentials are included only in `.env.example` for local evaluation;
replace them for any other use. This project is not affiliated with the portal.
