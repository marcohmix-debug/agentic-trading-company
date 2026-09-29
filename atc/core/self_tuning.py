"""The company adjusting its own settings from its own measurements.

Three numbers were wrong in ways the company could have noticed and could not
act on:

* the standard cycle ran on a 900 s cadence while an observed cycle took 1805 s
  end to end, so the schedule was asking for work faster than the work could be
  done and the micro cadence was starved for half an hour at a time;
* the agent queue drained one run at a time whatever the machine was doing;
* the liquidity guard shipped with a 25 bps spread ceiling chosen before a
  single book had been read.

Each of those has an objective signal already in the database.  Nothing here
requires an opinion -- only arithmetic on what happened.

WHAT MAY BE TUNED, AND WHY THAT LINE EXISTS
-------------------------------------------
Only parameters about *how fast and how much*: cadence, concurrency, the
liquidity thresholds.  The evidence bar, the risk caps, the live switch and the
integrity flags are frozen here, structurally, and ``FROZEN_KEYS`` refuses them
even if a future caller asks.

The reason is not caution, it is arithmetic.  A company that may lower its own
standard of proof will lower it until everything passes -- every strategy then
"succeeds" and the word stops meaning anything.  A company that may raise its
own daily-loss limit has no daily-loss limit.  Self-tuning is only worth having
where being wrong is *measurable*; where the parameter defines what counts as
being right, tuning it is just grading your own exam.

STABILITY
---------
Every adjustment is bounded (a step cap, plus a hard floor and ceiling), fired
only outside a dead band, and rate-limited per key.  A tuner that reacts to
every reading oscillates, and an oscillating cadence is worse than a badly
chosen fixed one.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

logger = logging.getLogger("atc.self_tuning")

#: Parameters this module must never touch, with the reason it must not.
#: Checked at write time, so a future caller cannot route around the policy by
#: passing a key in from somewhere else.
FROZEN_KEYS: dict[str, str] = {
    "evidence_bar_paper_days": "defines what counts as proof",
    "evidence_bar_min_trades": "defines what counts as proof",
    "evidence_bar_min_sharpe": "defines what counts as proof",
    "evidence_bar_max_dd_pct": "defines what counts as proof",
    "evidence_bar_net_of_costs": "defines what counts as proof",
    "live_trading_enabled": "real money",
    "max_daily_loss_pct": "a limit the company may not raise for itself",
    "max_drawdown_halt_pct": "a limit the company may not raise for itself",
    "max_order_notional_cents": "a limit the company may not raise for itself",
    "portfolio_cap_cents": "a limit the company may not raise for itself",
    "instrument_cap_cents": "a limit the company may not raise for itself",
    "strategy_cap_cents": "a limit the company may not raise for itself",
    "max_open_positions": "a limit the company may not raise for itself",
    "shorting_allowed": "a permission, not a setting",
    "leverage_allowed": "a permission, not a setting",
    "max_leverage": "a permission, not a setting",
    "paper_promotion_mode": "defines what counts as proof",
}

#: Minimum seconds between two adjustments of the same key.  An hour is longer
#: than any single cycle, so a change is always observed in effect before the
#: next one is considered.
COOLDOWN_S = 3600.0

# -- bounds: (floor, ceiling, max step per adjustment) -----------------------
BOUNDS: dict[str, tuple[float, float, float]] = {
    "cycle_interval_standard_s": (900.0, 21600.0, 900.0),
    "agent_workers": (1.0, 8.0, 1.0),
    "max_spread_bps": (5.0, 200.0, 15.0),
    # A hard ceiling of 100 bps: at four times the shipped setting the company
    # can still only ask for one percent of equity at risk per position, and
    # every size it produces is clamped by caps it cannot raise.
    "position_risk_bps": (5.0, 100.0, 10.0),
}

#: Observations required before a tuning is allowed to fire at all.
MIN_CYCLE_SAMPLES = 5
MIN_BOOK_SAMPLES = 30


@dataclass(frozen=True)
class Adjustment:
    key: str
    old: Any
    new: Any
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _percentile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return float(ordered[index])


class SelfTuner:
    """Reads the company's own record and corrects its operating settings."""

    def __init__(self, repo: Any, *, clock: Callable[[], datetime] | None = None,
                 cooldown_s: float = COOLDOWN_S):
        self.repo = repo
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.cooldown_s = cooldown_s

    # -- plumbing ----------------------------------------------------------

    def _now(self) -> datetime:
        value = self.clock()
        if not isinstance(value, datetime):
            value = datetime.fromtimestamp(float(value), timezone.utc)
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    def _iso(self) -> str:
        return self._now().isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def _current(self, key: str, default: float) -> float:
        row = self.repo.get("config_general", key)
        if not isinstance(row, dict):
            return default
        raw = row.get("value")
        try:
            return float(json.loads(raw) if isinstance(raw, str) else raw)
        except (TypeError, ValueError):
            return default

    def _last_change(self, key: str) -> datetime | None:
        latest: datetime | None = None
        for row in self.repo.filter("audit_events", action="self_tuned"):
            try:
                details = json.loads(row.get("details_json") or "{}")
            except (TypeError, ValueError):
                continue
            if details.get("key") != key:
                continue
            ts = _parse(row.get("ts"))
            if ts and (latest is None or ts > latest):
                latest = ts
        return latest

    def _cooling_down(self, key: str) -> bool:
        last = self._last_change(key)
        if last is None:
            return False
        return (self._now() - last).total_seconds() < self.cooldown_s

    def _apply(self, key: str, old: float, proposed: float, reason: str,
               evidence: dict[str, Any], *, integral: bool = False) -> Adjustment | None:
        """Clamp, step-limit and persist one adjustment."""
        if key in FROZEN_KEYS:
            # Defence in depth: the callers below never propose a frozen key,
            # but the policy has to hold at the write, not at the call site.
            logger.warning("refusing to tune %s: %s", key, FROZEN_KEYS[key])
            return None
        floor, ceiling, step = BOUNDS[key]
        proposed = max(old - step, min(old + step, proposed))
        proposed = max(floor, min(ceiling, proposed))
        if integral:
            proposed = float(int(round(proposed)))
        if proposed == old:
            return None

        value = int(proposed) if integral else round(proposed, 4)
        self.repo.upsert(
            "config_general",
            {"key": key, "value": json.dumps(value)},
            conflict_columns=("key",),
            writer="operator",
        )
        ts = self._iso()
        details = {"key": key, "old": old, "new": value, "reason": reason, **evidence}
        self.repo.put("audit_events", {
            "ts": ts, "actor": "self_tuner", "action": "self_tuned",
            "entity_type": "config_general", "entity_id": key,
            "details_json": details,
            "dedup_key": f"self_tuned:{key}:{ts}", "created_at": ts,
        }, writer="system")
        try:
            self.repo.put("improvement_log", {
                "id": uuid.uuid4().hex, "kind": "tuning", "action": f"{key}: {old} -> {value}",
                "target_type": "config_general", "target_id": key,
                "evidence_refs": json.dumps([reason], separators=(",", ":")),
                "status": "APPLIED",
            })
        except Exception:
            pass
        logger.info("self-tuned %s: %s -> %s (%s)", key, old, value, reason)
        return Adjustment(key, old, value, reason, details)

    # -- the measurements --------------------------------------------------

    def _cycle_durations(self) -> list[float]:
        """Seconds each recent standard cycle took, start to last committed stage."""
        durations: list[float] = []
        stages_by_cycle: dict[str, list[datetime]] = {}
        for row in self.repo.filter("cycle_stages"):
            committed = _parse(row.get("committed_at"))
            if committed is not None:
                stages_by_cycle.setdefault(str(row.get("cycle_id") or ""), []).append(committed)
        cycles = [row for row in self.repo.filter("cycles")
                  if row.get("kind") == "standard" and row.get("status") == "DONE"]
        cycles.sort(key=lambda row: str(row.get("created_at") or ""))
        for row in cycles[-20:]:
            started = _parse(row.get("created_at"))
            stamps = stages_by_cycle.get(str(row.get("id") or ""))
            if started is None or not stamps:
                continue
            durations.append(max(stamps).timestamp() - started.timestamp())
        return [value for value in durations if value >= 0]

    def tune_cycle_interval(self) -> Adjustment | None:
        """Ask for work no faster than the work can be done.

        A cadence shorter than the cycle it schedules does not produce more
        research: the conductor is inside the previous cycle when the next comes
        due, so the only thing the surplus produces is a starved micro cadence
        and a heartbeat that looks dead.
        """
        key = "cycle_interval_standard_s"
        if self._cooling_down(key):
            return None
        durations = self._cycle_durations()
        if len(durations) < MIN_CYCLE_SAMPLES:
            return None
        # p90, not the mean: the cadence has to survive the slow cycles, and a
        # mean is dragged down by the fast ones that were never the problem.
        observed = _percentile(durations, 0.9)
        current = self._current(key, 900.0)
        # A 20% dead band on either side: the target is "comfortably longer than
        # a slow cycle", not "exactly one cycle", which would re-tune forever.
        target = observed * 1.2
        if 0.8 * current <= target <= 1.25 * current:
            return None
        return self._apply(
            key, current, target,
            f"p90 standard cycle takes {observed:.0f}s against a {current:.0f}s cadence",
            {"samples": len(durations), "p90_seconds": round(observed, 1),
             "median_seconds": round(_percentile(durations, 0.5), 1)},
            integral=True,
        )

    def tune_agent_workers(self) -> Adjustment | None:
        """Match concurrency to whether the queue is actually backing up.

        Raised only when there is a real backlog AND the runs being produced
        are mostly succeeding: adding workers to a queue full of work that
        fails just multiplies the failure rate and the model spend.
        """
        key = "agent_workers"
        if self._cooling_down(key):
            return None
        runs = self.repo.filter("agent_runs")
        queued = sum(1 for row in runs if row.get("status") == "QUEUED")
        recent = sorted(runs, key=lambda row: str(row.get("created_at") or ""))[-100:]
        finished = [row for row in recent if row.get("status") in {"SUCCEEDED", "FAILED", "KILLED"}]
        if len(finished) < 20:
            return None
        success = sum(1 for row in finished if row.get("status") == "SUCCEEDED") / len(finished)
        current = self._current(key, 3.0)

        if queued >= 10 and success >= 0.7:
            return self._apply(key, current, current + 1,
                               f"{queued} runs queued with a {success:.0%} success rate",
                               {"queued": queued, "success_rate": round(success, 3)},
                               integral=True)
        if success < 0.4 and current > 1:
            return self._apply(key, current, current - 1,
                               f"only {success:.0%} of recent runs succeeded; "
                               f"more concurrency would multiply the waste",
                               {"queued": queued, "success_rate": round(success, 3)},
                               integral=True)
        return None

    def tune_spread_ceiling(self) -> Adjustment | None:
        """Calibrate the liquidity guard against the spreads actually observed.

        The shipped 25 bps was chosen before a single book had been read.  Set
        too tight it silently refuses every entry, which looks exactly like a
        company that has stopped finding trades.
        """
        key = "max_spread_bps"
        if self._cooling_down(key):
            return None
        try:
            rows = self.repo.filter("orderbook_snapshots")
        except Exception:
            return None
        spreads = [float(row["spread_bps"]) for row in rows
                   if row.get("spread_bps") is not None]
        if len(spreads) < MIN_BOOK_SAMPLES:
            return None
        current = self._current(key, 25.0)
        # Admit the routine spread and refuse the exceptional one.  p75 lets
        # three quarters of observed books through; a guard that refuses the
        # median market is not a guard, it is an outage.
        target = _percentile(spreads, 0.75)
        if 0.7 * current <= target <= 1.4 * current:
            return None
        return self._apply(
            key, current, target,
            f"p75 observed spread is {target:.1f}bps against a {current:.1f}bps ceiling",
            {"samples": len(spreads), "p50": round(_percentile(spreads, 0.5), 2),
             "p75": round(target, 2), "p95": round(_percentile(spreads, 0.95), 2)},
        )

    def tune_position_risk(self) -> Adjustment | None:
        """Spend more of the risk budget when it is going unused, less when it hurts.

        Two measurable signals, both already in the database:

        * the caps and the daily loss limit doing the refusing.  When most
          recent refusals are E_*_CAP or H_DAILY_LOSS_LIMIT the company is
          asking for more room than it has, and the honest correction is to ask
          for less per position rather than to want a bigger limit.
        * the portfolio sitting far under its cap while the refusals are about
          something else entirely.  Risk budget that is never spent is not
          prudence, it is a strategy nobody funded.

        Raised only when the realised record is not losing.  Sizing up into a
        drawdown is how a bad week becomes a bad month.
        """
        key = "position_risk_bps"
        if self._cooling_down(key):
            return None
        decisions = sorted(self.repo.filter("risk_decisions"),
                           key=lambda row: str(row.get("created_at") or ""))[-200:]
        if len(decisions) < 50:
            return None
        refused = [row for row in decisions
                   if not str(row.get("decision") or "").upper().startswith("APPROVE")]
        binding = sum(1 for row in refused
                      if str(row.get("reason_code") or "").startswith(("E_", "H_")))
        current = self._current(key, 10.0)

        positions = self.repo.filter("positions", status="OPEN")
        exposure = sum(int(row.get("notional_cents") or 0) for row in positions)
        cap = self._current("portfolio_cap_cents", 1_000_000.0)
        utilisation = exposure / cap if cap > 0 else 0.0

        closed = [row for row in self.repo.filter("paper_trades") if row.get("closed")]
        realised = sum(int(row.get("pnl_cents") or 0) for row in closed)

        evidence = {"decisions": len(decisions), "refused": len(refused),
                    "cap_or_loss_refusals": binding,
                    "utilisation": round(utilisation, 3),
                    "closed_trades": len(closed), "realised_pnl_cents": realised}

        if refused and binding >= 0.5 * len(refused) and utilisation >= 0.8:
            return self._apply(
                key, current, current * 0.6,
                f"{binding} of {len(refused)} refusals are caps or the loss limit "
                f"with the book {utilisation:.0%} of the way to its ceiling",
                evidence)
        if utilisation <= 0.5 and binding <= 0.2 * max(1, len(refused)) and realised >= 0:
            return self._apply(
                key, current, current * 1.4,
                f"the book is at {utilisation:.0%} of its cap and the caps are not "
                f"what is refusing; unspent risk budget funds nothing",
                evidence)
        return None

    def run_once(self) -> list[Adjustment]:
        """One tuning pass.  Never raises: a bad reading is not worth a cycle."""
        adjustments: list[Adjustment] = []
        for tuning in (self.tune_cycle_interval, self.tune_agent_workers,
                       self.tune_spread_ceiling, self.tune_position_risk):
            try:
                result = tuning()
            except Exception as exc:
                logger.warning("tuning %s failed: %s", tuning.__name__, exc)
                continue
            if result is not None:
                adjustments.append(result)
        return adjustments


__all__ = ["Adjustment", "BOUNDS", "COOLDOWN_S", "FROZEN_KEYS", "SelfTuner"]
