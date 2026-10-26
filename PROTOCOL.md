# PROTOCOL.md — How the Urja Meter Ops portal actually works

Reverse-engineered on 2026-10-26 against `https://urja-ops.flockenergy.tech`.
All findings come from an ordinary logged-in session: HTML, the browser-facing
SvelteKit bundles, and the portal's own XHR calls. The portal was only read from;
no write endpoints were used.

## 1. What the portal is

A SvelteKit (Node) application. Pages are server-rendered, but the interactive
parts call small JSON endpoints under `/portal/...`. Authentication is handled by
[better-auth](https://www.better-auth.com/) with a cookie session.

Public routes exposed by the SvelteKit manifest:

| Route | Purpose |
| --- | --- |
| `/login` | Sign-in form |
| `/meters` | Meter list (client-side search + pagination) |
| `/meters/[id]` | Meter detail (SSR, embeds a devalue payload) |
| `/transformers` | Distribution-transformer list + bulk export button |

## 2. Authentication

### Login

```
POST /login
Content-Type: application/x-www-form-urlencoded
Origin: https://urja-ops.flockenergy.tech
Referer: https://urja-ops.flockenergy.tech/login

email=operator%40urja.local&password=$PORTAL_PASSWORD
```

Important details:

* The form is handled by a SvelteKit **form action**. Without an `Origin` header
  the server rejects the request with `403 Cross-site POST form submissions are
  forbidden`. A plain `curl -X POST` fails until you add `Origin`.
* On success the server responds `200 application/json` with a redirect
  instruction (not an HTTP 3xx):

  ```json
  {"type":"redirect","status":303,"location":"/meters"}
  ```

* The useful part is the `Set-Cookie` header:

  ```
  Set-Cookie: __Secure-better-auth.session_token=<token>.<signature>;
              Max-Age=3600; Path=/; HttpOnly; Secure; SameSite=Lax
  ```

  The session lives for **1 hour**. There is no CSRF token and no bearer token.
* Wrong credentials return `200` with `{"type":"failure","status":401,...}` and a
  human-readable "Invalid email or password." message.
* The cookie is `Secure` and `HttpOnly`; it must be replayed on every request.

### Session inspection / logout

| Request | Result |
| --- | --- |
| `GET /api/auth/get-session` | `{"session":{"expiresAt":"...","token":"...",...},"user":{...}}` |
| `POST /api/auth/sign-out` | Requires `Origin`; revokes the session. Not used by this service. |

### Behaviour when unauthenticated

Two different behaviours, depending on the route family:

* `/portal/*` JSON endpoints → `401 application/json`
  `{"error":"unauthorized","message":"A valid session is required."}`
* Server-rendered pages such as `/meters/{id}` → `302 Location: /login`
* Unknown meter on a JSON endpoint → `404 {"error":"not_found","message":"Meter not found"}`
* Unknown meter on `/meters/{id}` → HTML 404 page.

## 3. Internal endpoints

All are `GET`, cookie-authenticated, and return JSON unless noted.

### 3.1 Meter search / list

```
GET /portal/meters/search?q={query}&page={n}
```

```json
{
  "data": [
    {
      "meterId": "J100001",
      "serialNo": "GE84132",
      "make": "L&T",
      "phaseType": "single",
      "installStatus": "Installed",
      "dtCode": "DT-002"
    }
  ],
  "total": 403,
  "page": 1,
  "pageSize": 20
}
```

* Fixed page size of 20. `page` is clamped to a minimum of 1.
* `q` is a case-insensitive substring match on **meterId or serialNo only**.
  Searching for a make (e.g. `genus`) therefore returns nothing; use the bulk
  export + a local index for richer filtering.
* Pages past the end return `data: []` with the real `total`.
* The `dtCode` here is useful, but the rest of the hierarchy is not included.

### 3.2 Distribution transformers

```
GET /portal/dts?page={n}
```

```json
{
  "data": [{"code":"DT-001","name":"Malviya Nagar DT 1","feederCode":"F-001","capacityKva":100}],
  "total": 40,
  "page": 1,
  "pageSize": 20
}
```

`q` is accepted but ignored. 20 rows per page. Capacity is a number (kVA).
The DT registry is the cleanest source of transformer names.

### 3.3 Meter location

```
GET /portal/meters/{meterId}/geo
```

```json
{"data":{"latitude":"26.822136543835608","longitude":"75.90718190602279"}}
```

Coordinates arrive as **strings** here, while the bulk export returns floats.
Coordinates are WGS84, clustered around Jaipur (lat ~26.79–27.04,
lng ~75.66–75.91).

### 3.4 Consumption history

```
GET /portal/meters/{meterId}/energy?from=YYYY-MM-DD&to=YYYY-MM-DD
```

```json
{
  "data": [
    {"timestamp":"23/06/2026 23:30","kwh":"42594.05","kvah":"46001.58","voltR":"231"},
    {"timestamp":"24/06/2026 00:00","kwh":"42594.47","kvah":"46002.02","voltR":"230"}
  ]
}
```

Observations:

* `kwh` and `kvah` are **cumulative meter registers**, not interval usage.
  Differences between consecutive rows are the useful consumption values.
* All numeric values are strings. Timestamps are `DD/MM/YYYY HH:MM`, with no
  timezone. The utility is in Jaipur, so IST (UTC+05:30) is the only sensible
  interpretation; this service states that assumption explicitly.
* With no parameters the portal returns a **7-day window** ending at the last
  available reading — in this dataset `30/06/2026 23:30`. That is the dataset's
  as-of date, **not** today's date.
* The available history is `2026-06-01` through `2026-06-30`. Wider requests are
  silently clamped to this window; requests entirely outside it return `[]`.
* `from` and `to` are inclusive calendar days in `YYYY-MM-DD`. Invalid values are
  **silently ignored** (falling back to the default window), and `from > to`
  returns an empty list. Both parameters are optional; `to` alone gives the 7
  days ending on `to`, `from` alone runs to the dataset end.
* The default window includes one extra leading reading (e.g. 337 rows =
  7×48 + 1) while an explicit `from` starts at 00:00, so row counts differ for
  the same nominal period. This service reports the actual returned window.
* **Granularity is not constant.** Many meters return 30-minute readings
  (337 rows for the default week); others return one reading per day
  (8 rows, all at 00:00). The client must not assume 30-minute intervals.
* The endpoint always returns `200` with `data: []` for an empty window; only an
  unknown meter produces a 404.

### 3.5 Bulk export (the good one)

The "Export all meters" button on `/transformers` does not just call a simple
endpoint. It first fetches a signing key and then HMAC-signs the export request
in the browser.

**Step 1 — fetch the signing key**

```
GET /portal/keys
-> {"data":{"signingSecret":"<32-char secret>"}}
```

**Step 2 — sign**

```
timestamp = unix seconds as a string
payload   = "GET\n/portal/export\n<exact query string>\n<timestamp>"
signature = HMAC-SHA256(signingSecret, payload), lowercase hex
```

Then:

```
GET /portal/export?page=1
x-timestamp: <timestamp>
x-signature: <signature>
Cookie: __Secure-better-auth.session_token=...
```

* Both the session cookie **and** a valid signature are required.
* The query string is signed literally; a signed `page=1` cannot be replayed as
  `page=1&x=1` (401 `signature_invalid`).
* Timestamps are freshness-checked. In testing, offsets of ±2 minutes were
  accepted and −5 minutes was rejected, i.e. the window is only a few minutes.
* `page` / `pageSize` are accepted but **ignored**: the response is always the
  full dataset.

Response (231 KB for 403 meters):

```json
{
  "data": [
    {
      "meterId": "J100001",
      "serialNo": "GE84132",
      "make": "L&T",
      "phaseType": "single",
      "installStatus": "Installed",
      "installType": "CT Operated",
      "build": "legacy",
      "dtCode": "DT-002",
      "hierarchy": {
        "zone":        {"name":"Jaipur Zone 2", "code":"Z-02"},
        "circle":      {"name":"Circle 2",      "code":"C-02"},
        "division":    {"name":"Division 2",    "code":"D-02"},
        "subdivision": {"name":"Subdivision 2", "code":"SD-02"},
        "substation":  {"name":"Substation 2",  "code":"SS-02"},
        "feeder":      {"name":"Feeder 2",      "code":"F-002"},
        "dt":          {"name":"Mansarovar DT 2","code":"DT-002"}
      },
      "geo": {"lat": 26.822136543835608, "lng": 75.90718190602279}
    }
  ],
  "total": 403
}
```

This is the best path for the whole dataset: one authenticated + signed request
returns every meter with its full hierarchy and coordinates.

### 3.6 Meter detail page (SSR)

```
GET /meters/{meterId}
```

The page embeds a SvelteKit `devalue` payload in an inline script:

```
data: [null,{type:"data",data:{user:{...}}},
       {type:"data",data:{
          meterId:"J100001",
          detail:{...},
          hierarchy:{...}
       },uses:{params:["id"]}}]
```

`detail` has **two different shapes**:

* Legacy meters (`"build":"legacy"`):

  ```json
  {"data":[
    {"parameterName":"Meter ID","parameterValue":"J100001"},
    {"parameterName":"Serial No","parameterValue":"GE84132"},
    {"parameterName":"Make","parameterValue":"L&T"},
    {"parameterName":"Phase Type","parameterValue":"single"},
    {"parameterName":"Installation Status","parameterValue":"Installed"},
    {"parameterName":"Installation Type","parameterValue":"CT Operated"}
  ]}
  ```

* v2 meters (`"build":"v2"`):

  ```json
  {"classData":"{\"installed_meter\":{\"MeterId\":\"J100004\",\"SerialNo\":\"SE65293\",\"Make\":\"Genus\",\"PhaseType\":\"single\",\"InstallationStatus\":\"Faulty\",\"InstallationType\":\"CT Operated\"}}"}
  ```

  i.e. `classData` is a **JSON string nested inside the payload**, with its own
  camelCase field names. The page's own JavaScript special-cases this.

The embedded `hierarchy` object uses display labels as keys and pre-formatted
strings as values:

```json
{"Meter ID":"J100001","Installation Status":"Installed","Installation Type":"CT Operated",
 "Zone":"Jaipur Zone 2 (Z-02)","Circle":"Circle 2 (C-02)","Division":"Division 2 (D-02)",
 "Subdivision":"Subdivision 2 (SD-02)","Sub Station":"Substation 2 (SS-02)",
 "Feeder":"Feeder 2 (F-002)","DT":"Mansarovar DT 2 (DT-002)"}
```

The parenthetical code is not always present or reliable; the bulk export's
structured hierarchy is the better source. The SSR page is still implemented in
the client as a fallback for single-meter lookups.

## 4. Data inventory (as observed)

| Thing | Count / values |
| --- | --- |
| Meters | 403 (`J100000` … `J100402`) |
| Makes | HPL 90, Genus 88, Secure 85, Allied 76, L&T 64 |
| Phase | single 277, three 126 |
| Install status | Installed 238, Faulty 90, Decommissioned 75 |
| Install type | Whole Current 205, CT Operated 198 |
| Build | legacy 238, v2 165 |
| Distribution transformers | 40 (`DT-001` … `DT-040`) |
| Zones | 3 (`Z-01`, `Z-02`, `Z-03`) |
| Feeders | 28 unique codes |
| Substations | 18 unique names / 19 codes (one blank) |
| Reported lat/lng | 26.788–27.037 N, 75.662–75.912 E |

## 5. Quirks and messy data

These matter more than the happy path:

1. **Two detail schemas.** Legacy meters expose `detail.data[]`; v2 meters expose
   `detail.classData` (a JSON string). Any consumer must handle both, and should
   not infer the shape from the meter ID.
2. **Numeric strings.** Energy values, coordinates (on `/geo`), and some SSR
   hierarchy values are strings. The bulk export is cleaner (floats for `geo`).
3. **Stale / anchored time window.** The dataset ends on 2026-06-30 even though
   the portal runs later. "Latest" means latest in the dataset.
4. **Mixed consumption granularity.** Some meters report every 30 minutes,
   others daily, both through the same endpoint. Interval spacing must be
   detected rather than assumed.
5. **Cumulative registers.** `kwh`/`kvah` are odometer-style values. The API
   computes per-interval deltas; it must tolerate resets/regressions instead of
   blindly subtracting.
6. **Hierarchy blanks and mismatches.** Codes and names are independently
   missing at some levels:
   - 5 circles have a blank name, 6 have a blank code;
   - 3 substations have a blank code;
   - 3 feeders have a blank name, 5 have a blank code.
   Blank values create duplicate-looking nodes (e.g. `{"name":"Circle 1","code":""}`
   and `{"name":"","code":"C-01"}`). This service derives the missing half when
   it is unambiguous (name ↔ level+number ↔ code) and records the aliases.
7. **Duplicate DT name.** `DT-007` is called "Sanganer DT 7" in the DT registry
   (`/portal/dts`) and in most meter rows, but "Old Malviya Nagar Xfmr" for some
   meters. The registry name is treated as canonical; the alias is retained.
8. **Hierarchy is not a strict tree.** Some codes are reported under multiple
   parents (e.g. `D-03` appears under more than one circle). The export is a set
   of per-meter paths, not a normalised tree. Reconstructing a tree requires a
   deterministic rule for choosing a parent; this service uses the most
   frequently reported parent and documents/report the conflicts.
9. **Silent parameter handling.** Invalid energy dates fall back to the default
   window; unknown query parameters are ignored. Validation must therefore
   happen in the new API layer, not be assumed from the portal.
10. **Search is narrow.** `q` searches meter ID and serial only, case-insensitively;
    it does not search make, status or hierarchy.
11. **No pagination on export.** `page`/`pageSize` on `/portal/export` are
    ignored; the response is always the full 403-meter dataset.
12. **Two auth failure modes.** JSON endpoints return 401; SSR pages redirect.
    An adapter must treat both as "session expired" and re-login.

## 6. Error envelope

Portal JSON errors share a shape:

```json
{"error":"unauthorized","message":"A valid session is required."}
{"error":"not_found","message":"Meter not found"}
{"error":"signature_invalid","message":"Missing or invalid request signature."}
```

There are no rate-limit headers or documented quotas. Requests were kept
sequential and gentle during reconnaissance.

## 7. What this service chooses to use

| Need | Source |
| --- | --- |
| Meter list/detail/hierarchy/geo | Signed bulk export (one request, cached) |
| Single-meter detail fallback | SSR `/meters/{id}` + devalue parsing |
| Consumption | `/portal/meters/{id}/energy` per meter, cached briefly |
| Transformer registry | `/portal/dts` (paged, cached) |
| Session health | Login response cookie expiry + `/api/auth/get-session` |

The bulk export is the primary path because it is complete, signed and clean;
the narrower endpoints are used for freshness-sensitive consumption data and as
fallbacks. See `README.md` for the API design and `REFLECTION.md` for the
trade-offs.
