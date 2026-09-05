---
name: flight-fare-scanner
description: Searches, discovers and monitors low-fare flights, printing ranked results and generating PDF reports plus JSON audit files. Supports fixed routes, origin-to-anywhere relative low-fare discovery through Google Flights Deals, price history and optional email delivery. Pluggable data backends (Google Flights via SerpApi, or Amadeus GDS). Supports multi-stop connections, multi-city through-tickets, open-jaw, OPEN-return and split-ticket strategies. Use when the user wants to find cheap flights, discover unusually discounted destinations from an origin, compare airfares, track ticket prices, get price-drop alerts, or set up recurring flight price inspection - including mentions of 低价机票, 特价机票, 机票巡检, 机票比价, 机票价格监控, 降价提醒, 多段联程, 中转, 开口程, OPEN 票, Google Flights, or Skyscanner.
---

# Flight Fare Watch

Low-fare search and monitoring with a pluggable data backend. Results are
printed directly (the default), kept as price history in SQLite, and can be
emailed. No messaging platform is required.

## Data backends

```bash
python3 $S/fare_watch.py providers --config $CFG   # capability matrix
```

| | `google_flights` (default) | `amadeus` |
|---|---|---|
| Source | Google Flights via SerpApi | Amadeus GDS, official API |
| Key | self-serve, 100 free searches/mo | self-serve, free test tier |
| Airports per leg | **any number** | 1 + 2 alternatives |
| Alliance filter | **yes** (Star/SkyTeam/Oneworld) | no |
| Market price level | **yes** (low/typical/high) | no |
| Cheapest-dates in one call | no | **yes** (`cheapest_dates`) |
| Layover duration filter | **yes** | no |
| Force / ban a connection point | no | **yes** |
| ±3 day window (server-side) | no | **yes** |
| Refundable / no-penalty filter | no | **yes** |
| Bookable seat count | no | **yes** |

Both cap connections at 2 per itinerary, and both do multi-city and open-jaw.

**Unsupported options are rejected, never silently ignored.** Asking
`google_flights` for `via: [HKG]` fails with a concrete alternative rather than
quietly returning unfiltered results you would then trust. `validate` reports
this without spending a call.

**Skyscanner is not supported.** Its Flights API is partner-gated with no
self-serve signup, so it cannot be wired up from a personal machine. Google
Flights has no official API either, which is why that path goes through
SerpApi.

### City-to-airport preflight

Before creating a watch from a natural-language city, use WebSearch to identify
its common commercial airports, then configure airport IATA codes. Scheduled
runs never perform WebSearch: the local `city_airports.py` map expands a small,
verified set of metropolitan codes (`LON`, `NYC`, `TYO`, `PAR`, `ROM`) without
an API call. For this watch, `LON` becomes `LHR,LGW` before a Google Flights
request; other London airports are deliberately excluded. Unmapped codes are
treated as literal airports; do not assume that every three-letter city code is
accepted by SerpApi.

### 行李、签证与过境信息

PDF 会优先展示 provider 实际返回的托运行李信息：明确数量显示“含 N 件”，
模糊文本显示“可能包含，须以出票页确认”，缺失则显示“未返回”。不得根据
航司、舱位或历史经验推断行李额度。若报价未返回额度，PDF 可另列航司官网
最低经济舱公开参考（目前覆盖 QR/LA/IB/CX），但必须与“报价返回的托运行李”
分开展示，并明确最终以出票页的票价品牌和最主要承运人规则为准。

PDF 对已收录中转机场显示中文名称、实际停留时长，以及带公开来源链接的保守
规划建议。建议值不是机场 MCT、不是航司保障，也不能替代同一票号的最低衔接
时间；实际停留短于建议时标为“偏紧”。当前映射覆盖 DOH、JFK、LAX、MAD、
CDG、AMS。交互式维护映射时可先用 WebSearch 核实公开机场/航司/入境来源；
定时任务只读取本地映射，绝不额外联网。

默认按中国大陆普通护照（`traveler.passport_nationality: CN`）整理签证与过境
核验。交互式真实巡检在取得候选航段后，应以目的地及中转地的官方移民、外交
或使领馆来源进行 WebSearch，向用户说明来源链接和核验日期，再将摘要写入
`visa.entries` / `visa-cache.json`。签证结论始终标记为“须最终确认”。

定时任务绝不执行 WebSearch；只读取 `visa-cache.json`，报告必须标注“引用
缓存，非实时”。缓存缺失、过期或机场国家不明时显示“未完成官方核验”，而
不是给出肯定的过境或入境结论。

## Requirement clarification (required before a real search)

Before creating or changing a watch, spending an API call, or installing a
schedule, confirm the following with the user. If a detail is already explicit,
repeat it back for confirmation rather than asking again.

1. **Query type** — choose exactly one:
   - Fixed route: `oneway`, `roundtrip`, `flex_roundtrip`, or `focus_roundtrip`.
   - Flexible destination: `low_fare_discovery` (origin → anywhere); this must
     not be presented as a search for a specified destination.
   - Complex itinerary: `open_jaw`, `multi_city`, `open_return`, or
     `split_ticket`; state the ticketing and self-transfer implications.
2. **Airports and destination scope** — confirm the origin airport(s), any
   nearby airports, and, for a fixed route, the destination airport(s). Resolve
   natural-language cities with WebSearch before configuration. For a discovery
   watch, explicitly record that there is no fixed destination.
3. **Dates and trip shape** — confirm fixed outbound/return dates, or a search
   window plus trip length. For one-way searches, confirm the departure date or
   date window. For return trips, confirm the return date, `trip_days`, or an
   acceptable duration range. Never silently use today's date, a default month,
   or a default trip length.
4. **Search constraints** — confirm cabin, travellers, maximum stops, accepted
   airlines/alliance, price target, and whether nearby airports are acceptable.
   Explain that a normal itinerary supports at most two connections per leg.
5. **Hub positioning** — if comparing departure hubs, confirm the actual home
   airport, every candidate hub, whether separate PNRs are acceptable, and the
   minimum connection buffer. Do not imply a buffer supplies missed-connection
   protection.
6. **Monitoring cadence** — for recurring watches, confirm the interval and
   monthly API-call estimate before installing a schedule; do not activate a
   timer merely because a one-off search succeeded.

If type, destination scope, or dates are missing, ask concise follow-up
questions and stop before calling the provider. A safe question template is:

```text
1) 查询类型：单程、固定日期往返、日期范围内约 N 天往返，还是“从某地飞任意目的地”的低价发现？
2) 出发与目的地：哪些机场可接受？是否包含附近机场？
3) 日期：固定日期，还是从哪天到哪天；往返停留几天？
```

After clarification, run `validate --show-body` first. State the resulting API
call count, then run the real query only when it matches the confirmed scope.

## Setup in Codex (one time)

This is a Codex skill. Install the directory containing this file at
`$CODEX_HOME/skills/flight-fare-scanner` (the default is `~/.codex/skills`),
then restart or refresh Codex so it can discover the skill. If using the
Codex skill installer, install the repository path that contains `SKILL.md`.

On macOS/Linux:

```bash
CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
SKILL="$CODEX_HOME/skills/flight-fare-scanner"
STATE="$HOME/.flight-fare-scanner"
mkdir -p "$STATE"
cp "$SKILL/config.example.yaml" "$STATE/config.yaml"
cp "$SKILL/credentials.env.example" "$STATE/credentials.env"
chmod 600 "$STATE/credentials.env" "$STATE/config.yaml"
```

On Windows PowerShell:

```powershell
$CodexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' }
$Skill = Join-Path $CodexHome 'skills\flight-fare-scanner'
$State = Join-Path $HOME '.flight-fare-scanner'
New-Item -ItemType Directory -Force $State | Out-Null
Copy-Item (Join-Path $Skill 'config.example.yaml') (Join-Path $State 'config.yaml')
Copy-Item (Join-Path $Skill 'credentials.env.example') (Join-Path $State 'credentials.env')
```

1. **Required** — get a key for whichever backend you chose and put it in
   `credentials.env`:
   * `google_flights` → <https://serpapi.com/users/sign_up>, export `SERPAPI_KEY`
   * `amadeus` → <https://developers.amadeus.com/register>, export
     `AMADEUS_CLIENT_ID` / `AMADEUS_CLIENT_SECRET`
2. Edit `provider:` and `watches:` in `config.yaml`.
3. *Optional* — to email results, set `notify.email.enabled: true` with your
   SMTP host/recipients, and export `SMTP_USER` / `SMTP_PASSWORD`. Use an
   **app-specific password**; providers reject normal account passwords for
   SMTP.

Credentials live only in `credentials.env` (env vars). Never put them in the
config file, and never commit either file.

## Commands

In a macOS/Linux shell, always `source ~/.flight-fare-scanner/credentials.env`
before running a real query. In PowerShell, set the variables from the file in
the current session; the example file uses POSIX `export` syntax, so do not
dot-source it directly in PowerShell.

```bash
CFG=~/.flight-fare-scanner/config.yaml
S=$CODEX_HOME/skills/flight-fare-scanner/scripts

# Check config + build every request body. Zero API calls - do this first.
python3 $S/fare_watch.py validate --config $CFG --show-body

# Exercise the whole pipeline offline, no credentials needed.
python3 $S/fare_watch.py run --config $CFG \
    --mock $CODEX_HOME/skills/flight-fare-scanner/fixtures/sample-offers.json

# Real search. Prints a Markdown table of the cheapest offers per watch.
python3 $S/fare_watch.py run --config $CFG

# Plain-text output (nicer in a terminal); or machine-readable JSON.
python3 $S/fare_watch.py run --config $CFG --format text
python3 $S/fare_watch.py run --config $CFG --format json

# One watch only; email this run even if email is disabled in config.
python3 $S/fare_watch.py run --config $CFG --watch sha-bkk-rt --email

# Local only: never touch email/DingTalk.
python3 $S/fare_watch.py run --config $CFG --no-notify

# Recorded price history with a trend sparkline.
python3 $S/fare_watch.py history --config $CFG --watch sha-bkk-rt

# Resolve a city name to IATA codes (needs Amadeus credentials).
python3 $S/fare_watch.py airports --config $CFG --keyword shanghai

# Which backend is configured, and what can it do?
python3 $S/fare_watch.py providers --config $CFG
```

Exit codes: `0` ok · `1` all watches failed / invalid config · `2` credential
or API setup error.

## Output and delivery

Four independent sinks, each switchable under `notify:` in the config:

| Sink | Default | Purpose |
|---|---|---|
| `console` | **on** | prints to stdout; the primary way to read results |
| `report` | on | writes one PDF report plus a JSON audit file |
| `email` | off | SMTP plain text + HTML, with the PDF report attached |
| `dingtalk` | off | optional robot webhook; nothing depends on it |

**stdout carries only the report; all progress goes to stderr.** That keeps
`--format json` directly parseable and makes the output safe to paste
verbatim. Useful consequences:

```bash
# just the results, no progress noise
python3 $S/fare_watch.py run --config $CFG 2>/dev/null
# pipe structured results into another tool
python3 $S/fare_watch.py run --config $CFG --format json 2>/dev/null | jq '.results[0].price'
```

Flags: `--format markdown|text|json` overrides the configured format ·
`--quiet` suppresses stdout · `--email` forces one email even when disabled or
nothing is actionable · `--no-notify` skips the two outbound sinks only.

A local report writes a self-contained **PDF** plus a JSON audit file for every
run. The PDF contains price, strategy, actual route, airport expansions,
filters, alert signals, cheapest offers, Chinese transit-airport names, actual
layovers, conservative buffer guidance and baggage evidence. JSON remains the
machine-readable audit record. Existing Markdown reports are historical files
and are not removed.

## Scheduling

```bash
S=$CODEX_HOME/skills/flight-fare-scanner/scripts

# Weekly flexible-date discovery: 9 anchored searches per run.
$S/install_schedule.sh --watch hk-prd-lon-scout --interval 7d
# Daily discovery is opt-in: configure an explicit low_fare_discovery watch first.
$S/install_schedule.sh --watch <your-discovery-watch-id> --interval 1d
```

Each `--watch` installation gets an independent launcher, log and launchd
label (or crontab marker), so scout and focus never share a cadence. It validates
the selected watch and explicitly sources `credentials.env`. Remove an
individual job with `--uninstall --watch <id>`.

Fares update a few times a day; intervals under 15 minutes are rejected as
quota waste. Use `validate` after changing either schedule to see its 30-day
call estimate.

## Ticketing strategies

Set `strategy:` per watch. Full config reference: `config.example.yaml`.

| Strategy | Meaning | API calls | Notes |
|---|---|---|---|
| `oneway` | single leg | 1 | |
| `roundtrip` | A→B, B→A on fixed dates | 1 | |
| `flex_roundtrip` | **cheapest N-day round trip in a date window** | 1 per anchor date | ideal weekly scout for a two-month window |
| `focus_roundtrip` | re-check latest scout winner | 1 | resolves dates from `focus_from` snapshot |
| `hub_open_jaw_scout` | **出发枢纽 + 接驳票开口程比价** | 5 per date anchor | HKG direct vs positioned ICN; separate PNR risk |
| `low_fare_discovery` | **origin→anywhere 相对低价发现** | **1** | Google Flights Deals; destination-free, ranks discount then price |
| `cheapest_dates` | **cheapest date pairs in a window** | **1 total** | amadeus only; dates+price, no flight detail |
| `open_jaw` | 开口程: in via one city, home from another | 1 | `outbound` + `inbound` blocks |
| `multi_city` | **2–6 段联程：每段可有附近机场与 0–2 次中转** | 1 | `legs:` list, chronological, one ticket |
| `open_return` | OPEN 票 proxy: sweeps return dates, cheapest wins | 1 per date | see caveat below |
| `split_ticket` | 拆票自转: separate tickets, prices SUMMED | 1 per ticket | see risk below |

### Searching a date window (don't grid-sweep)

For "cheapest ~7-day round trip departing any time in Dec–Jan", pricing every
departure×return pair is hundreds of calls. Two cheaper routes:

```yaml
# ~9 calls, works on any backend, full flight detail
strategy: flex_roundtrip
depart_window: { from: 2026-12-01, to: 2027-01-31, step_days: 7 }
trip_days: 7            # or [6, 8]
```

```yaml
# 1 call, amadeus only, returns dates+price without flight detail
strategy: cheapest_dates
depart_window: { from: 2026-12-01, to: 2027-01-31 }
trip_days: 7
```

`flex_roundtrip` holds the trip length fixed and only anchors the departure
date, so cost is `anchors × trip lengths`; it refuses to exceed
`depart_window.max_probes` (default 12). Dates between anchors are not priced —
shrink `step_days` for finer resolution at proportional cost.

`cheapest_dates` pushes the whole search server-side via Amadeus Flight
Cheapest Date Search. Use it as a scout, then point a `roundtrip` watch at the
winning dates for flight numbers.

### Hub open-jaw and positioning tickets

`hub_open_jaw_scout` always searches the actual `home_origin` open jaw. Add
`positioning_hubs` (for example `ICN`) only when you want to compare a lower-fare
hub. Each configured hub creates four independent tickets — outbound positioning,
outbound main leg, inbound main leg, return positioning — and only keeps
combinations that satisfy `positioning.min_buffer_minutes` at the hub.
The PDF/JSON reports show the full main-trip dates, component prices, actual
buffers and PNR count.

A buffer is a filter, **not protection**: missed connections are not protected,
baggage may not be through-checked, and transit/entry requirements are the
traveller's responsibility. Confirm all timings and terms before booking.

```yaml
strategy: hub_open_jaw_scout
home_origin: HKG
positioning_hubs: [ICN]    # optional; omit/[] for home-origin direct only
outbound_destination: LIM
inbound_origin: GIG
depart_window: { from: 2027-04-01, to: 2027-06-24, step_days: 14, max_probes: 60 }
trip_days: 27
positioning: { min_buffer_minutes: 240 }
```

With no optional hub, each anchor costs one multi-city direct search. Each
configured positioning hub adds four one-way tickets, so HKG plus ICN costs five
calls per anchor. Do not schedule this unless the resulting quota estimate is
acceptable.

### Daily relative low-fare discovery

`low_fare_discovery` searches **from one configured origin to any destination**
using SerpApi's `google_flights_deals` engine. It does not impose a fixed price
threshold: returned deals rank by `discount_percentage` descending, then price.
It costs one search per run, so do not schedule it until choosing the following
values explicitly:

```yaml
strategy: low_fare_discovery
origin: PVG
nearby_origins: [SHA]       # optional
home_country: China          # optional; labels results domestic/international
discovery_type: roundtrip   # roundtrip | oneway
outbound_window: { from: 2026-10-01, to: 2026-11-30 }
trip_length: [5, 9]         # roundtrip only; or travel_duration: 1/2/3
max_deals: 20
schedule: { interval: 1d }
```

For `oneway`, omit `trip_length` and `travel_duration`. `home_country` must
match the textual country name returned by the provider (for example `China`);
it only labels the PDF/JSON results and never filters them. Fixed destinations,
`legs`, forced connections and return-date fields are rejected because Deals is
an origin→anywhere discovery API.

### Scout → focus monitoring

For a quota-constrained Google Flights watch, schedule `flex_roundtrip` as a
weekly **scout** and add a daily `focus_roundtrip` with `focus_from: <scout-id>`.
The scout's winning outbound/return pair is committed to SQLite; the focus job
reads that persisted pair, so the two jobs can run independently. A focus job
fails safely until the scout has produced its first snapshot.

### You often don't need price history at all

Both backends ship their own baseline, so a first run can already tell you
whether a price is good:

* `google_flights` returns `price_insights` — rendered as **Market level**
  (`low`/`typical`/`high`) plus the typical price band.
* Amadeus offers no equivalent, so there the SQLite history is what gives you a
  reference.

The history/alerting layer is what turns a one-off lookup into monitoring; for a
single "what's it cost right now" question, one `run` is enough.

Connection control, per leg: `max_stops` (0/1/2), `nearby_origins` /
`nearby_destinations` (extra airports). For a precise London search use
`destination: LHR` plus `nearby_destinations: [LGW]`; each `multi_city` leg
accepts the same pair of nearby-airport options. `alliances` (google_flights),
`airlines` / `exclude_airlines`, `layover_minutes` (google_flights), `via` /
`exclude_via` / `date_window` / `max_flight_time_pct` (amadeus).

### Two things no backend can do

**多次中转 is capped at 2 connections per itinerary.** Both backends top out
there (`maxNumberOfConnections` 0/1/2 on Amadeus, the `stops` bucket on Google
Flights). To route through more points, either name them explicitly with
`multi_city`, or use `split_ticket`.

**True OPEN (undated return) tickets are not retrievable.** Every backend
requires a date on each leg. `open_return` therefore prices a window of return
dates and reports the cheapest as a proxy — useful for finding the cheapest
return date, *not* a real open-dated fare. On `amadeus` you can additionally set
`refundable_only: true` / `no_penalty_only: true` and book a changeable fare; an
actual OPEN ticket needs a human agent or airline office.

**`split_ticket` means separate PNRs.** A missed connection is not protected,
baggage is not through-checked, you clear immigration and re-check bags at each
hand-off, and a transit visa may be required. The tool prints this warning on
every such result. Leave a wide buffer.

### Asking for full-service carriers only

On `google_flights`, use an explicit `airlines:` allow-list for a
full-service-only watch. The provider sends it to SerpApi **and verifies the
returned marketing-carrier codes locally**; an offer containing a code outside
the list, or no identifiable code, is excluded and the report says how many
were removed.

```yaml
# Typical China / London full-service starting point. Adjust to your acceptance.
airlines: [CX, BA, VS, LH, LX, AF, KL, AY, QR, EK, EY, TK, SQ, CA, MU, CZ]
```

`alliances:` is still useful for broad searches, but it omits independent
full-service carriers such as Qatar Airways (`QR`), Emirates (`EK`) and Etihad
(`EY`). On `amadeus`, likewise enumerate acceptable carrier codes in
`airlines:`.

## Alerting

Per-run comparison against the previous snapshot and all recorded history:

- `price_drop` — fell by `drop_abs` currency units **or** `drop_pct` percent
- `new_low` — beat the all-time recorded low
- `target_hit` — at or below the watch's `target_price`
- `market_low` — Google Flights marks the fare as low for the current market
  (can alert even on the first observation)
- `price_rise` — rose by `alert_on_rise_pct` (a warning, not a deal)
- `baseline` — first observation records the local baseline; `market_low` may
  additionally alert when Google has an immediate market-level signal

Repeat alerts of the same kind at the same price are suppressed for
`quiet_repeat_hours`. Outbound sinks default to `only_on_alert: true`, so email
stays quiet unless something is actionable; console and the local report show
every watch on every run.

## Where things land

| Path | Contents |
|---|---|
| `~/.flight-fare-scanner/config.yaml` | watches and thresholds |
| `~/.flight-fare-scanner/credentials.env` | API keys (chmod 600, never commit) |
| `~/.flight-fare-scanner/fares.sqlite3` | price history + alert log |
| `~/.flight-fare-scanner/reports/` | 每次巡检的 PDF 报告 + JSON 审计文件 |
| `~/.flight-fare-scanner/watch.log` | scheduled-run output |

## Quota

Each watch costs 1 API call per run, except `open_return` (one per candidate
date), `split_ticket` (one per ticket) and round trips with
`resolve_return: true` on google_flights (one extra). `validate` prints an
upper bound for each run and, for watches with `schedule.interval`, estimates a
30-day total against `quota.monthly_limit`.

Mind the free allowances: SerpApi gives **100 searches/month**. A discovery
watch scheduled daily costs about **30 calls/month** (or fewer when SerpApi
serves its one-hour cache), while the supplied scout + focus profile costs about
69. Keep all scheduled watches within `quota.monthly_limit`; `validate` warns
rather than silently lowering a schedule.

For `amadeus`, test and production keys are not interchangeable; `environment`
must match the key, and test data is a reduced cached subset, so prices there
are indicative, not bookable.

Prices from any backend are indicative. Confirm on the airline or agency site
before booking — this tool watches fares, it does not book them.

## Troubleshooting

Run `validate` first — it catches bad IATA codes, past/too-distant dates,
out-of-order legs, and options your chosen backend cannot honour, all without
spending quota.

For per-backend request/response contracts, error codes and per-sink delivery
troubleshooting (SMTP and DingTalk), see [reference.md](reference.md).

