"""Alert rule evaluation with incident lifecycle (open -> resolved).

A rule opens at most one incident at a time, so a sustained breach does not
spam duplicates; the incident resolves once the metric returns to normal."""
import logging
import operator
import sqlite3
import time
from typing import Optional

from .metrics import service_metrics

log = logging.getLogger("vigilaunty.alerts")

OPERATORS = {
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
}


def evaluate_rules(conn: sqlite3.Connection, now: Optional[float] = None) -> dict:
    now = now if now is not None else time.time()
    opened, resolved = [], []

    rules = conn.execute(
        "SELECT r.*, s.name AS service FROM alert_rules r "
        "JOIN services s ON s.id = r.service_id"
    ).fetchall()

    for rule in rules:
        metrics = service_metrics(conn, rule["service_id"], rule["window_minutes"], now)
        value = metrics[rule["metric"]]
        if value is None:  # no data in window: nothing to judge
            continue

        breached = OPERATORS[rule["operator"]](value, rule["threshold"])
        open_inc = conn.execute(
            "SELECT * FROM incidents WHERE rule_id = ? AND resolved_at IS NULL",
            (rule["id"],),
        ).fetchone()

        if breached and not open_inc:
            cur = conn.execute(
                "INSERT INTO incidents (rule_id, opened_at, peak_value, last_value) "
                "VALUES (?, ?, ?, ?)",
                (rule["id"], now, value, value),
            )
            opened.append(cur.lastrowid)
            log.warning(
                "incident opened",
                extra={"fields": {
                    "incident_id": cur.lastrowid, "service": rule["service"],
                    "metric": rule["metric"], "value": value,
                    "threshold": rule["threshold"], "severity": rule["severity"],
                }},
            )
        elif breached and open_inc:
            worst = max if rule["operator"] in (">", ">=") else min
            conn.execute(
                "UPDATE incidents SET last_value = ?, peak_value = ? WHERE id = ?",
                (value, worst(value, open_inc["peak_value"]), open_inc["id"]),
            )
        elif not breached and open_inc:
            conn.execute(
                "UPDATE incidents SET resolved_at = ?, last_value = ? WHERE id = ?",
                (now, value, open_inc["id"]),
            )
            resolved.append(open_inc["id"])
            log.info(
                "incident resolved",
                extra={"fields": {"incident_id": open_inc["id"], "service": rule["service"]}},
            )

    return {"evaluated": len(rules), "opened": opened, "resolved": resolved}
