"""Amadeus Self-Service API provider (GDS data).

Uses the POST form of Flight Offers Search, which is the only form that accepts
`originDestinations` and can therefore express multi-city, open-jaw and forced
connection points.

Strengths over the Google Flights path: forced/banned connection points, a
server-side +/-3 day window, fare-condition filters (refundable / no penalty),
and a bookable-seat count in the response.
Weaknesses: at most two alternative airports per leg, no alliance filter, and
test-environment data is a reduced cached subset.
"""

import datetime
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from . import base

HOSTS = {
    "test": "https://test.api.amadeus.com",
    "production": "https://api.amadeus.com",
}
RETRYABLE_STATUS = (429, 500, 502, 503, 504)

# Amadeus takes the first airport plus at most two alternatives.
MAX_ALTERNATIVE_AIRPORTS = 2
MAX_ORIGIN_DESTINATIONS = 6
MAX_OFFERS = 250


class AmadeusProvider(base.Provider):
    name = "amadeus"
    CAPABILITIES = frozenset({
        base.CAP_MULTI_CITY,
        base.CAP_OPEN_JAW,
        base.CAP_VIA,
        base.CAP_EXCLUDE_VIA,
        base.CAP_DATE_WINDOW,
        base.CAP_MULTI_AIRPORT,
        base.CAP_AIRLINE_FILTER,
        base.CAP_FARE_FLEX,
        base.CAP_BAGS_FILTER,
        base.CAP_MAX_PRICE,
        base.CAP_TRAVEL_CLASS,
        base.CAP_MAX_STOPS,
        base.CAP_MAX_FLIGHT_TIME,
        base.CAP_SEAT_COUNT,
        base.CAP_DATE_GRID,
    })

    def __init__(self, client_id=None, client_secret=None, environment="test",
                 min_interval_ms=300, max_retries=4, token_cache=None,
                 timeout=40, logger=None):
        super(AmadeusProvider, self).__init__(logger=logger)
        self.client_id = client_id or os.environ.get("AMADEUS_CLIENT_ID")
        self.client_secret = client_secret or os.environ.get("AMADEUS_CLIENT_SECRET")
        if not self.client_id or not self.client_secret:
            raise base.ProviderError(
                "Missing Amadeus credentials. Set AMADEUS_CLIENT_ID / "
                "AMADEUS_CLIENT_SECRET, or switch provider in the config.")
        if environment not in HOSTS:
            raise base.ProviderError("environment must be 'test' or 'production'")
        self.host = HOSTS[environment]
        self.environment = environment
        self.min_interval = max(min_interval_ms, 100) / 1000.0
        self.max_retries = max_retries
        self.timeout = timeout
        self.token_cache = token_cache
        self._token = None
        self._token_expires_at = 0.0
        self._last_call_at = 0.0

    # ---------------------------------------------------------------- auth

    def _load_cached_token(self):
        if not self.token_cache or not os.path.exists(self.token_cache):
            return
        try:
            with open(self.token_cache, "r") as handle:
                cached = json.load(handle)
        except (ValueError, IOError):
            return
        # A cache written for another key/env must not be reused.
        if cached.get("client_id") != self.client_id:
            return
        if cached.get("environment") != self.environment:
            return
        if cached.get("expires_at", 0) - 60 > time.time():
            self._token = cached.get("access_token")
            self._token_expires_at = cached["expires_at"]

    def _store_cached_token(self):
        if not self.token_cache:
            return
        directory = os.path.dirname(self.token_cache)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(self.token_cache, "w") as handle:
            json.dump({"client_id": self.client_id,
                       "environment": self.environment,
                       "access_token": self._token,
                       "expires_at": self._token_expires_at}, handle)
        os.chmod(self.token_cache, 0o600)

    def _access_token(self):
        if self._token and self._token_expires_at - 60 > time.time():
            return self._token
        self._load_cached_token()
        if self._token and self._token_expires_at - 60 > time.time():
            return self._token

        body = urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }).encode("utf-8")
        request = urllib.request.Request(
            self.host + "/v1/security/oauth2/token", data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise base.ProviderError(
                "Amadeus auth failed (HTTP {0}). Verify the key/secret and that "
                "it belongs to the '{1}' environment. {2}".format(
                    exc.code, self.environment,
                    exc.read().decode("utf-8", "replace")[:300]),
                status=exc.code)
        self._token = payload["access_token"]
        self._token_expires_at = time.time() + float(payload.get("expires_in", 1799))
        self._store_cached_token()
        return self._token

    # ------------------------------------------------------------- request

    def _pace(self):
        elapsed = time.time() - self._last_call_at
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_call_at = time.time()

    def _call(self, method, path, query=None, body=None):
        url = self.host + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = json.dumps(body).encode("utf-8") if body is not None else None

        attempt = 0
        while True:
            attempt += 1
            self._pace()
            request = urllib.request.Request(
                url, data=data,
                headers={"Authorization": "Bearer " + self._access_token(),
                         "Content-Type": "application/vnd.amadeus+json",
                         "Accept": "application/vnd.amadeus+json"},
                method=method)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    self.call_count += 1
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode("utf-8", "replace")
                try:
                    payload = json.loads(raw)
                except ValueError:
                    payload = {"raw": raw}

                if exc.code == 401 and attempt <= 2:
                    self._token = None
                    self._token_expires_at = 0
                    continue
                if exc.code in RETRYABLE_STATUS and attempt <= self.max_retries:
                    delay = self._retry_delay(exc, attempt)
                    self.log("HTTP {0}, retry {1}/{2} in {3:.1f}s".format(
                        exc.code, attempt, self.max_retries, delay))
                    time.sleep(delay)
                    continue
                raise base.ProviderError(
                    "Amadeus {0} {1} failed (HTTP {2}): {3}".format(
                        method, path, exc.code, _describe_issues(payload)),
                    status=exc.code)
            except urllib.error.URLError as exc:
                if attempt <= self.max_retries:
                    time.sleep(min(2 ** attempt, 30))
                    continue
                raise base.ProviderError("Network error: {0}".format(exc.reason))

    @staticmethod
    def _retry_delay(exc, attempt):
        retry_after = exc.headers.get("Retry-After") if exc.headers else None
        if retry_after:
            try:
                return min(float(retry_after), 60.0)
            except ValueError:
                pass
        return min(2 ** attempt, 30)

    # ---------------------------------------------------------- body build

    def _body(self, query):
        legs = query.legs
        if len(legs) > MAX_ORIGIN_DESTINATIONS:
            raise base.CapabilityError(
                "Amadeus prices at most {0} legs on one ticket, got {1}; use "
                "strategy split_ticket".format(MAX_ORIGIN_DESTINATIONS, len(legs)))

        origin_destinations = []
        for index, leg in enumerate(legs, start=1):
            if len(leg.origins) - 1 > MAX_ALTERNATIVE_AIRPORTS:
                raise base.CapabilityError(
                    "Amadeus allows one origin plus {0} alternatives per leg; leg "
                    "{1} lists {2}. Trim the list, or use the google_flights "
                    "provider which accepts any number.".format(
                        MAX_ALTERNATIVE_AIRPORTS, index, len(leg.origins)))
            if len(leg.destinations) - 1 > MAX_ALTERNATIVE_AIRPORTS:
                raise base.CapabilityError(
                    "Amadeus allows one destination plus {0} alternatives per leg; "
                    "leg {1} lists {2}.".format(
                        MAX_ALTERNATIVE_AIRPORTS, index, len(leg.destinations)))

            entry = {
                "id": str(index),
                "originLocationCode": leg.origin,
                "destinationLocationCode": leg.destination,
            }
            date_time = {"date": leg.date}
            if leg.date_window:
                date_time["dateWindow"] = leg.date_window
            if leg.time_window:
                date_time["timeWindow"] = leg.time_window
            entry["departureDateTimeRange"] = date_time

            if leg.via:
                entry["includedConnectionPoints"] = leg.via[:2]
            if leg.exclude_via:
                entry["excludedConnectionPoints"] = leg.exclude_via[:3]
            if len(leg.origins) > 1:
                entry["alternativeOriginsCodes"] = leg.origins[1:]
            if len(leg.destinations) > 1:
                entry["alternativeDestinationsCodes"] = leg.destinations[1:]
            origin_destinations.append(entry)

        ids = [entry["id"] for entry in origin_destinations]
        return {
            "currencyCode": query.currency,
            "originDestinations": origin_destinations,
            "travelers": _travelers(query),
            "sources": ["GDS"],
            "searchCriteria": _search_criteria(query, ids),
        }

    def describe_request(self, query):
        if query.depart_window:
            return json.dumps(self._date_grid_params(query), indent=2,
                              ensure_ascii=False)
        return json.dumps(self._body(query), indent=2, ensure_ascii=False)

    # ------------------------------------------------------------ date grid

    def _date_grid_params(self, query):
        """Params for Flight Cheapest Date Search.

        One call covers a whole departure window crossed with a trip-length
        range, which is what makes it the cheap way to answer "when is this
        route cheapest?".
        """
        leg = query.legs[0]
        params = {
            "origin": leg.origin,
            "destination": leg.destination,
            "departureDate": "{0},{1}".format(*query.depart_window),
            "oneWay": "false",
            "nonStop": "true" if query.max_stops == 0 else "false",
            # DURATION gives one entry per (departure date, trip length) pair.
            "viewBy": "DURATION",
        }
        if query.duration_days:
            low, high = query.duration_days
            params["duration"] = (str(low) if low == high
                                  else "{0},{1}".format(low, high))
        if query.max_price is not None:
            params["maxPrice"] = int(query.max_price)
        return params

    def _search_date_grid(self, query):
        params = self._date_grid_params(query)
        payload = self._call("GET", "/v1/shopping/flight-dates", query=params)
        return parse_date_grid(payload, query, environment=self.environment,
                               provider_name=self.name)

    # -------------------------------------------------------------- search

    def search(self, query):
        if query.depart_window:
            return self._search_date_grid(query)
        payload = self._call("POST", "/v2/shopping/flight-offers",
                             body=self._body(query))
        offers = [_parse_offer(offer, query.currency)
                  for offer in payload.get("data", [])]
        offers = [offer for offer in offers if offer]
        offers.sort(key=lambda item: item["price"])
        meta = {"provider": self.name, "environment": self.environment}
        if self.environment == "test":
            meta["note"] = ("Test environment returns a reduced cached subset; "
                            "prices are indicative, not bookable.")
        return offers[:query.max_offers], meta

    def search_locations(self, keyword, sub_types="AIRPORT,CITY", limit=10):
        return self._call("GET", "/v1/reference-data/locations", query={
            "subType": sub_types, "keyword": keyword, "page[limit]": limit})


# ------------------------------------------------------------------ helpers

def parse_date_grid(payload, query, environment=None, provider_name="amadeus"):
    """Parse a Flight Cheapest Date Search response into neutral offers.

    Standalone so the mock provider can replay a fixture through the very same
    parser instead of carrying a second copy of the logic.
    """
    currency = (payload.get("meta") or {}).get("currency") or query.currency

    offers = []
    for row in payload.get("data", []):
        price = (row.get("price") or {}).get("total")
        if price is None:
            continue
        depart = row.get("departureDate")
        back = row.get("returnDate")
        # Date-level result: there is no itinerary detail, so the placeholder
        # segment is labelled rather than faked as a nonstop flight, and the
        # stop count is left unknown instead of asserting zero.
        itineraries = [base.make_itinerary([base.make_segment(
            flight="(dates only)", origin=row.get("origin"),
            destination=row.get("destination"), depart_at=depart,
            arrive_at=depart)])]
        if back:
            itineraries.append(base.make_itinerary([base.make_segment(
                flight="(dates only)", origin=row.get("destination"),
                destination=row.get("origin"), depart_at=back,
                arrive_at=back)]))
        for itinerary in itineraries:
            itinerary["stops"] = None
        offers.append(base.make_offer(
            price=price, currency=currency, itineraries=itineraries,
            extras={"departure_date": depart, "return_date": back,
                    "trip_days": _day_gap(depart, back),
                    "offers_link": (row.get("links") or {}).get("flightOffers")}))

    offers.sort(key=lambda item: item["price"])
    notes = [
        "Cheapest-date scan: one API call returned {0} date combination(s). "
        "Prices are date-level only - no flight numbers or routing. Run a normal "
        "roundtrip watch on the winning dates for flight detail.".format(
            len(offers))]
    warnings = payload.get("warnings") or []
    if any("Maximum response size" in (w.get("title") or "") for w in warnings):
        notes.append(
            "The API truncated the result (maximum response size). Narrow the "
            "window or the duration range to see the full grid.")
    if environment == "test":
        notes.append(
            "Cheapest Date Search has limited route coverage, especially in the "
            "test environment; an empty result usually means the route is not "
            "covered rather than that no fares exist.")
    meta = {"provider": provider_name, "environment": environment,
            "date_grid": True, "notes": notes}
    return offers[:query.max_offers], meta


def _day_gap(start, end):
    """Whole days between two ISO dates, or None when either is missing."""
    if not start or not end:
        return None
    try:
        first = datetime.date.fromisoformat(start)
        second = datetime.date.fromisoformat(end)
    except ValueError:
        return None
    return (second - first).days


def _describe_issues(payload):
    issues = payload.get("errors") or []
    parts = []
    for issue in issues:
        source = issue.get("source") or {}
        where = source.get("parameter") or source.get("pointer") or ""
        parts.append("[{0}] {1}: {2}{3}".format(
            issue.get("code", "?"), issue.get("title", ""),
            issue.get("detail", ""),
            " (at {0})".format(where) if where else ""))
    return " | ".join(parts) if parts else json.dumps(payload)[:400]


def _travelers(query):
    travelers = []
    next_id = 1
    adult_ids = []
    for _ in range(query.adults):
        travelers.append({"id": str(next_id), "travelerType": "ADULT"})
        adult_ids.append(str(next_id))
        next_id += 1
    for _ in range(query.children):
        travelers.append({"id": str(next_id), "travelerType": "CHILD"})
        next_id += 1
    for _ in range(query.infants):
        if not adult_ids:
            raise base.CapabilityError("infants require at least one adult")
        travelers.append({"id": str(next_id), "travelerType": "HELD_INFANT",
                          "associatedAdultId": adult_ids[(next_id - 1) % len(adult_ids)]})
        next_id += 1
    return travelers


def _search_criteria(query, origin_destination_ids):
    max_offers = min(max(query.max_offers, 1), MAX_OFFERS)
    flight_filters = {}

    if query.max_stops is not None:
        flight_filters["connectionRestriction"] = {
            "maxNumberOfConnections": int(query.max_stops),
            "airportChangeAllowed": False,
            "technicalStopsAllowed": True,
        }
    if query.travel_class:
        flight_filters["cabinRestrictions"] = [{
            "cabin": query.travel_class.upper(),
            "coverage": "MOST_SEGMENTS",
            "originDestinationIds": origin_destination_ids,
        }]
    # Carrier codes nest under carrierRestrictions, not flightFilters directly.
    carrier_restrictions = {}
    if query.airlines:
        carrier_restrictions["includedCarrierCodes"] = query.airlines
    if query.exclude_airlines:
        carrier_restrictions["excludedCarrierCodes"] = query.exclude_airlines
    if carrier_restrictions:
        flight_filters["carrierRestrictions"] = carrier_restrictions
    if query.max_flight_time_pct:
        flight_filters["maxFlightTime"] = int(query.max_flight_time_pct)

    criteria = {"maxFlightOffers": max_offers, "addOneWayOffers": True}
    if flight_filters:
        criteria["flightFilters"] = flight_filters
    if query.max_price is not None:
        criteria["maxPrice"] = int(query.max_price)

    # Fare flexibility lives under pricingOptions.
    pricing_options = {}
    if query.checked_bags_only:
        pricing_options["includedCheckedBagsOnly"] = True
    if query.refundable_only:
        pricing_options["refundableFare"] = True
    if query.no_penalty_only:
        pricing_options["noPenaltyFare"] = True
    if pricing_options:
        criteria["pricingOptions"] = pricing_options
    return criteria


def _iso_duration(iso):
    """PT12H35M -> 12h35m"""
    if not iso:
        return None
    match = re.match(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?", iso)
    if not match:
        return iso
    days, hours, minutes = (int(value or 0) for value in match.groups())
    return base.human_duration_minutes(days * 24 * 60 + hours * 60 + minutes)


def _parse_offer(offer, currency):
    cabin_by_segment = {}
    bags_by_segment = {}
    for traveler in offer.get("travelerPricings", [])[:1]:
        for fare in traveler.get("fareDetailsBySegment", []):
            cabin_by_segment[fare.get("segmentId")] = fare.get("cabin")
            bags = fare.get("includedCheckedBags") or {}
            quantity = bags.get("quantity")
            weight = bags.get("weight")
            unit = bags.get("weightUnit") or "KG"
            if quantity is not None:
                bags_by_segment[fare.get("segmentId")] = {
                    "status": "explicit", "pieces": quantity,
                    "text": "含 {0} 件托运行李".format(quantity)}
            elif weight is not None:
                bags_by_segment[fare.get("segmentId")] = {
                    "status": "explicit", "weight": weight, "weight_unit": unit,
                    "text": "含托运行李 {0}{1}".format(weight, unit)}
            else:
                bags_by_segment[fare.get("segmentId")] = None

    itineraries = []
    for itinerary in offer.get("itineraries", []):
        segments = []
        for segment in itinerary.get("segments", []):
            departure = segment.get("departure", {})
            arrival = segment.get("arrival", {})
            segment_id = segment.get("id")
            segments.append(base.make_segment(
                flight="{0}{1}".format(segment.get("carrierCode", ""),
                                       segment.get("number", "")),
                origin=departure.get("iataCode"),
                destination=arrival.get("iataCode"),
                depart_at=departure.get("at"),
                arrive_at=arrival.get("at"),
                duration=_iso_duration(segment.get("duration")),
                cabin=cabin_by_segment.get(segment_id),
                checked_bags=bags_by_segment.get(segment_id),
                operating=(segment.get("operating") or {}).get("carrierCode"),
                technical_stops=segment.get("numberOfStops", 0),
                aircraft=(segment.get("aircraft") or {}).get("code"),
            ))
        if segments:
            itineraries.append(base.make_itinerary(
                segments, duration=_iso_duration(itinerary.get("duration"))))

    if not itineraries:
        return None
    price = offer.get("price", {})
    bag_values = [segment.get("checked_bags") for itinerary in itineraries
                  for segment in itinerary.get("segments") if segment.get("checked_bags")]
    baggage_summary = bag_values[0] if bag_values else {
        "status": "unknown", "text": "未返回托运行李信息"}
    return base.make_offer(
        price=price.get("grandTotal", price.get("total", 0)),
        currency=price.get("currency") or currency,
        itineraries=itineraries,
        validating_airlines=offer.get("validatingAirlineCodes") or [],
        seats=offer.get("numberOfBookableSeats"),
        last_ticketing_date=offer.get("lastTicketingDate"),
        extras={"baggage_summary": baggage_summary},
    )
