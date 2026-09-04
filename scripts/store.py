"""SQLite-backed price history and fluctuation rules.

One row per watch per inspection run. Keeping the full best-offer JSON and
strategy metadata means reports can be regenerated later without re-querying a
provider, and a focus watch can reuse its scout's winning dates.
"""

import datetime
import json
import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshot (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    watch_id     TEXT    NOT NULL,
    strategy     TEXT    NOT NULL,
    captured_at  TEXT    NOT NULL,
    currency     TEXT    NOT NULL,
    best_price   REAL    NOT NULL,
    offer_count  INTEGER NOT NULL,
    fingerprint  TEXT    NOT NULL,
    detail_json  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshot_watch
    ON snapshot (watch_id, captured_at);

CREATE TABLE IF NOT EXISTS alert_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    watch_id  TEXT NOT NULL,
    kind      TEXT NOT NULL,
    price     REAL NOT NULL,
    fired_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alert_watch
    ON alert_log (watch_id, kind, fired_at);
"""


def _now():
    return datetime.datetime.now().replace(microsecond=0).isoformat()


class Store(object):
    def __init__(self, path):
        path = os.path.expanduser(path)
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    # ------------------------------------------------------------- snapshots

    def record_snapshot(self, watch_id, strategy, currency, best_price,
                        offer_count, fingerprint, detail):
        captured_at = _now()
        self.conn.execute(
            "INSERT INTO snapshot (watch_id, strategy, captured_at, currency, "
            "best_price, offer_count, fingerprint, detail_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (watch_id, strategy, captured_at, currency, float(best_price),
             int(offer_count), fingerprint, json.dumps(detail, ensure_ascii=False)),
        )
        self.conn.commit()
        return captured_at

    def latest_snapshot(self, watch_id):
        row = self.conn.execute(
            "SELECT * FROM snapshot WHERE watch_id = ? "
            "ORDER BY id DESC LIMIT 1", (watch_id,)).fetchone()
        return dict(row) if row else None

    def historical_min(self, watch_id, exclude_id=None):
        if exclude_id is None:
            row = self.conn.execute(
                "SELECT MIN(best_price) AS low FROM snapshot WHERE watch_id = ?",
                (watch_id,)).fetchone()
        else:
            row = self.conn.execute(
                "SELECT MIN(best_price) AS low FROM snapshot "
                "WHERE watch_id = ? AND id <> ?", (watch_id, exclude_id)).fetchone()
        return row["low"] if row and row["low"] is not None else None

    def recent_series(self, watch_id, limit=20):
        rows = self.conn.execute(
            "SELECT captured_at, best_price, currency FROM snapshot "
            "WHERE watch_id = ? ORDER BY id DESC LIMIT ?",
            (watch_id, limit)).fetchall()
        return [dict(row) for row in reversed(rows)]

    def history(self, watch_id, limit=20):
        rows = self.conn.execute(
            "SELECT * FROM snapshot WHERE watch_id = ? ORDER BY id DESC LIMIT ?",
            (watch_id, limit)).fetchall()
        return [dict(row) for row in rows]

    def watch_ids(self):
        rows = self.conn.execute(
            "SELECT DISTINCT watch_id FROM snapshot ORDER BY watch_id").fetchall()
        return [row["watch_id"] for row in rows]

    # ---------------------------------------------------------------- alerts

    def recent_alert(self, watch_id, kind, within_hours):
        cutoff = (datetime.datetime.now()
                  - datetime.timedelta(hours=within_hours)).replace(microsecond=0)
        row = self.conn.execute(
            "SELECT * FROM alert_log WHERE watch_id = ? AND kind = ? "
            "AND fired_at >= ? ORDER BY id DESC LIMIT 1",
            (watch_id, kind, cutoff.isoformat())).fetchone()
        return dict(row) if row else None

    def record_alert(self, watch_id, kind, price):
        self.conn.execute(
            "INSERT INTO alert_log (watch_id, kind, price, fired_at) "
            "VALUES (?, ?, ?, ?)", (watch_id, kind, float(price), _now()))
        self.conn.commit()


# ------------------------------------------------------------- fluctuation

class Trigger(object):
    def __init__(self, kind, severity, message):
        self.kind = kind
        self.severity = severity  # "info" | "deal" | "warn"
        self.message = message


def evaluate(store, watch_id, price, currency, target_price, alerting,
             previous, hist_min, price_level=None):
    """Compare the current best price against history and return alert triggers.

    The first observation only establishes a baseline - alerting on it would fire
    on every newly added watch.
    """
    triggers = []

    if previous is None:
        triggers.append(Trigger(
            "baseline", "info",
            "Baseline established at {0:.0f} {1}.".format(price, currency)))
    else:
        prev_price = float(previous["best_price"])
        delta = prev_price - price
        pct = (delta / prev_price * 100.0) if prev_price else 0.0

        drop_abs = alerting.get("drop_abs")
        drop_pct = alerting.get("drop_pct")
        hit_abs = drop_abs is not None and delta >= float(drop_abs)
        hit_pct = drop_pct is not None and pct >= float(drop_pct)
        if delta > 0 and (hit_abs or hit_pct):
            triggers.append(Trigger(
                "price_drop", "deal",
                "Dropped {0:.0f} {1} ({2:.1f}%) from {3:.0f} to {4:.0f}.".format(
                    delta, currency, pct, prev_price, price)))

        if alerting.get("alert_on_new_low", True) and hist_min is not None \
                and price < float(hist_min):
            triggers.append(Trigger(
                "new_low", "deal",
                "New all-time low: {0:.0f} {1} (previous best {2:.0f}).".format(
                    price, currency, float(hist_min))))

        rise_pct = alerting.get("alert_on_rise_pct")
        if rise_pct is not None and prev_price and delta < 0:
            rise = (-delta) / prev_price * 100.0
            if rise >= float(rise_pct):
                triggers.append(Trigger(
                    "price_rise", "warn",
                    "Rose {0:.0f} {1} ({2:.1f}%) from {3:.0f} to {4:.0f}.".format(
                        -delta, currency, rise, prev_price, price)))

    if target_price is not None and price <= float(target_price):
        triggers.append(Trigger(
            "target_hit", "deal",
            "At or below your target of {0:.0f} {1}.".format(
                float(target_price), currency)))

    if price_level == "low":
        triggers.append(Trigger(
            "market_low", "deal",
            "Google Flights marks this fare as low for the current market."))

    return _suppress(store, watch_id, price, triggers, alerting)


def _suppress(store, watch_id, price, triggers, alerting):
    """Drop repeat alerts of the same kind at effectively the same price."""
    quiet_hours = alerting.get("quiet_repeat_hours")
    if not quiet_hours:
        return triggers
    tolerance = float(alerting.get("quiet_price_tolerance", 1.0))
    kept = []
    for trigger in triggers:
        if trigger.severity == "info":
            kept.append(trigger)
            continue
        last = store.recent_alert(watch_id, trigger.kind, float(quiet_hours))
        if last and abs(float(last["price"]) - price) <= tolerance:
            continue
        kept.append(trigger)
    return kept


def sparkline(series):
    """Render a price series as a compact unicode trend bar."""
    blocks = "▁▂▃▄▅▆▇█"
    values = [float(item["best_price"]) for item in series]
    if len(values) < 2:
        return ""
    low, high = min(values), max(values)
    if high == low:
        return blocks[0] * len(values)
    span = high - low
    return "".join(
        blocks[min(int((value - low) / span * (len(blocks) - 1)), len(blocks) - 1)]
        for value in values
    )
