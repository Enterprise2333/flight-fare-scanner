"""Turn a watch definition into provider-neutral searches ("probes").

A watch may need more than one search:

  combine="min"  the cheapest result across probes wins. Used by date sweeps
                 (OPEN-return proxy) and nearby-airport fan-out on backends
                 that cannot express multiple airports in one call.
  combine="sum"  every probe must succeed and their cheapest offers are added
                 up. Used by split-ticket (self-transfer) strategies.

This module knows nothing about any specific API. It validates what is
universally checkable (IATA shape, date sanity, chronological order) and leaves
backend-specific limits to each provider's capability check.
"""

import copy
import datetime
import json
import re

from providers import base

MAX_LEGS = 6
DATE_WINDOW_RE = re.compile(r"^[MPI][1-3]D$")
IATA_RE = re.compile(r"^[A-Z]{3}$")

STRATEGIES = (
    "oneway", "roundtrip", "open_jaw", "multi_city", "open_return",
    "split_ticket", "flex_roundtrip", "cheapest_dates", "focus_roundtrip",
    "low_fare_discovery", "hub_open_jaw_scout",
)

# Options a watch may override on top of `defaults`.
OPTION_KEYS = (
    "currency", "adults", "children", "infants", "travel_class", "max_stops",
    "max_offers", "max_price", "airlines", "exclude_airlines", "alliances",
    "refundable_only", "no_penalty_only", "checked_bags_only",
    "layover_minutes", "max_flight_time_pct", "market", "language",
)


class ConfigError(Exception):
    pass


class Probe(object):
    """One neutral search plus the metadata needed to report on it."""

    def __init__(self, key, label, query, leg_summary, metadata=None):
        self.key = key
        self.label = label
        self.query = query
        self.leg_summary = leg_summary
        self.metadata = metadata or {}


class ProbeSet(object):
    def __init__(self, watch_id, label, strategy, combine, probes, notes, route=None,
                 sort_mode="price"):
        self.watch_id = watch_id
        self.label = label
        self.strategy = strategy
        self.combine = combine
        self.probes = probes
        self.notes = notes
        self.route = route or (probes[0].leg_summary if probes else "")
        self.sort_mode = sort_mode


# --------------------------------------------------------------- validation

def _codes(value, field, limit=None):
    """Normalise one code or a list of codes to a list of IATA codes."""
    if value is None or value == "":
        return []
    if isinstance(value, str):
        value = [part.strip() for part in value.split(",") if part.strip()]
    codes = []
    for item in value:
        code = str(item).strip().upper()
        if not IATA_RE.match(code):
            raise ConfigError(
                "{0}='{1}' is not a 3-letter IATA code. Use `fare_watch.py "
                "airports --keyword <city>` to look one up.".format(field, item))
        codes.append(code)
    if limit and len(codes) > limit:
        raise ConfigError("{0} accepts at most {1} codes, got {2}".format(
            field, limit, len(codes)))
    return codes


def _date(value, field):
    if isinstance(value, datetime.date):
        parsed = value
    else:
        try:
            parsed = datetime.datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
        except ValueError:
            raise ConfigError("{0}='{1}' must be YYYY-MM-DD".format(field, value))
    today = datetime.date.today()
    if parsed < today:
        raise ConfigError("{0}={1} is in the past".format(field, parsed))
    if parsed > today + datetime.timedelta(days=365):
        raise ConfigError(
            "{0}={1} is more than 365 days out; flight backends do not sell that "
            "far ahead".format(field, parsed))
    return parsed.isoformat()


def _airlines(value, field):
    """Airline codes are 2 chars; alliances are separated out by the caller."""
    if not value:
        return []
    if isinstance(value, str):
        value = [part.strip() for part in value.split(",") if part.strip()]
    out = []
    for item in value:
        code = str(item).strip().upper()
        if code in base.ALLIANCES:
            raise ConfigError(
                "'{0}' is an alliance, not an airline code - put it in "
                "`alliances:` instead of `{1}:`".format(code, field))
        if not re.match(r"^[0-9A-Z]{2}$", code):
            raise ConfigError(
                "{0}='{1}' is not a 2-character IATA airline code".format(field, item))
        out.append(code)
    return out


def _alliances(value):
    if not value:
        return []
    if isinstance(value, str):
        value = [part.strip() for part in value.split(",") if part.strip()]
    out = []
    for item in value:
        code = str(item).strip().upper().replace("-", "_").replace(" ", "_")
        if code not in base.ALLIANCES:
            raise ConfigError(
                "alliances='{0}' is not recognised; allowed: {1}".format(
                    item, ", ".join(base.ALLIANCES)))
        out.append(code)
    return out


# ------------------------------------------------------------- leg building

def _leg(spec, index, defaults):
    """Build a neutral Leg from a config leg spec."""
    field = "legs[{0}]".format(index)
    origins = _codes(spec.get("from") or spec.get("origin"), field + ".from")
    destinations = _codes(spec.get("to") or spec.get("destination"), field + ".to")
    if not origins:
        raise ConfigError(field + ".from is required")
    if not destinations:
        raise ConfigError(field + ".to is required")

    # Extra airports may be listed inline ("HKG,CAN") or via nearby_*.
    origins += _codes(spec.get("nearby_origins"), field + ".nearby_origins")
    destinations += _codes(spec.get("nearby_destinations"),
                           field + ".nearby_destinations")

    window = spec.get("date_window") or defaults.get("date_window")
    if window:
        window = str(window).upper()
        if not DATE_WINDOW_RE.match(window):
            raise ConfigError(
                "date_window='{0}' is invalid. Allowed: I1D/I2D/I3D (+/-), "
                "P1D..P3D (after), M1D..M3D (before).".format(window))

    return base.Leg(
        origins=_dedupe(origins),
        destinations=_dedupe(destinations),
        date=_date(spec.get("date"), field + ".date"),
        date_window=window,
        time_window=spec.get("time_window"),
        times=spec.get("times"),
        via=_codes(spec.get("via"), field + ".via"),
        exclude_via=_codes(spec.get("exclude_via"), field + ".exclude_via"),
    )


def _dedupe(codes):
    seen = set()
    out = []
    for code in codes:
        if code not in seen:
            seen.add(code)
            out.append(code)
    return out


def _query(legs, options):
    if not legs:
        raise ConfigError("at least one leg is required")
    if len(legs) > MAX_LEGS:
        raise ConfigError(
            "a single ticket supports at most {0} legs; split the trip into "
            "multiple watches or use strategy 'split_ticket'. Got {1}.".format(
                MAX_LEGS, len(legs)))
    _assert_chronological(legs)

    travel_class = options.get("travel_class")
    if travel_class:
        travel_class = str(travel_class).upper()
        if travel_class not in base.CABINS:
            raise ConfigError("travel_class must be one of {0}".format(
                ", ".join(base.CABINS)))

    max_stops = options.get("max_stops", options.get("max_connections"))
    if max_stops is not None:
        max_stops = int(max_stops)
        if max_stops not in (0, 1, 2):
            raise ConfigError(
                "max_stops must be 0, 1 or 2 - both backends cap connections per "
                "itinerary at 2. For deeper routings name the stopovers with "
                "strategy 'multi_city', or use 'split_ticket'. Got {0}.".format(
                    max_stops))

    layover = options.get("layover_minutes")
    if layover is not None:
        if not (isinstance(layover, (list, tuple)) and len(layover) == 2):
            raise ConfigError("layover_minutes must be [min, max] in minutes")
        layover = (int(layover[0]), int(layover[1]))
        if layover[0] > layover[1]:
            raise ConfigError("layover_minutes min must not exceed max")

    max_offers = int(options.get("max_offers", 40))
    if max_offers < 1:
        raise ConfigError("max_offers must be at least 1")

    return base.Query(
        legs=legs,
        adults=options.get("adults", 1),
        children=options.get("children", 0),
        infants=options.get("infants", 0),
        currency=options.get("currency", "CNY"),
        travel_class=travel_class,
        max_stops=max_stops,
        airlines=_airlines(options.get("airlines"), "airlines"),
        exclude_airlines=_airlines(options.get("exclude_airlines"),
                                   "exclude_airlines"),
        alliances=_alliances(options.get("alliances")),
        max_price=options.get("max_price"),
        max_offers=max_offers,
        refundable_only=options.get("refundable_only", False),
        no_penalty_only=options.get("no_penalty_only", False),
        checked_bags_only=options.get("checked_bags_only", False),
        layover_minutes=layover,
        max_flight_time_pct=options.get("max_flight_time_pct"),
        market=options.get("market"),
        language=options.get("language"),
    )


def _discovery_query(options, discovery):
    """Build a destination-free query for relative low-fare discovery."""
    travel_class = options.get("travel_class")
    if travel_class:
        travel_class = str(travel_class).upper()
        if travel_class not in base.CABINS:
            raise ConfigError("travel_class must be one of {0}".format(
                ", ".join(base.CABINS)))
    max_stops = options.get("max_stops", options.get("max_connections"))
    if max_stops is not None:
        max_stops = int(max_stops)
        if max_stops not in (0, 1, 2):
            raise ConfigError("max_stops must be 0, 1 or 2")
    max_offers = int(options.get("max_offers", 40))
    if max_offers < 1:
        raise ConfigError("max_offers must be at least 1")
    return base.Query(
        legs=[], adults=options.get("adults", 1),
        children=options.get("children", 0), infants=options.get("infants", 0),
        currency=options.get("currency", "CNY"), travel_class=travel_class,
        max_stops=max_stops, airlines=_airlines(options.get("airlines"), "airlines"),
        exclude_airlines=_airlines(options.get("exclude_airlines"),
                                   "exclude_airlines"),
        alliances=_alliances(options.get("alliances")),
        max_price=options.get("max_price"), max_offers=max_offers,
        market=options.get("market"), language=options.get("language"),
        discovery=discovery)


def _discovery_window(value):
    """Optional deals date window as a pair of validated ISO dates."""
    if value is None:
        return None
    return _window(value, "outbound_window")


def _discovery_type(value):
    choice = str(value or "roundtrip").lower()
    if choice not in ("oneway", "roundtrip"):
        raise ConfigError("discovery_type must be 'oneway' or 'roundtrip'")
    return choice


def _home_country(value):
    """Optional display-country matcher used to label discovery results."""
    if value is None or value == "":
        return None
    country = str(value).strip()
    if not re.match(r"^[A-Za-z][A-Za-z .'-]{1,63}$", country):
        raise ConfigError(
            "home_country must match the provider's textual country name, e.g. China")
    return country.casefold()
def _assert_chronological(legs):
    previous = None
    for leg in legs:
        if previous and leg.date < previous:
            raise ConfigError(
                "legs must be in chronological order: {0} comes after {1}".format(
                    previous, leg.date))
        previous = leg.date


def _leg_label(legs):
    """Route string, marking surface gaps with '//'.

    An open jaw must not collapse to 'PVG -> PAR -> PVG': that would hide the
    fact that the return departs from a different city.
    """
    if not legs:
        return ""
    chunks = []
    previous_to = None
    for leg in legs:
        origin = "/".join(leg.origins)
        destination = "/".join(leg.destinations)
        if previous_to is not None and leg.origin == previous_to:
            chunks[-1] += " -> " + destination
        else:
            chunks.append(origin + " -> " + destination)
        previous_to = leg.destination
    return " // ".join(chunks)


# -------------------------------------------------------------- strategies

def _leg_specs(watch):
    """Normalise every single-ticket strategy to a list of leg specs."""
    strategy = watch["strategy"]

    if strategy == "oneway":
        return [{
            "from": watch.get("origin"), "to": watch.get("destination"),
            "date": watch.get("depart"),
            "via": watch.get("via"), "exclude_via": watch.get("exclude_via"),
            "nearby_origins": watch.get("nearby_origins"),
            "nearby_destinations": watch.get("nearby_destinations"),
            "date_window": watch.get("date_window"), "times": watch.get("times"),
        }]

    if strategy == "roundtrip":
        if not watch.get("return"):
            raise ConfigError("roundtrip requires 'return'")
        return [
            {"from": watch.get("origin"), "to": watch.get("destination"),
             "date": watch.get("depart"), "via": watch.get("via"),
             "exclude_via": watch.get("exclude_via"),
             "nearby_origins": watch.get("nearby_origins"),
             "nearby_destinations": watch.get("nearby_destinations"),
             "date_window": watch.get("date_window"), "times": watch.get("times")},
            {"from": watch.get("destination"), "to": watch.get("origin"),
             "date": watch.get("return"), "via": watch.get("return_via"),
             "exclude_via": watch.get("exclude_via"),
             "nearby_origins": watch.get("nearby_destinations"),
             "nearby_destinations": watch.get("nearby_origins"),
             "date_window": watch.get("return_date_window")
                            or watch.get("date_window")},
        ]

    if strategy == "open_jaw":
        outbound = watch.get("outbound") or {}
        inbound = watch.get("inbound") or {}
        if not outbound or not inbound:
            raise ConfigError("open_jaw requires both 'outbound' and 'inbound'")
        out_from = _codes(outbound.get("from"), "outbound.from")
        out_to = _codes(outbound.get("to"), "outbound.to")
        in_from = _codes(inbound.get("from"), "inbound.from")
        in_to = _codes(inbound.get("to"), "inbound.to")
        if out_to == in_from and out_from == in_to:
            raise ConfigError(
                "open_jaw legs form a plain round trip (same city pair both "
                "ways). Use strategy 'roundtrip' instead, or change an endpoint.")
        return [dict(outbound), dict(inbound)]

    if strategy == "multi_city":
        legs = watch.get("legs") or []
        if len(legs) < 2:
            raise ConfigError("multi_city requires at least 2 legs")
        return [dict(leg) for leg in legs]

    raise ConfigError("strategy '{0}' has no single-ticket form".format(strategy))


def _sweep_dates(window, field):
    start = _date(window.get("from"), field + ".from")
    end = _date(window.get("to"), field + ".to")
    step = int(window.get("step_days", 1) or 1)
    if step < 1:
        raise ConfigError(field + ".step_days must be >= 1")
    start_date = datetime.date.fromisoformat(start)
    end_date = datetime.date.fromisoformat(end)
    if end_date < start_date:
        raise ConfigError(field + ".to must not be before .from")

    dates = []
    cursor = start_date
    while cursor <= end_date:
        dates.append(cursor.isoformat())
        cursor += datetime.timedelta(days=step)
    max_probes = int(window.get("max_probes", 12))
    if len(dates) > max_probes:
        raise ConfigError(
            "{0} expands to {1} API calls (limit {2}). Increase step_days, narrow "
            "the window, or raise max_probes deliberately.".format(
                field, len(dates), max_probes))
    return dates


def _trip_days(value, field):
    """Trip length in days: a single number or an inclusive [min, max]."""
    if value is None:
        raise ConfigError(field + " is required (days, e.g. 7 or [6, 8])")
    if isinstance(value, (list, tuple)):
        if len(value) != 2:
            raise ConfigError(field + " must be a number or [min, max]")
        low, high = int(value[0]), int(value[1])
    else:
        low = high = int(value)
    if low < 1:
        raise ConfigError(field + " must be at least 1 day")
    if low > high:
        raise ConfigError(field + " min must not exceed max")
    if high > 60:
        raise ConfigError(field + " above 60 days is not supported")
    return low, high


def _window(value, field):
    """A {from, to} departure window, validated as dates."""
    if not value:
        raise ConfigError(field + " is required ({from, to[, step_days]})")
    return (_date(value.get("from"), field + ".from"),
            _date(value.get("to"), field + ".to"))


def validate_focus(watch):
    """Validate a dynamic focus watch without needing its scout snapshot."""
    scout_id = str(watch.get("focus_from") or "").strip()
    if not scout_id:
        raise ConfigError(
            "focus_roundtrip requires `focus_from: <scout-watch-id>`")
    if scout_id == watch.get("id"):
        raise ConfigError("focus_roundtrip cannot use itself as focus_from")
    if not watch.get("origin") or not watch.get("destination"):
        raise ConfigError(
            "focus_roundtrip requires its own origin and destination so the "
            "focused query keeps the intended route and airport set")
    return scout_id


def materialize_focus_roundtrip(watch, scout_snapshot):
    """Turn the latest scout winner into a concrete `roundtrip` watch.

    The stable cross-run contract is the snapshot's stored winning offer, not a
    fragile in-memory hand-off. This lets a daily focus job reuse the latest
    weekly scout result even when the jobs run in separate processes.
    """
    scout_id = validate_focus(watch)
    if not scout_snapshot:
        raise ConfigError(
            "focus_roundtrip '{0}' has no stored scout result from '{1}'. Run "
            "the scout watch first.".format(watch.get("id"), scout_id))
    try:
        detail = scout_snapshot.get("detail_json") or {}
        if isinstance(detail, str):
            detail = json.loads(detail)
        best = detail.get("best") or {}
        extras = best.get("extras") or {}
        depart = extras.get("departure_date")
        back = extras.get("return_date")
        itineraries = best.get("itineraries") or []
        if not depart and itineraries:
            depart = itineraries[0].get("depart_at")
        if not back and len(itineraries) > 1:
            back = itineraries[-1].get("depart_at")
        depart = str(depart or "")[:10]
        back = str(back or "")[:10]
    except (TypeError, ValueError, KeyError) as exc:
        raise ConfigError(
            "focus_roundtrip could not read dates from scout '{0}': {1}".format(
                scout_id, exc))
    if not depart or not back:
        raise ConfigError(
            "focus_roundtrip needs a scout whose winning offer has both outbound "
            "and return dates. Ensure the scout uses a round-trip strategy.")

    resolved = copy.deepcopy(watch)
    resolved["strategy"] = "roundtrip"
    resolved["depart"] = depart
    resolved["return"] = back
    resolved.pop("focus_from", None)
    return resolved, {
        "scout_watch_id": scout_id,
        "scout_captured_at": scout_snapshot.get("captured_at"),
        "departure_date": depart,
        "return_date": back,
    }


def expand(watch, defaults):
    """Expand one watch into a ProbeSet of neutral queries."""
    strategy = watch.get("strategy")
    if strategy not in STRATEGIES:
        raise ConfigError("unknown strategy '{0}'; allowed: {1}".format(
            strategy, ", ".join(STRATEGIES)))
    watch_id = watch.get("id")
    if not watch_id:
        raise ConfigError("every watch needs a unique 'id'")

    options = dict(defaults)
    options.update({key: value for key, value in watch.items()
                    if key in OPTION_KEYS and value is not None})
    # `max_connections` is the older name for max_stops; honour both.
    if watch.get("max_connections") is not None:
        options["max_stops"] = watch["max_connections"]
    label = watch.get("label") or watch_id
    notes = []

    if strategy == "hub_open_jaw_scout":
        home = _codes(watch.get("home_origin"), "home_origin")
        if len(home) != 1:
            raise ConfigError("hub_open_jaw_scout requires exactly one home_origin")
        # Positioning hubs are opt-in. With no field the watch searches only
        # the traveller's actual origin as one protected main-ticket candidate.
        # `hubs` remains a backward-compatible alias that may include home_origin.
        raw_positioning_hubs = watch.get("positioning_hubs")
        legacy_hubs = raw_positioning_hubs is None and watch.get("hubs") is not None
        if legacy_hubs:
            raw_positioning_hubs = watch.get("hubs")
        positioned_hubs = _dedupe(_codes(raw_positioning_hubs,
                                         "positioning_hubs" if not legacy_hubs else "hubs"))
        positioned_hubs = [hub for hub in positioned_hubs if hub != home[0]]
        hubs = [home[0]] + positioned_hubs
        outbound = _codes(watch.get("outbound_destination"), "outbound_destination")
        inbound = _codes(watch.get("inbound_origin"), "inbound_origin")
        if len(outbound) != 1 or len(inbound) != 1:
            raise ConfigError("hub_open_jaw_scout requires one outbound_destination and inbound_origin")
        window = _window(watch.get("depart_window"), "depart_window")
        trip_days = _trip_days(watch.get("trip_days"), "trip_days")
        if trip_days[0] != trip_days[1]:
            raise ConfigError("hub_open_jaw_scout requires one fixed trip_days value")
        step = int((watch.get("depart_window") or {}).get("step_days", 14) or 14)
        if step < 1:
            raise ConfigError("depart_window.step_days must be >= 1")
        positioning = watch.get("positioning") or {}
        buffer_minutes = int(positioning.get("min_buffer_minutes", 0) or 0)
        if buffer_minutes < 0:
            raise ConfigError("positioning.min_buffer_minutes must be >= 0")
        start = datetime.date.fromisoformat(window[0])
        end = datetime.date.fromisoformat(window[1])
        anchors = []
        cursor = start
        while cursor <= end:
            anchors.append(cursor)
            cursor += datetime.timedelta(days=step)
        required = len(anchors) * (1 + 4 * len(positioned_hubs))
        max_probes = int((watch.get("depart_window") or {}).get("max_probes", 60))
        if required > max_probes:
            raise ConfigError(
                "hub_open_jaw_scout needs {0} API calls ({1} date anchors; "
                "HKG direct=1 plus each positioned hub=4 per anchor), over "
                "max_probes={2}".format(required, len(anchors), max_probes))
        probes = []
        for anchor in anchors:
            depart = _date(anchor, "depart_window")
            back = _date(anchor + datetime.timedelta(days=trip_days[0]),
                         "depart_window+trip_days")
            journey = {"departure_date": depart, "return_date": back}
            direct_legs = [base.Leg(home, outbound, depart),
                           base.Leg(inbound, home, back)]
            probes.append(Probe(
                "direct:{0}".format(depart),
                "{0} direct open jaw {1}→{2}".format(home[0], depart, back),
                _query(direct_legs, options), _leg_label(direct_legs),
                {"group": "direct:{0}".format(depart), "role": "direct",
                 "hub": home[0], "journey_dates": journey, "ticket_count": 1}))
            for hub in positioned_hubs:
                group = "hub:{0}:{1}".format(hub, depart)
                legs_by_role = (
                    ("position_out", base.Leg(home, [hub], depart)),
                    ("main_out", base.Leg([hub], outbound, depart)),
                    ("main_in", base.Leg(inbound, [hub], back)),
                    ("position_back", base.Leg([hub], home, back)),
                )
                for role, leg in legs_by_role:
                    probes.append(Probe(
                        "{0}:{1}".format(group, role),
                        "{0} {1}: {2}".format(hub, role, _leg_label([leg])),
                        _query([leg], options), _leg_label([leg]),
                        {"group": group, "role": role, "hub": hub,
                         "journey_dates": journey, "ticket_count": 4,
                         "min_buffer_minutes": buffer_minutes}))
        notes.append(
            "Hub comparison: {0} direct plus {1}; positioned hubs use separate "
            "PNRs and require at least {2} minutes at the hub.".format(
                home[0], ", ".join(positioned_hubs) if positioned_hubs else "no optional hubs",
                buffer_minutes))
        return ProbeSet(watch_id, label, strategy, "hub", probes, notes,
                        route="{0} → {1} // {2} → {0}".format(
                            "/".join(hubs), outbound[0], inbound[0]))

    if strategy == "low_fare_discovery":
        forbidden = ("destination", "nearby_destinations", "legs", "via",
                     "exclude_via", "return", "return_window")
        present = [key for key in forbidden if watch.get(key) not in (None, [], "")]
        if present:
            raise ConfigError(
                "low_fare_discovery searches destination-free deals; remove: {0}".format(
                    ", ".join(present)))
        origins = _codes(watch.get("origin"), "origin") + \
            _codes(watch.get("nearby_origins"), "nearby_origins")
        origins = _dedupe(origins)
        if not origins:
            raise ConfigError("low_fare_discovery requires origin")
        discovery_type = _discovery_type(watch.get("discovery_type"))
        outbound_window = _discovery_window(watch.get("outbound_window"))
        trip_length = watch.get("trip_length")
        travel_duration = watch.get("travel_duration")
        if discovery_type == "oneway" and (trip_length is not None or
                                             travel_duration is not None):
            raise ConfigError(
                "oneway low_fare_discovery cannot use trip_length or travel_duration")
        if trip_length is not None and travel_duration is not None:
            raise ConfigError(
                "use either trip_length or travel_duration, not both")
        if trip_length is not None:
            trip_length = _trip_days(trip_length, "trip_length")
        if travel_duration is not None:
            travel_duration = int(travel_duration)
            if travel_duration not in (1, 2, 3):
                raise ConfigError(
                    "travel_duration must be 1 (week), 2 (weekend), or 3 (2 weeks)")
        max_deals = int(watch.get("max_deals", 20))
        if max_deals < 1 or max_deals > 50:
            raise ConfigError("max_deals must be between 1 and 50")
        discovery = {
            "origins": origins,
            "type": discovery_type,
            "outbound_window": outbound_window,
            "trip_length": trip_length,
            "travel_duration": travel_duration,
            "max_deals": max_deals,
            "home_country": _home_country(watch.get("home_country")),
        }
        discovery_options = dict(options)
        discovery_options["max_offers"] = max_deals
        query = _discovery_query(discovery_options, discovery)
        probe = Probe("discover", "{0} -> anywhere ({1})".format(
            "/".join(origins), discovery_type), query,
            "/".join(origins) + " -> ANYWHERE")
        notes.append(
            "Relative low-fare discovery: destinations are ranked by reported "
            "discount percentage, then price; no fixed price threshold is applied.")
        if outbound_window:
            notes.append("Departure window: {0}..{1}.".format(*outbound_window))
        return ProbeSet(watch_id, label, strategy, "min", [probe], notes,
                        route="{0} -> ANYWHERE".format("/".join(origins)),
                        sort_mode="relative_deal")

    if strategy == "focus_roundtrip":
        raise ConfigError(
            "focus_roundtrip is resolved from its scout snapshot by the runner; "
            "use fare_watch.py run or validate rather than expanding it directly")

    if strategy == "cheapest_dates":
        # One call: the backend itself reports the cheapest dates across the
        # whole window for the requested trip length.
        window = _window(watch.get("depart_window"), "depart_window")
        days = _trip_days(watch.get("trip_days"), "trip_days")
        origins = _codes(watch.get("origin"), "origin")
        destinations = _codes(watch.get("destination"), "destination")
        if len(origins) > 1 or len(destinations) > 1:
            raise ConfigError(
                "cheapest_dates takes exactly one origin and one destination "
                "(the cheapest-dates endpoint has no multi-airport form). Add "
                "one watch per origin, or use strategy flex_roundtrip.")
        # A nominal pair of legs keeps the neutral model uniform; the window
        # and duration are what the backend actually uses.
        legs = [base.Leg(origins, destinations, window[0]),
                base.Leg(destinations, origins, window[0])]
        query = _query(legs, options)
        query.depart_window = window
        query.duration_days = days
        probe = Probe(key="grid",
                      label="{0} -> {1}  {2}..{3}, {4}".format(
                          origins[0], destinations[0], window[0], window[1],
                          "{0}d".format(days[0]) if days[0] == days[1]
                          else "{0}-{1}d".format(*days)),
                      query=query,
                      leg_summary=_leg_label(legs))
        notes.append(
            "Cheapest-date scan over {0}..{1} for a {2} trip in a SINGLE API "
            "call.".format(window[0], window[1],
                           "{0}-day".format(days[0]) if days[0] == days[1]
                           else "{0}-{1} day".format(*days)))
        return ProbeSet(watch_id, label, strategy, "min", [probe], notes,
                        route="{0} -> {1} -> {0}".format(origins[0],
                                                         destinations[0]))

    if strategy == "flex_roundtrip":
        # Portable alternative to cheapest_dates: anchor departure dates across
        # the window and hold the trip length fixed. Costs one call per anchor
        # (times each trip length), but works on every backend and returns full
        # flight detail.
        spec = watch.get("depart_window") or {}
        window = _window(spec, "depart_window")
        days = _trip_days(watch.get("trip_days"), "trip_days")
        step = int(spec.get("step_days", 7) or 7)
        if step < 1:
            raise ConfigError("depart_window.step_days must be >= 1")
        max_probes = int(spec.get("max_probes", 12))

        origins = _codes(watch.get("origin"), "origin") + \
            _codes(watch.get("nearby_origins"), "nearby_origins")
        destinations = _codes(watch.get("destination"), "destination") + \
            _codes(watch.get("nearby_destinations"), "nearby_destinations")

        start = datetime.date.fromisoformat(window[0])
        end = datetime.date.fromisoformat(window[1])
        durations = sorted({days[0], days[1]})
        anchors = []
        cursor = start
        while cursor <= end:
            anchors.append(cursor)
            cursor += datetime.timedelta(days=step)

        planned = len(anchors) * len(durations)
        if planned > max_probes:
            raise ConfigError(
                "flex_roundtrip would need {0} API calls ({1} departure dates x "
                "{2} trip length(s)), over the limit of {3}. Raise "
                "depart_window.step_days, narrow the window, use a single "
                "trip_days value, or raise depart_window.max_probes "
                "deliberately.".format(planned, len(anchors), len(durations),
                                       max_probes))

        probes = []
        for anchor in anchors:
            for span in durations:
                back = anchor + datetime.timedelta(days=span)
                legs = [
                    base.Leg(_dedupe(origins), _dedupe(destinations),
                             _date(anchor, "depart_window")),
                    base.Leg(_dedupe(destinations), _dedupe(origins),
                             _date(back, "depart_window+trip_days")),
                ]
                probes.append(Probe(
                    key="{0}+{1}d".format(anchor.isoformat(), span),
                    label="depart {0}, back {1} ({2}d)".format(
                        anchor.isoformat(), back.isoformat(), span),
                    query=_query(legs, options),
                    leg_summary=_leg_label(legs)))
        if not probes:
            raise ConfigError("flex_roundtrip produced no probe; check the window")

        notes.append(
            "Flexible window: {0} departure date(s) every {1} day(s) across "
            "{2}..{3}, trip length {4} -> {5} API call(s). The cheapest "
            "combination is reported; dates between anchors are not "
            "priced.".format(len(anchors), step, window[0], window[1],
                             "{0}d".format(days[0]) if days[0] == days[1]
                             else "{0}d or {1}d".format(*days), len(probes)))
        return ProbeSet(watch_id, label, strategy, "min", probes, notes,
                        route=_leg_label(probes[0].query.legs))

    if strategy == "open_return":
        window = watch.get("return_window") or {}
        if not window:
            raise ConfigError(
                "open_return requires 'return_window' {from, to, step_days}")
        origins = _codes(watch.get("origin"), "origin") + \
            _codes(watch.get("nearby_origins"), "nearby_origins")
        destinations = _codes(watch.get("destination"), "destination") + \
            _codes(watch.get("nearby_destinations"), "nearby_destinations")
        depart = _date(watch.get("depart"), "depart")

        probes = []
        for return_date in _sweep_dates(window, "return_window"):
            if return_date < depart:
                continue
            legs = [
                base.Leg(origins, destinations, depart,
                         via=_codes(watch.get("via"), "via"),
                         exclude_via=_codes(watch.get("exclude_via"),
                                            "exclude_via")),
                base.Leg(destinations, origins, return_date,
                         via=_codes(watch.get("return_via"), "return_via"),
                         exclude_via=_codes(watch.get("exclude_via"),
                                            "exclude_via")),
            ]
            probes.append(Probe(
                key="return={0}".format(return_date),
                label="{0} return {1}".format(_leg_label(legs), return_date),
                query=_query(legs, options),
                leg_summary=_leg_label(legs)))
        if not probes:
            raise ConfigError("open_return produced no probe: every return date "
                              "falls before the departure date")
        notes.append(
            "OPEN proxy: the return leg is priced across {0} candidate dates; the "
            "cheapest is reported. A true open-dated ticket must be booked with "
            "an agent.".format(len(probes)))
        return ProbeSet(watch_id, label, strategy, "min", probes, notes,
                        route=_leg_label(probes[0].query.legs))

    if strategy == "split_ticket":
        specs = watch.get("legs") or []
        if len(specs) < 2:
            raise ConfigError(
                "split_ticket requires at least 2 legs (one per ticket)")
        probes = []
        all_legs = []
        for index, spec in enumerate(specs):
            leg = _leg(dict(spec), index + 1, options)
            all_legs.append(leg)
            probes.append(Probe(
                key="ticket{0}".format(index + 1),
                label="ticket {0}: {1}".format(index + 1, _leg_label([leg])),
                query=_query([leg], options),
                leg_summary=_leg_label([leg])))
        notes.append(
            "SPLIT TICKET: {0} separate tickets, separate PNRs. No protection on "
            "a missed connection, baggage is not through-checked, and you must "
            "clear immigration/customs and re-check bags at each hand-off. Verify "
            "transit visa rules and leave a wide buffer.".format(len(probes)))
        return ProbeSet(watch_id, label, strategy, "sum", probes, notes,
                        route=_leg_label(all_legs))

    specs = _leg_specs(watch)
    legs = [_leg(spec, index + 1, options) for index, spec in enumerate(specs)]
    query = _query(legs, options)
    probe = Probe(key="main", label=_leg_label(legs), query=query,
                  leg_summary=_leg_label(legs))
    if strategy == "open_jaw":
        notes.append("Open-jaw itinerary: outbound and inbound use different "
                     "endpoints; ground transport between them is on you.")
    if len(legs) > 2:
        notes.append("Multi-city itinerary priced as one ticket across "
                     "{0} legs.".format(len(legs)))
    return ProbeSet(watch_id, label, strategy, "min", [probe], notes)
