# Reflection

Five answers, in the order the brief asks them. The implementation details and
operating notes live in [README.md](README.md); the reconnaissance evidence is in
[PROTOCOL.md](PROTOCOL.md).

## 1. What assumptions did you make?

The biggest assumption was that **the portal is genuinely read-only for this
task**. I treated it that way in the adapter and never called a state-changing
route. That let me use a single server-owned session and focus on retrieval.

For the data model:

- **The signed bulk export is the authoritative inventory.** It is complete, has
  one consistent shape, and contains hierarchy and coordinates. The portal's
  search endpoint only matches meter ID/serial and cannot support status, make,
  feeder or zone filtering, so I made the export the primary source and filtered
  locally. Live per-meter detail is used only where it adds value.
- **The DT registry is authoritative for transformer names and capacities.**
  Meter rows contain at least one stale alias (`DT-007`: `Old Malviya Nagar Xfmr`
  vs `Sanganer DT 7`). The registry spelling wins, and the original is retained in
  `data_quality` so the decision is auditable.
- **Per-meter hierarchy paths are claims, not a tree.** When a node appears under
  multiple parents I choose the most frequently reported parent with a
  deterministic tie-break, and expose every reported parent as a conflict. This is
  a guess about which parent is "right", but it is a transparent, reproducible
  guess.
- **Registers are cumulative.** Usage is the positive difference between adjacent
  readings. The first reading has no delta, and a negative difference is flagged
  as a regression rather than converted into negative consumption.
- **Timestamps are in the configured portal timezone, IST by default.** The portal
  sends offset-less local strings; treating them as UTC would shift every reading
  by 5.5 hours.
- **The portal's available window is the limit of history.** At recon time the
  data ran from 2026-06-01 to 2026-06-30 and the default window was seven days. I
  did not invent a longer history or backfill.
- **Five-minute dataset freshness is acceptable.** The portal changes slowly
  enough for a cached snapshot, and every data response exposes
  `X-Data-Fetched-At` so a caller can decide whether that is fresh enough.
- **This is a single-process, single-tenant service.** In-memory session, cache
  and locks are proportional to 403 meters and one deployment. I did not pretend
  it was a distributed system.

The optional `API_KEY` is a shared deployment secret, not user identity. I
assumed reviewers would run locally with it disabled, which is the default.

## 2. What was the most difficult part, and how did you get unstuck?

The most difficult part was that the important contracts were not documented
anywhere: they were encoded in the frontend. Two examples stood out.

**The signed bulk export.** The visible page only showed a table, but the network
traffic hinted at a full export. I read the SvelteKit JavaScript chunks until I
found the export call and the signing helper. The helper joined `GET`,
`/portal/export`, the query string and a timestamp with newlines, then signed that
with HMAC-SHA256 using a key fetched from `/portal/keys`. I reproduced the
construction in a small Python script, compared signatures, and only then wired it
into the adapter. Getting the literal query string right mattered: the export
ignores `page`, but the signature still covers `page=1`. Once I understood that,
the whole snapshot strategy became possible.

**The meter detail page.** `/meters/{id}` is server-rendered HTML, not JSON. The
payload is a SvelteKit inline script containing a JavaScript (not JSON) array with
unquoted keys and nested data. Some meters use a legacy `parameterName` /
`parameterValue` schema; newer ones use a `classData` JSON string containing
`installed_meter`. My first parser broke on the second shape. I built a balanced
bracket scanner that ignores brackets inside strings, parsed the isolated array
with `json5`, and then normalised both schemas behind one function. The lesson was
to isolate one unknown at a time: first get the raw payload, then parse it, then
normalise it.

The other difficult area was structural rather than syntactic: **turning flat,
inconsistent paths into a hierarchy without lying about the data.** There are
blank codes, blank names, aliases and nodes with multiple parents. I resisted
"fixing" it by inventing a clean tree. Instead I made identity code-first where
possible, repaired only unambiguous blanks, chose the most frequent parent, and
published conflicts and data-quality flags. Reviewing the raw counts (54 parent
conflicts) was what convinced me to expose the mess rather than hide it.

How I got unstuck generally: short, disposable smoke scripts; recording exact
requests and responses; changing one variable at a time; and converting every
discovered quirk into a pure unit test so the knowledge did not live only in my
head.

## 3. What would you do with another day?

I would prioritise the jump from "excellent small service" to "operable service":

1. **Persistence and a real index.** Move the snapshot and meter history into
   SQLite, then implement filtering, full-text search and proximity with indexes.
   The current in-memory layer is correct for 403 meters but does not answer what
   happens at 400,000.
2. **Background refresh.** Refresh the snapshot on a schedule and serve reads from
   a warm copy, with ETags/conditional GETs. That removes cold-start latency and
   makes freshness a deliberate SLA rather than a side effect of traffic.
3. **Operational visibility.** Metrics for upstream latency, cache hit/stale
   ratios, login and signature failures, plus request IDs and tracing. A `/metrics`
   endpoint and structured JSON logs would be the first step.
4. **A live, opt-in contract test in CI.** Unit tests with a fake portal are fast
   and deterministic, but they cannot detect the portal changing shape. A nightly
   read-only smoke test gated by credentials would catch that.
5. **Packaging and delivery discipline.** Dockerfile, CI for tests/lint/OpenAPI
   drift, and a deployment example behind a reverse proxy with rate limiting.
6. **A stronger auth and tenancy story.** Multiple API keys with expiry/rotation,
   per-key limits, and optionally OIDC for humans. The current key is explicitly a
   shared secret.
7. **A more analytical dashboard.** Per-DT/feeder consumption aggregates, anomaly
   trends and a proper time-series chart instead of the current interval bar chart.

If forced to pick only one, it would be persistence plus background refresh,
because it changes the service's failure and scaling characteristics more than any
other single change.

## 4. What mistake did you make?

My most consequential early mistake was **assuming the bulk export was paginated
like the transformer registry.** It accepted a `page` parameter, so I built the
first loader as a page loop. Reconnaissance showed that the export ignores
pagination and always returns all 403 meters, while the signature still covers the
literal `page=1` query. I had designed around a behaviour the portal did not have.
I removed the loop and treated the export as one snapshot, but the detour cost time
and briefly made the adapter more complicated than the protocol warranted.

A smaller but instructive mistake was **trusting one sample detail page**. The
first meter I inspected used the legacy detail schema, so I initially normalised
only that. Sampling another build revealed `classData`. That is why the normaliser
now supports both schemas and why the test suite exercises both; it is also why I
stopped extrapolating from a single response.

In both cases the root cause was the same: I formed a model from a convenient
sample instead of verifying the contract across the dataset before building on it.

## 5. Reviewing your own submission, what would you criticise?

Plenty.

- **The scaling story is honest but limited.** A single in-memory snapshot and
  synchronous portal calls are fine for this dataset, but the design is not what I
  would ship for a real distribution utility. The README says so, but a reviewer
  is right to see this as a demo-grade architecture.
- **The hierarchy is still a heuristic.** Exposing conflicts is better than
  hiding them, but choosing the most frequent parent can still put a meter under
  the wrong substation. I would want an authoritative network registry or a
  human-in-the-loop reconciliation workflow before treating it as truth.
- **The tests are mostly fake-portal tests.** They are fast and cover the parsing
  and protocol edge cases I know about, but they cannot prove the live portal has
  not changed. Session-expiry, clock-skew and long-running-cache behaviour are
  especially under-tested against the real system.
- **Cold-start latency is visible.** The first request pays for login, export,
  registry and normalisation. There is no prewarm or background refresh. A
  readiness probe can trigger the load, but that is not the same as a warm service.
- **The optional API key is coarse.** It protects the API from casual access, not
  from an authenticated user, and it does not cover `/docs` or `/reference`. A
  public deployment would need a reverse proxy or real auth in front.
- **Observability is thin.** There are useful logs and cache headers, but no
  metrics, tracing or request IDs. Debugging a portal degradation in production
  would be harder than it should be.
- **The dashboard is intentionally dependency-free but not deeply tested.** I
  verified it against the live dataset (inventory, detail drawer, hierarchy, map,
  about, API-key prompt path), but it has no automated browser tests, and its
  accessibility and chart interaction are basic. It is a convenience surface, not
  the product.
- **Some API decisions are compromises.** For example, any non-404 upstream 4xx is
  mapped into the portal-error family, and per-meter consumption is still fetched
  synchronously on a cache miss. Both are defensible at this scale, but a larger
  deployment would want finer semantics and a queue/background fetch.
- **Packaging stops at "works locally."** No Dockerfile, CI or lockfile-level
  reproducibility. The README is complete enough to run, but a reviewer has to
  supply their own operational scaffolding.

What I am most confident about is the part that mattered most to the exercise:
the protocol is understood and documented, the messy data is normalised without
silently inventing facts, the failure modes are explicit, and the code is covered
by tests around the behaviours that were hard to discover.
