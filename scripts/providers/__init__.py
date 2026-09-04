"""Provider registry.

Adding a backend means implementing `base.Provider` and registering it here.
Nothing else in the skill needs to change: strategies emit a neutral
`base.Query`, and every provider translates that into its own request and its
response back into neutral offers.
"""

import json
import os
import random

import city_airports
from . import base
from .amadeus import AmadeusProvider
from .google_flights import GoogleFlightsProvider

PROVIDERS = {
    GoogleFlightsProvider.name: GoogleFlightsProvider,
    AmadeusProvider.name: AmadeusProvider,
}

DEFAULT_PROVIDER = GoogleFlightsProvider.name


class MockProvider(base.Provider):
    """Replays a saved fixture so the whole pipeline runs with no key or quota.

    Accepts either provider's native response shape and reuses that provider's
    real parser, so the fixtures double as parser regression tests.
    """

    name = "mock"
    # Never block a test run on capabilities.
    CAPABILITIES = frozenset({
        value for key, value in vars(base).items() if key.startswith("CAP_")
    })

    def __init__(self, fixture_path, jitter_pct=0.0, logger=None):
        super(MockProvider, self).__init__(logger=logger)
        with open(os.path.expanduser(fixture_path), "r") as handle:
            self.fixture = json.load(handle)
        self.jitter_pct = jitter_pct
        rows = self.fixture.get("data") or []
        if "hub_responses" in self.fixture:
            self.shape = "hub_google"
        elif "deals" in self.fixture:
            self.shape = "google_flights_deals"
        elif "best_flights" in self.fixture or "other_flights" in self.fixture:
            self.shape = "google_flights"
        elif rows and rows[0].get("type") == "flight-date":
            self.shape = "amadeus_date_grid"
        elif "data" in self.fixture:
            self.shape = "amadeus"
        else:
            raise base.ProviderError(
                "Unrecognised fixture shape in {0}: expected Amadeus "
                "'data' (flight-offer or flight-date), Google Flights "
                "'best_flights', or Google Flights Deals 'deals'.".format(
                    fixture_path))

    def search(self, query):
        self.call_count += 1
        payload = json.loads(json.dumps(self.fixture))
        if self.shape == "hub_google":
            route_key = "|".join("{0}>{1}".format(
                ",".join(leg.origins), ",".join(leg.destinations))
                for leg in query.legs)
            raw = self.fixture["hub_responses"].get(route_key)
            if raw is None:
                raise base.ProviderError("No hub fixture response for " + route_key)
            payload = json.loads(json.dumps(raw))
        offers = []
        meta = {"provider": "mock", "shape": self.shape}
        expansion_notes = city_airports.expansion_notes(query.legs)
        if expansion_notes:
            meta["notes"] = expansion_notes

        if self.shape == "google_flights_deals":
            for raw in payload.get("deals") or []:
                offer = GoogleFlightsProvider._parse_deal(
                    raw, query.currency,
                    (query.discovery or {}).get("home_country"))
                if offer:
                    offers.append(offer)
            offers, rejected = GoogleFlightsProvider._filter_allowlisted_offers(
                offers, query.airlines)
            offers.sort(key=GoogleFlightsProvider._deals_sort_key)
            max_deals = (query.discovery or {}).get("max_deals", query.max_offers)
            offers = offers[:max_deals]
            meta["discovery"] = True
            meta.setdefault("notes", []).append(
                "Relative low-fare discovery ranks reported discount percentage "
                "before price; results are destination-free deals, not a fixed "
                "route comparison.")
            if rejected:
                meta["notes"].append(
                    "Local airline allow-list verification excluded {0} deal(s).".format(
                        rejected))
        elif self.shape == "amadeus_date_grid":
            from .amadeus import parse_date_grid
            offers, grid_meta = parse_date_grid(payload, query,
                                                environment="test",
                                                provider_name="mock")
            meta.update(grid_meta)
        elif self.shape == "amadeus":
            from .amadeus import _parse_offer
            for raw in payload.get("data", []):
                offer = _parse_offer(raw, query.currency)
                if offer:
                    offers.append(offer)
        else:
            is_round_trip = len(query.legs) == 2 and not query.is_open_jaw
            trip_type = 1 if is_round_trip else (3 if len(query.legs) > 1 else 2)
            options = (payload.get("best_flights") or []) + \
                      (payload.get("other_flights") or [])
            for raw in options:
                offer = GoogleFlightsProvider._parse_option(
                    raw, query.currency, trip_type)
                if offer:
                    # Mirror the real provider: requested dates are recorded
                    # even when a multi-city response itemises only one leg.
                    if query.legs:
                        offer["extras"].setdefault(
                            "departure_date", query.legs[0].date)
                    if len(query.legs) > 1:
                        offer["extras"].setdefault(
                            "return_date", query.legs[-1].date)
                    offers.append(offer)
            offers, rejected = GoogleFlightsProvider._filter_allowlisted_offers(
                offers, query.airlines)
            if rejected:
                meta.setdefault("notes", []).append(
                    "Local airline allow-list verification excluded {0} offer(s) "
                    "whose marketing carrier was missing from, or outside, the "
                    "configured allow-list.".format(rejected))
            # Mirror the real provider's meta so tests see the same caveats.
            insights = payload.get("price_insights") or {}
            if insights:
                meta["price_level"] = insights.get("price_level")
                meta["typical_price_range"] = insights.get("typical_price_range")
            if is_round_trip:
                meta["note"] = (
                    "Round-trip total price; the return itinerary is not "
                    "itemised. Set provider.resolve_return: true to fetch it "
                    "(costs one extra API call per watch).")

        if self.jitter_pct:
            # Random drift per call so repeated runs move prices and the
            # fluctuation rules get genuinely exercised.
            factor = 1.0 + random.uniform(-self.jitter_pct, self.jitter_pct) / 100.0
            for offer in offers:
                offer["price"] = round(offer["price"] * factor, 2)

        if self.shape == "google_flights_deals":
            offers.sort(key=GoogleFlightsProvider._deals_sort_key)
        else:
            offers.sort(key=lambda item: item["price"])
        self.log("mock({0}) -> {1} offers".format(self.shape, len(offers)))
        return offers[:query.max_offers], meta


PROVIDERS[MockProvider.name] = MockProvider


def build(name, settings=None, logger=None):
    """Instantiate a provider by name with its config sub-section."""
    settings = dict(settings or {})
    if name not in PROVIDERS:
        raise base.ProviderError(
            "Unknown provider '{0}'. Available: {1}".format(
                name, ", ".join(sorted(PROVIDERS))))

    # `name` is part of the config block but not a constructor argument.
    settings.pop("name", None)
    cls = PROVIDERS[name]
    try:
        return cls(logger=logger, **settings)
    except TypeError as exc:
        raise base.ProviderError(
            "Bad settings for provider '{0}': {1}. Check the provider block in "
            "your config.".format(name, exc))


def capability_table():
    """Rendered support matrix, used by `providers` command and the docs."""
    tokens = sorted({value for key, value in vars(base).items()
                     if key.startswith("CAP_")})
    names = [GoogleFlightsProvider.name, AmadeusProvider.name]
    rows = [["capability"] + names]
    for token in tokens:
        row = [token]
        for provider_name in names:
            row.append("yes" if token in PROVIDERS[provider_name].CAPABILITIES
                       else "-")
        rows.append(row)
    return rows
