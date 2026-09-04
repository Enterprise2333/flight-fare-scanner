# Reference

Details behind `SKILL.md`. Amadeus field names and limits come from the
`FlightOffersSearch_v2` OpenAPI specification; Google Flights parameters from
the SerpApi `google_flights` engine documentation.

## Architecture

```
strategies.expand(watch)  ->  ProbeSet of neutral base.Query
                                        |
                          provider.check(query)   <- rejects unsupported options
                                        |
                          provider.search(query)  -> neutral offers + meta
                                        |
                    store (SQLite) -> alert rules -> sinks

focus_roundtrip: latest scout snapshot -> concrete round-trip dates -> same path
```

`providers/base.py` defines the neutral `Query`/`Leg`/offer model and the
capability tokens. Nothing above the provider layer knows which backend is in
use; nothing below it knows about watches or alerting.

### The capability contract

Every optional search feature maps to a token (`via`, `date_window`,
`alliance_filter`, `fare_flex`, ...). A provider declares the tokens it
supports; `Query.required_capabilities()` reports the tokens a specific query
actually needs; `Provider.check()` raises when the difference is non-empty.

This exists because **silently dropping a filter is the dangerous failure
mode**. If `via: [HKG]` were ignored, the run would return unfiltered results
that look authoritative. Failing with "provider 'google_flights' does not
support `via` - name the stopover explicitly as its own leg with strategy
multi_city" is recoverable; a wrong answer you trust is not.

`fare_watch.py providers` prints the live matrix; it is generated from the
classes, so it cannot drift from the code.

## Backend: google_flights (SerpApi)

Google retired QPX in 2018 and ships no replacement, so Google Flights data
must come from a service that renders the page and returns JSON.

`GET https://serpapi.com/search?engine=google_flights`

| Parameter | Use |
|---|---|
| `departure_id` / `arrival_id` | IATA code, city kgmid, or **comma-separated list** |
| `type` | `1` round trip, `2` one way, `3` multi-city |
| `outbound_date` / `return_date` | `YYYY-MM-DD`; forbidden when `type=3` |
| `multi_city_json` | `[{departure_id, arrival_id, date, times?}, ...]` |
| `stops` | `1` nonstop, `2` ≤1 stop, `3` ≤2 stops (`0` = any) |
| `include_airlines` / `exclude_airlines` | IATA codes **or** `STAR_ALLIANCE`/`SKYTEAM`/`ONEWORLD`; mutually exclusive |
| `travel_class` | `1` economy … `4` first |
| `sort_by` | `2` = price; this tool always sorts by price |
| `layover_duration` | `"min,max"` minutes |
| `deep_search` | `true` matches the website exactly, slower |
| `currency`, `gl`, `hl`, `adults`, `children`, `infants_in_seat`, `max_price` | |

Response: `best_flights[]` + `other_flights[]`, each with `flights[]` (segments
with `departure_airport{id,time}`, `arrival_airport`, `duration` in **minutes**,
`airline`, `flight_number`, `travel_class`), `layovers[]`, `total_duration`,
`price` (bare number in the requested currency), `extensions[]` (where baggage
is stated in prose), `departure_token`, `booking_token`. Plus `price_insights`
with `price_level` (low/typical/high) and `typical_price_range`.

### City-code safety

The Google provider expands a curated set of metropolitan codes via
`scripts/city_airports.py` before constructing `departure_id` or `arrival_id`.
For this watch, `LON` becomes `LHR,LGW`; other London airports are deliberately
excluded. The map is intentionally local and versioned, so scheduled runs do
not make an additional WebSearch/API request. When configuring an unmapped city,
use WebSearch first and provide explicit airport codes rather than assuming its
IATA city code is accepted.

### 行李与签证/过境证据

Google Flights 的托运行李来自 `extensions` 文本，只有出现明确数量时才写入
“含 N 件托运行李”；模糊“已含行李”文本与缺失字段均不推断额度。Amadeus 的
`includedCheckedBags.quantity` 或 `weight` 则作为结构化行李信息显示。

`notify.PUBLIC_LOWEST_BAGGAGE` 是 QR/LA/IB/CX 的最低经济舱公开规则参考，
用于报价未返回行李字段时的独立 PDF 行；它绝不写回 `baggage_summary`，也不能
作为实际票价的行李权益。每条参考必须保留原始航司公开 URL，最终权益由出票页
的 fare brand、最主要承运人及联程协议决定。

`airport_transit.TRANSIT_GUIDES` 将 IATA 映射为中文机场名、保守规划预留分钟数、
原因及公开来源 URL。`notify._itinerary_cn()` 用相邻航段的 `arrive_at` 与
`depart_at` 计算实际停留；`_transit_guidance_cn()` 比较实际值与建议值，显示
“满足建议”或“短于建议，偏紧”。建议值不是 MCT，不得用于宣称航司误机保障。
该本地映射仅覆盖已核实的机场；定时运行不联网，交互式维护时才用 WebSearch
复核来源。

`visa-cache.json` 只存放人工通过官方移民、外交或使领馆页面核验后的摘要、来源
URL、核验时间、护照国籍、国家代码和 `destination`/`transit` 类型。脚本不会
自行做签证裁决。交互式运行可更新缓存；设置 `FARE_WATCH_SCHEDULED=1` 的定时
运行只读缓存并在报告中明确标记非实时。无匹配、过期或未知机场国家一律显示
“未完成官方核验”。

### Things to know

- **Round trip returns the total price but only the outbound itinerary.** The
  return legs are itemised only by a second call carrying `departure_token`.
  The tool says so in a note; `resolve_return: true` spends the extra call.
- `include_airlines` is sent upstream, then an explicit local `airlines:`
  allow-list is rechecked against every parsed marketing carrier. An offer with
  an unlisted or unidentifiable carrier is excluded and reported in `meta.notes`.
  Alliance-only filters cannot receive the same local recheck because the result
  has no reliable alliance-membership field.
- Segments arrive as one flat `flights[]` array even for multi-leg trips. They
  are regrouped into itineraries using the `layovers[]` markers, since a real
  connection produces a layover entry and a leg break does not.
- No seat count, no fare-condition filter, no forced/banned connection point,
  no server-side date window.
- Free plan: **100 searches/month**. A 429 means the monthly allowance or the
  rate limit is gone, and the tool does not retry it.
- This is scraped data. It mirrors what the Google Flights page shows, which is
  itself indicative rather than a booking guarantee.

## Backend: google_flights_deals (SerpApi)

`GET https://serpapi.com/search?engine=google_flights_deals` is the
origin→anywhere discovery surface. It deliberately has **no `arrival_id`**:
configure `departure_id` only, then receive relative deals across destinations.

| Parameter | Use |
|---|---|
| `departure_id` | one airport or comma-separated configured origin airports |
| `type` | `1` round trip, `2` one way |
| `outbound_date` | optional exact date or `from,to` flexible departure window |
| `trip_length` | round trip only; one value or `min,max` days |
| `travel_duration` | round trip only: `1` week, `2` weekend, `3` two weeks |
| `stops`, `include_airlines`, `exclude_airlines`, `travel_class` | same broad filtering semantics as normal Google Flights |

Each `deals[]` entry exposes `name`, `country`, `price`, `average_price`,
`discount_percentage`, dates, departure/arrival airport codes, airline and
stops. `low_fare_discovery` sorts by discount percentage descending, then price;
it does **not** apply a fixed amount threshold. Set optional `home_country` to
the same textual country name returned by Deals (such as `China`) to label each
entry domestic or international; classification never filters results.

## Backend: amadeus (GDS)

`POST /v2/shopping/flight-offers` — the POST form is required because only it
accepts `originDestinations`, and therefore multi-city, open-jaw and forced
connection points.

Auth: `POST /v1/security/oauth2/token`, `grant_type=client_credentials`. Tokens
last ~30 min and are cached at `~/.flight-fare-scanner/.token.json` (mode 600),
keyed by client id + environment so a cache is never reused across keys.
Hosts: `https://test.api.amadeus.com` / `https://api.amadeus.com` — a key is
valid for exactly one of them.

```jsonc
{
  "currencyCode": "CNY",
  "originDestinations": [            // 1..6 legs
    { "id": "1",
      "originLocationCode": "HKG",
      "destinationLocationCode": "LON",
      "departureDateTimeRange": { "date": "2026-12-10", "dateWindow": "I3D" },
      "includedConnectionPoints": ["DOH"],   // max 2
      "excludedConnectionPoints": ["PEK"],   // max 3
      "alternativeOriginsCodes": ["CAN","SZX"]  // max 2
    }
  ],
  "travelers": [{ "id": "1", "travelerType": "ADULT" }],
  "sources": ["GDS"],
  "searchCriteria": {
    "maxFlightOffers": 40,           // max 250
    "addOneWayOffers": true,
    "pricingOptions": { "includedCheckedBagsOnly": true, "refundableFare": true },
    "flightFilters": {
      "maxFlightTime": 200,
      "connectionRestriction": { "maxNumberOfConnections": 2 },  // 0|1|2 ONLY
      "cabinRestrictions": [{ "cabin": "ECONOMY", "coverage": "MOST_SEGMENTS",
                              "originDestinationIds": ["1"] }],
      "carrierRestrictions": { "includedCarrierCodes": ["CX"] }
    }
  }
}
```

### Constraints that silently break requests

- Legs must be **chronologically ordered**; out-of-order input returns a vague
  400. Checked locally first.
- Dates must be within **today .. +365 days**.
- `dateWindow` **cannot** combine with `originRadius`/`destinationRadius`.
- `maxNumberOfConnections` accepts only **0, 1, 2**.
- Carrier codes belong under `flightFilters.carrierRestrictions`, and
  baggage/refundability under `searchCriteria.pricingOptions`. One level too
  high is accepted as JSON but ignored by the API.
- One origin + **two** alternatives per leg. More than that is rejected with a
  pointer to the google_flights backend.

Response per offer: `price.grandTotal`, `price.currency`,
`validatingAirlineCodes`, `numberOfBookableSeats`, `lastTicketingDate`,
`itineraries[].duration` (ISO-8601), `itineraries[].segments[]`,
`segments[].numberOfStops` (**technical** stops inside one segment), and
`travelerPricings[0].fareDetailsBySegment[]` for cabin and
`includedCheckedBags`, joined by `segmentId`.

| Status | Meaning | Handling |
|---|---|---|
| 400 | bad parameter/combination | not retried; `errors[]` surfaced verbatim |
| 401 | token expired/revoked | token dropped, retried once |
| 429 | rate limit | retried honouring `Retry-After` |
| 5xx | upstream fault | retried up to 4 times |

Test environment serves a reduced cached subset: fine for validating the
pipeline and relative trends, not live and not bookable. Move to
`environment: production` with a production key before trusting an absolute
number.

## Neutral offer shape

Both backends emit this, so storage, alerting and rendering are backend-blind:

| Field | Notes |
|---|---|
| `price`, `currency` | float + ISO-4217 |
| `validating_airlines` | list of 2-char codes |
| `seats` | `None` when the backend has no such field |
| `itineraries[]` | `{path, duration, stops, depart_at, arrive_at, segments[]}` |
| `fingerprint` | sha1 of flight numbers + departure times |
| `extras` | backend-specific (`price_level`, `carbon_g`, `departure_token`, `baggage_summary`, `visa_context`) |

Connections are `len(segments) - 1` per itinerary. `fingerprint` lets an
unchanged price on a *different* routing still register as a change.

### Report provenance fields

Every successful result sent to console, local PDF/JSON and SMTP includes
`source`, `captured_at`, `airline_policy`, `connection_policy`, `probe_count`
and `estimated_calls`. The per-offer dates are taken from the requested
outbound/return pair even where Google omits the return itinerary. This makes a
result auditable without reading scheduler logs; actual stop counts remain in
each offer row rather than being inferred from the configured maximum. Chinese
airport names and transit-buffer guidance are resolved at PDF render time from
local, sourced metadata; historical JSON stays backward compatible.

## Strategy modelling

| Config | Legs produced |
|---|---|
| `roundtrip` A/B | `A→B` d1, `B→A` d2 (one call on both backends) |
| `flex_roundtrip` | `anchors × trip lengths` round-trip calls; cheapest wins |
| `focus_roundtrip` | latest stored winner from `focus_from`, rechecked as one fixed round trip |
| `cheapest_dates` | one date-grid call; the backend chooses the dates |
| `low_fare_discovery` | one destination-free Deals call; results ranked by discount then price |
| `hub_open_jaw_scout` | one direct open jaw per anchor, plus four one-way tickets for each optional positioning hub; only valid hub-time combinations remain |
| `open_jaw` | `A→B` d1, `C→A` d2 (C ≠ B) — `multi_city_json` on Google |
| `multi_city` | 2–6 chronological legs, one ticket; each leg may declare `nearby_origins` / `nearby_destinations` |
| `open_return` | one **call per candidate return date**; cheapest wins |
| `split_ticket` | one **call per ticket**; cheapest of each is **summed** |

`low_fare_discovery` calls Deals once; its stored current price is the
price of the highest-ranked relative deal, so a different destination may top a
later run. Treat its history as discovery activity, not a same-route fare trend.


`hub_open_jaw_scout` always includes the actual `home_origin`; `positioning_hubs`
is optional and defaults to empty. Each configured positioning hub adds four
separate tickets per date anchor. The strategy stores complete `journey_dates`,
the selected hub, component
prices, PNR count and actual hub buffers. A positioned hub is retained only if
both airport-local buffer comparisons meet `positioning.min_buffer_minutes`.
The four tickets are independent: a matching time window is not a protected
connection, baggage is not guaranteed through-checked, and transit/entry rules
remain the traveller's responsibility.

`open_jaw` rejects a config where both endpoints mirror each other, since that
is a plain round trip and would waste a call. Route labels mark a surface gap
with `//`, so an open jaw reads `PVG -> PAR // ROM -> PVG` rather than hiding
the different return origin. Split-ticket legs must price in the same currency
or the sum is refused instead of silently mixing units.

`focus_roundtrip` requires `focus_from`, `origin` and `destination`. The scout
writes its winner (including requested departure/return dates) into `snapshot.detail_json`.
The focus job reads the newest snapshot for that scout ID, so it works across
separate launchd/cron processes. It fails rather than guessing if no scout
snapshot exists or the snapshot lacks a complete round-trip date pair.

### Nearby destination airports

Use one explicit destination airport plus `nearby_destinations` for airports
the traveller accepts nearby. For example, `destination: LHR` with
`nearby_destinations: [LGW]` produces `arrival_id=LHR,LGW`. The return leg
automatically reverses the same sets. The same two keys are supported in every
`multi_city.legs[]` item, and `validate --show-body` displays the final
`multi_city_json` before any query is spent.

Empty results are not an error: usually the filters are too tight. Relax
`max_stops`, `max_price`, `travel_class`, `alliances`, or a forced `via`. In a
date sweep a single failing probe is logged and skipped, and the watch still
reports from the probes that succeeded.

### Date-window searches

The naive way to answer "cheapest 7-day round trip in a two-month window" is a
departure×return grid — ~62 × 62 pairs. Two cheaper routes exist.

**Quota-aware cadence.** Put `schedule: { interval: 7d }` on the scout and
`schedule: { interval: 1d }` on the focus watch. `validate` estimates each
scheduled watch over a 30-day month, includes the optional `resolve_return`
follow-up call, and compares the sum to `quota.monthly_limit`. The supplied
9-query weekly scout plus daily focus estimates about 69 SerpApi calls/month.

**`cheapest_dates`** — Amadeus `GET /v1/shopping/flight-dates`, **one call**:

```
origin=HKG&destination=LON&departureDate=2026-12-01,2027-01-31
&oneWay=false&duration=7&nonStop=false&viewBy=DURATION
```

`departureDate` and `duration` both accept inclusive `min,max` ranges, and
`viewBy` picks `DATE` / `DURATION` / `WEEK` granularity. The response is a list
of `{origin, destination, departureDate, returnDate, price.total}` — dates and
prices only, **no flight numbers**. Itineraries are therefore emitted with a
labelled `(dates only)` placeholder and `stops = None`, so the report shows `-`
rather than fabricating a nonstop. A `warnings[].title` of "Maximum response
size reached" means the grid was truncated. Route coverage is narrow, so an
empty result usually means the route is not covered rather than that no fares
exist. Only Amadeus declares `CAP_DATE_GRID`.

**`flex_roundtrip`** — the portable fallback. Anchors the departure date every
`step_days` across the window and holds `trip_days` fixed, so cost is
`anchors × trip lengths`, capped by `max_probes` (default 12). Works on any
backend and returns full flight detail. Dates between anchors are never priced,
which the report states explicitly. Because the requested outbound/return dates
are authoritative, both are stamped into `extras` so the Dates column is
correct even when a backend does not itemise the return leg.

**Why not Google Flights Deals?** SerpApi's `google_flights_deals` engine takes
`outbound_date` as a range plus `trip_length`/`travel_duration`, which looks
like an exact match for this job — and it even returns `average_price` and
`discount_percentage`. But it exposes **no `arrival_id`**: it is an
origin→anywhere discovery surface, so it cannot pin a destination and is
unusable for a fixed route. It is worth revisiting only for "where can I go
cheaply?" questions.

### Built-in price baselines

`google_flights` returns `price_insights.price_level` (`low`/`typical`/`high`)
and `typical_price_range`, surfaced as **Market level**. That answers "is this
cheap?" on the very first run, before any history exists. Amadeus has no
equivalent, so there the SQLite history is the only reference. Keep this in
mind before assuming the alerting layer is required: for a one-off lookup it is
not.

## Adding a backend

1. Subclass `base.Provider` in `providers/`, declare `name` and `CAPABILITIES`.
2. Implement `search(query) -> (offers, meta)` using `base.make_segment`,
   `base.make_itinerary` and `base.make_offer`; optionally
   `describe_request(query)` for `validate --show-body`.
3. Register it in `providers/PROVIDERS`.

Raise `base.CapabilityError` for anything the backend cannot express and
`base.ProviderError` for transport failures. Declaring a capability you do not
actually enforce is the one thing that breaks the contract, so keep
`CAPABILITIES` honest.

## Delivery sinks

All four sinks are independent and individually switchable. Delivery is
attempted **after** the snapshot is committed to SQLite, so no sink failure can
lose data. Each failure is reported on stderr and the run continues.

| Sink | Suppressed by | Gating default |
|---|---|---|
| `console` | `--quiet` | shows every watch |
| `report` | `enabled: false` | writes a PDF report plus JSON audit each run |
| `email` | `--no-notify` | `only_on_alert: true` |
| `dingtalk` | `--no-notify` | `only_on_alert: true` |

### Stream discipline

stdout receives only the rendered report. Progress lines, file paths and sink
status all go to stderr. This is what makes `--format json` safe to pipe and
the default output safe to quote verbatim. Do not add progress prints to stdout.

### Email (SMTP)

Messages contain a plain-text part (the same rendering as `--format text`) and
an HTML part with a real `<table>` of offers. When the report sink is enabled,
the run's PDF is attached. All interpolated values are HTML-escaped.

Config carries host, port, security and recipients; the username and password
are read only from the environment variables named by `user_env` /
`password_env`.

| Provider | Host | Port | `security` | Password to use |
|---|---|---|---|---|
| Gmail | smtp.gmail.com | 587 | starttls | app password (needs 2FA) |
| Outlook / M365 | smtp.office365.com | 587 | starttls | app password |
| QQ | smtp.qq.com | 465 | ssl | 授权码 |
| 163 | smtp.163.com | 465 | ssl | 授权码 |
| iCloud | smtp.mail.me.com | 587 | starttls | app-specific password |

| Symptom | Cause |
|---|---|
| `SMTP authentication failed (535)` | using the account password instead of an app-specific password / 授权码 |
| `Connection refused` / timeout | wrong port, or `security` should be `ssl` (465) rather than `starttls` (587) |
| `SMTPNotSupportedError: STARTTLS` | the server wants implicit TLS — switch `security: ssl` and port 465 |
| Sent but never arrives | provider silently spam-filtered it; check that `from` matches the authenticated `SMTP_USER` |
| `notify.email.to is empty` | recipients are config, not env — set `notify.email.to` |

`security: none` sends unencrypted and unauthenticated; it exists for local
relays and testing only.

### DingTalk (optional)

Nothing in the skill depends on this sink; it ships disabled.

| Symptom | Cause |
|---|---|
| `errcode 310000` | security setting rejected it: required keyword missing from the body, signature invalid, or source IP not allowlisted |
| `errcode 300001` | invalid or revoked `access_token` in the webhook URL |
| HTTP 200 but no message | robot removed from the group, or the group was archived |
| `@` did not notify | the mobile number must also appear in the message text (the sender adds it automatically) |

Signing is HMAC-SHA256 over `"<timestamp>\n<secret>"`, base64-encoded then
URL-encoded, appended as `&timestamp=&sign=`. Set `DINGTALK_SECRET` only when
the robot's security mode is "Signing"; sending a signature to a keyword-only
robot fails.

### Adding a sink

Sinks are dispatched in `_deliver()` in `fare_watch.py`. A new one needs a
renderer in `notify.py` plus a block in `_deliver` that honours `--no-notify`
for outbound delivery and catches `NotifyError` rather than propagating it.
`notify.select(results, only_on_alert)` gives the standard gating, and
`notify.serialise(results)` the plain-data form.

## Scheduled-run pitfalls

- **No inherited environment.** cron/launchd give you a near-empty env, so
  credentials exported in `~/.zshrc` are invisible. The generated launcher
  sources `credentials.env` explicitly for this reason.
- **launchd needs an absolute interpreter path.** The installer resolves
  `python3` at install time and writes the absolute path.
- **A sleeping Mac skips `StartInterval` firings.** `RunAtLoad` is set so a run
  happens on wake/login rather than waiting a full interval.
- Verify a scheduled job actually ran with `tail -f ~/.flight-fare-scanner/watch.log`;
  launchd's own stdout/stderr go to `launchd.out.log` / `launchd.err.log`.
