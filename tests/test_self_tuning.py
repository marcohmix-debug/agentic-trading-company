"""The company correcting its own settings -- and the settings it may not touch.

The line these tests defend is not caution, it is arithmetic: a company allowed
to lower its own standard of proof will lower it until everything passes, and
one allowed to raise its own loss limit has no loss limit.  Self-tuning belongs
where being wrong is measurable.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from atc.core.self_tuning import BOUNDS, FROZEN_KEYS, SelfTuner
from atc.storage.repository import Repository

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def repo(tmp_path):
    return Repository(tmp_path / "atc.db")


def tuner(repo, **kwargs):
    return SelfTuner(repo, clock=lambda: NOW, **kwargs)


def iso(moment):
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def config(repo, key, value):
    repo.upsert("config_general", {"key": key, "value": json.dumps(value)},
                conflict_columns=("key",), writer="operator")


def cycle(repo, cycle_id, *, started, duration_s, kind="standard"):
    repo.put("cycles", {"id": cycle_id, "kind": kind, "status": "DONE",
                        "last_stage": "DONE", "goal": "t",
                        "created_at": iso(started)}, writer="system")
    repo.put("cycle_stages", {"cycle_id": cycle_id, "stage": "LEARN",
                              "status": "COMMITTED",
                              "committed_at": iso(started + timedelta(seconds=duration_s)),
                              "dedup_key": f"cycle:{cycle_id}:stage:LEARN"}, writer="system")


# ---------------------------------------------------------------------------
# cadence: ask for work no faster than the work can be done
# ---------------------------------------------------------------------------

def test_a_cadence_shorter_than_its_cycle_is_widened(repo):
    """The observed failure: 1805s cycles on a 900s schedule."""
    config(repo, "cycle_interval_standard_s", 900)
    for index in range(8):
        cycle(repo, f"c{index}", started=NOW - timedelta(hours=index + 1), duration_s=1800)

    adjustment = tuner(repo).tune_cycle_interval()
    assert adjustment is not None
    assert adjustment.new > 900
    assert json.loads(repo.get("config_general", "cycle_interval_standard_s")["value"]) == adjustment.new


def test_a_cadence_that_already_fits_is_left_alone(repo):
    config(repo, "cycle_interval_standard_s", 3600)
    for index in range(8):
        cycle(repo, f"c{index}", started=NOW - timedelta(hours=index + 1), duration_s=2900)

    assert tuner(repo).tune_cycle_interval() is None


def test_too_few_cycles_is_not_evidence(repo):
    config(repo, "cycle_interval_standard_s", 900)
    for index in range(2):
        cycle(repo, f"c{index}", started=NOW - timedelta(hours=index + 1), duration_s=5000)

    assert tuner(repo).tune_cycle_interval() is None


def test_one_adjustment_cannot_leap(repo):
    """A step cap and a ceiling, so a freak reading cannot park the company."""
    config(repo, "cycle_interval_standard_s", 900)
    for index in range(8):
        cycle(repo, f"c{index}", started=NOW - timedelta(hours=index + 1), duration_s=100_000)

    adjustment = tuner(repo).tune_cycle_interval()
    floor, ceiling, step = BOUNDS["cycle_interval_standard_s"]
    assert adjustment.new <= 900 + step
    assert adjustment.new <= ceiling


def test_a_key_is_not_retuned_inside_its_cooldown(repo):
    """A tuner that reacts to every reading oscillates, and an oscillating
    cadence is worse than a badly chosen fixed one."""
    config(repo, "cycle_interval_standard_s", 900)
    for index in range(8):
        cycle(repo, f"c{index}", started=NOW - timedelta(hours=index + 1), duration_s=1800)

    subject = tuner(repo)
    assert subject.tune_cycle_interval() is not None
    assert subject.tune_cycle_interval() is None      # same pass, still cooling


# ---------------------------------------------------------------------------
# concurrency
# ---------------------------------------------------------------------------

def agent_runs(repo, statuses):
    # agent_runs.agent_class_id is a real foreign key.
    from atc.agents.seed import seed_agent_classes

    seed_agent_classes(repo)
    for index, status in enumerate(statuses):
        repo.put("agent_runs", {
            "id": f"r{index}", "agent_class_id": "literature",
            "work_item_type": "summarize", "work_item_id": f"w{index}",
            "status": status, "retry_count": 0,
            "dedup_key": f"run:literature:w{index}",
            "created_at": iso(NOW - timedelta(minutes=index)),
        }, writer="system")


def test_a_backlog_of_succeeding_work_earns_another_worker(repo):
    config(repo, "agent_workers", 3)
    agent_runs(repo, ["SUCCEEDED"] * 25 + ["QUEUED"] * 12)

    adjustment = tuner(repo).tune_agent_workers()
    assert adjustment is not None and adjustment.new == 4


def test_a_backlog_of_failing_work_does_not(repo):
    """More concurrency on work that fails just multiplies the model spend."""
    config(repo, "agent_workers", 3)
    agent_runs(repo, ["FAILED"] * 25 + ["QUEUED"] * 12)

    adjustment = tuner(repo).tune_agent_workers()
    assert adjustment is not None and adjustment.new == 2   # reduced, not raised


def test_concurrency_never_drops_below_one(repo):
    config(repo, "agent_workers", 1)
    agent_runs(repo, ["FAILED"] * 25)
    assert tuner(repo).tune_agent_workers() is None


# ---------------------------------------------------------------------------
# liquidity ceiling
# ---------------------------------------------------------------------------

def books(repo, spreads):
    for index, spread in enumerate(spreads):
        repo.put("orderbook_snapshots", {
            "id": f"b{index}", "instrument": "BTCUSDT",
            "observed_at": iso(NOW - timedelta(minutes=index)),
            "mid_cents": 1_000_000, "spread_bps": spread,
            "bid_depth_cents": 1, "ask_depth_cents": 1, "imbalance": 0.0,
            "dedup_key": f"book:BTCUSDT:{index}", "created_at": iso(NOW),
        }, writer="system")


def test_a_ceiling_that_refuses_the_median_market_is_widened(repo):
    """25 bps was chosen before a single book had been read."""
    config(repo, "max_spread_bps", 25.0)
    books(repo, [60.0] * 40)

    adjustment = tuner(repo).tune_spread_ceiling()
    assert adjustment is not None and adjustment.new > 25.0


def test_a_well_calibrated_ceiling_is_left_alone(repo):
    config(repo, "max_spread_bps", 25.0)
    books(repo, [22.0] * 40)
    assert tuner(repo).tune_spread_ceiling() is None


def test_too_few_books_is_not_evidence(repo):
    config(repo, "max_spread_bps", 25.0)
    books(repo, [90.0] * 5)
    assert tuner(repo).tune_spread_ceiling() is None


# ---------------------------------------------------------------------------
# the line
# ---------------------------------------------------------------------------

def test_the_evidence_bar_and_the_risk_caps_are_frozen(repo):
    """These define what counts as proof and what risk is permitted.  Tuning
    them is grading your own exam."""
    for key in ("evidence_bar_min_sharpe", "evidence_bar_min_trades",
                "max_daily_loss_pct", "max_drawdown_halt_pct",
                "portfolio_cap_cents", "live_trading_enabled"):
        assert key in FROZEN_KEYS


def test_a_frozen_key_is_refused_at_the_write_not_the_call_site(repo):
    """Defence in depth: the policy holds even if a future caller asks."""
    subject = tuner(repo)
    assert subject._apply("evidence_bar_min_sharpe", 1.0, 0.1, "because", {}) is None
    assert repo.get("config_general", "evidence_bar_min_sharpe") is None


def test_no_tunable_key_is_also_a_frozen_one(repo):
    assert not (set(BOUNDS) & set(FROZEN_KEYS))


def test_a_pass_survives_a_bad_reading(repo):
    """A tuning that cannot be computed must not take the cycle down with it."""
    class Broken:
        def filter(self, *a, **k):
            raise RuntimeError("no")

        def get(self, *a, **k):
            raise RuntimeError("no")

    assert SelfTuner(Broken(), clock=lambda: NOW).run_once() == []
