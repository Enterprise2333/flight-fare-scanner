"""Provider-neutral query/offer model and the capability contract.

The point of this module is that a strategy describes *what* to search for
without knowing which backend will answer. Backends differ in real ways -
Amadeus can force a connection point, Google Flights cannot; Google Flights can
fan out over unlimited origin airports, Amadeus allows two alternatives.

Every optional search feature is therefore tagged with a capability token. A
provider declares the tokens it supports, and any request using a token the
provider lacks is REJECTED rather than silently downgraded. Silently dropping a
filter is the dangerous failure mode: the caller would trust a result set that
was never actually filtered.
"""

import hashlib
import json

# ------------------------------------------------------------- capabilities

CAP_MULTI_CITY = "multi_city"          # 3+ legs on one ticket
CAP_OPEN_JAW = "open_jaw"              # legs whose endpoints do not join up
CAP_VIA = "via"                        # force an itinerary through a point
CAP_EXCLUDE_VIA = "exclude_via"        # ban a connection point
CAP_DATE_WINDOW = "date_window"        # +/- N days around a date, server-side
CAP_MULTI_AIRPORT = "multi_airport"    # several origin/destination airports
CAP_AIRLINE_FILTER = "airline_filter"  # include/exclude carrier codes
CAP_ALLIANCE_FILTER = "alliance_filter"  # filter by Star/SkyTeam/Oneworld
CAP_FARE_FLEX = "fare_flex"            # refundable / no-penalty fare filters
CAP_BAGS_FILTER = "bags_filter"        # checked-baggage-included filter
CAP_MAX_PRICE = "max_price"
CAP_TRAVEL_CLASS = "travel_class"
CAP_MAX_STOPS = "max_stops"
CAP_LAYOVER_FILTER = "layover_filter"  # min/max layover minutes
CAP_MAX_FLIGHT_TIME = "max_flight_time"  # cap detour vs shortest itinerary
CAP_SEAT_COUNT = "seat_count"          # response exposes bookable seat count
CAP_PRICE_INSIGHTS = "price_insights"  # response exposes low/typical/high level
CAP_BOOKING_LINK = "booking_link"      # response exposes a booking handoff
CAP_DEAL_DISCOVERY = "deal_discovery"  # origin-to-anywhere relative low-fare search
CAP_DATE_GRID = "date_grid"            # ONE call answers "cheapest dates in a
                                       # window for a given trip length"

# Human-readable remedy shown when a provider lacks a capability.
CAPABILITY_HINTS = {
    CAP_VIA: "drop `via`, or name the stopover explicitly as its own leg with "
             "strategy multi_city",
    CAP_EXCLUDE_VIA: "drop `exclude_via`; filter the printed results instead",
    CAP_DATE_WINDOW: "drop `date_window` and sweep dates with separate watches, "
                     "or use strategy open_return for the return leg",
    CAP_MULTI_AIRPORT: "keep a single airport per leg and add one watch per "
                       "origin",
    CAP_ALLIANCE_FILTER: "list the individual carrier codes in `airlines` "
                         "instead of an alliance name",
    CAP_FARE_FLEX: "drop `refundable_only`/`no_penalty_only`; this backend "
                   "cannot filter by fare conditions",
    CAP_BAGS_FILTER: "drop `checked_bags_only`",
    CAP_LAYOVER_FILTER: "drop `layover_minutes`",
    CAP_MAX_FLIGHT_TIME: "drop `max_flight_time_pct`",
    CAP_MULTI_CITY: "this backend cannot price 3+ legs as one ticket; use "
                    "strategy split_ticket",
    CAP_OPEN_JAW: "this backend cannot price an open jaw; book two one-ways "
                  "with strategy split_ticket",
    CAP_DATE_GRID: "this backend has no cheapest-dates endpoint that pins a "
                   "destination; use strategy flex_roundtrip, which sweeps "
                   "anchor dates instead (a few calls rather than one)",
    CAP_DEAL_DISCOVERY: "this backend cannot discover relative low fares from "
                        "one origin to arbitrary destinations",
}

ALLIANCES = ("STAR_ALLIANCE", "SKYTEAM", "ONEWORLD")
CABINS = ("ECONOMY", "PREMIUM_ECONOMY", "BUSINESS", "FIRST")


class ProviderError(Exception):
    """Transport/API failure from a backend."""

    def __init__(self, message, status=None, retryable=False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class CapabilityError(Exception):
    """The query asks for something this provider cannot express."""


# ---------------------------------------------------------------- the query

class Leg(object):
    """One directional hop of an itinerary, provider-neutral."""

    def __init__(self, origins, destinations, date, date_window=None,
                 time_window=None, times=None, via=None, exclude_via=None):
        self.origins = list(origins)
        self.destinations = list(destinations)
        self.date = date
        self.date_window = date_window
        self.time_window = time_window
        self.times = times
        self.via = list(via or [])
        self.exclude_via = list(exclude_via or [])

    @property
    def origin(self):
        return self.origins[0]

    @property
    def destination(self):
        return self.destinations[0]

    def to_dict(self):
        return {
            "origins": self.origins, "destinations": self.destinations,
            "date": self.date, "date_window": self.date_window,
            "time_window": self.time_window, "times": self.times,
            "via": self.via, "exclude_via": self.exclude_via,
        }


class Query(object):
    """A provider-neutral flight search."""

    def __init__(self, legs, adults=1, children=0, infants=0, currency="CNY",
                 travel_class=None, max_stops=None, airlines=None,
                 exclude_airlines=None, alliances=None, max_price=None,
                 max_offers=40, refundable_only=False, no_penalty_only=False,
                 checked_bags_only=False, layover_minutes=None,
                 max_flight_time_pct=None, market=None, language=None,
                 depart_window=None, duration_days=None, discovery=None):
        self.legs = legs
        self.adults = int(adults)
        self.children = int(children or 0)
        self.infants = int(infants or 0)
        self.currency = currency
        self.travel_class = travel_class
        self.max_stops = max_stops
        self.airlines = list(airlines or [])
        self.exclude_airlines = list(exclude_airlines or [])
        self.alliances = list(alliances or [])
        self.max_price = max_price
        self.max_offers = int(max_offers)
        self.refundable_only = bool(refundable_only)
        self.no_penalty_only = bool(no_penalty_only)
        self.checked_bags_only = bool(checked_bags_only)
        self.layover_minutes = layover_minutes
        self.max_flight_time_pct = max_flight_time_pct
        self.market = market
        self.language = language
        # Date-grid mode: ask the backend itself which dates are cheapest,
        # instead of the caller sweeping candidate dates.
        self.depart_window = depart_window      # (from_iso, to_iso)
        self.duration_days = duration_days      # (min_days, max_days)
        # Origin-to-anywhere relative deal discovery. This intentionally has no
        # destination Leg because the backend chooses destinations.
        self.discovery = dict(discovery or {})

    # -------------------------------------------------------- introspection

    @property
    def is_open_jaw(self):
        """True when consecutive legs do not join up (a surface gap)."""
        for previous, current in zip(self.legs, self.legs[1:]):
            if current.origin != previous.destination:
                return True
        return False

    def required_capabilities(self):
        """Capability tokens this particular query actually depends on."""
        needed = set()
        if self.depart_window:
            needed.add(CAP_DATE_GRID)
        if self.discovery:
            needed.add(CAP_DEAL_DISCOVERY)
        if len(self.legs) > 2:
            needed.add(CAP_MULTI_CITY)
        if self.is_open_jaw:
            needed.add(CAP_OPEN_JAW)
        for leg in self.legs:
            if leg.via:
                needed.add(CAP_VIA)
            if leg.exclude_via:
                needed.add(CAP_EXCLUDE_VIA)
            if leg.date_window:
                needed.add(CAP_DATE_WINDOW)
            if len(leg.origins) > 1 or len(leg.destinations) > 1:
                needed.add(CAP_MULTI_AIRPORT)
        if self.airlines or self.exclude_airlines:
            needed.add(CAP_AIRLINE_FILTER)
        if self.alliances:
            needed.add(CAP_ALLIANCE_FILTER)
        if self.refundable_only or self.no_penalty_only:
            needed.add(CAP_FARE_FLEX)
        if self.checked_bags_only:
            needed.add(CAP_BAGS_FILTER)
        if self.max_price is not None:
            needed.add(CAP_MAX_PRICE)
        if self.travel_class:
            needed.add(CAP_TRAVEL_CLASS)
        if self.max_stops is not None:
            needed.add(CAP_MAX_STOPS)
        if self.layover_minutes:
            needed.add(CAP_LAYOVER_FILTER)
        if self.max_flight_time_pct:
            needed.add(CAP_MAX_FLIGHT_TIME)
        return needed

    def to_dict(self):
        return {
            "legs": [leg.to_dict() for leg in self.legs],
            "adults": self.adults, "children": self.children,
            "infants": self.infants, "currency": self.currency,
            "travel_class": self.travel_class, "max_stops": self.max_stops,
            "airlines": self.airlines, "exclude_airlines": self.exclude_airlines,
            "alliances": self.alliances, "max_price": self.max_price,
            "max_offers": self.max_offers,
            "refundable_only": self.refundable_only,
            "no_penalty_only": self.no_penalty_only,
            "checked_bags_only": self.checked_bags_only,
            "layover_minutes": self.layover_minutes,
            "max_flight_time_pct": self.max_flight_time_pct,
            "depart_window": self.depart_window,
            "duration_days": self.duration_days,
            "discovery": self.discovery,
        }


# --------------------------------------------------------------- the offer

def make_segment(flight, origin, destination, depart_at, arrive_at,
                 duration=None, cabin=None, checked_bags=None,
                 operating=None, technical_stops=0, aircraft=None):
    return {
        "flight": flight, "operating": operating,
        "from": origin, "to": destination,
        "depart_at": depart_at, "arrive_at": arrive_at,
        "duration": duration, "cabin": cabin, "checked_bags": checked_bags,
        "technical_stops": technical_stops, "aircraft": aircraft,
    }


def make_itinerary(segments, duration=None):
    codes = []
    for index, segment in enumerate(segments):
        if index == 0:
            codes.append(segment["from"])
        codes.append(segment["to"])
    return {
        "path": "→".join(code for code in codes if code),
        "duration": duration,
        "stops": max(len(segments) - 1, 0),
        "depart_at": segments[0]["depart_at"] if segments else None,
        "arrive_at": segments[-1]["arrive_at"] if segments else None,
        "segments": segments,
    }


def fingerprint(itineraries):
    """Stable id for a routing, so an unchanged price on a different routing
    is still distinguishable in the history."""
    parts = []
    for itinerary in itineraries:
        for segment in itinerary["segments"]:
            parts.append("{0}@{1}".format(segment["flight"], segment["depart_at"]))
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:12]


def make_offer(price, currency, itineraries, validating_airlines=None,
               seats=None, last_ticketing_date=None, booking_link=None,
               extras=None):
    return {
        "price": float(price),
        "currency": currency,
        "validating_airlines": list(validating_airlines or []),
        "seats": seats,
        "last_ticketing_date": last_ticketing_date,
        "itineraries": itineraries,
        "fingerprint": fingerprint(itineraries),
        "booking_link": booking_link,
        "extras": extras or {},
    }


def human_duration_minutes(minutes):
    if minutes is None:
        return "-"
    minutes = int(minutes)
    hours, rest = divmod(minutes, 60)
    if hours and rest:
        return "{0}h{1:02d}m".format(hours, rest)
    if hours:
        return "{0}h".format(hours)
    return "{0}m".format(rest)


# ------------------------------------------------------------ base provider

class Provider(object):
    name = "base"
    CAPABILITIES = frozenset()
    # Some backends need one API call per leg group; used for quota estimates.
    calls_per_query = 1

    def __init__(self, logger=None):
        self.log = logger or (lambda msg: None)
        self.call_count = 0

    def check(self, query):
        """Raise CapabilityError if the query needs something unsupported."""
        missing = sorted(query.required_capabilities() - set(self.CAPABILITIES))
        if not missing:
            return
        details = []
        for token in missing:
            hint = CAPABILITY_HINTS.get(token)
            details.append("`{0}`{1}".format(
                token, " - " + hint if hint else ""))
        raise CapabilityError(
            "provider '{0}' does not support: {1}".format(
                self.name, "; ".join(details)))

    def search(self, query):
        """Return (offers, meta). Offers must be make_offer() dicts."""
        raise NotImplementedError

    def describe_request(self, query):
        """What would be sent, for `validate --show-body`."""
        return json.dumps(query.to_dict(), indent=2, ensure_ascii=False)
