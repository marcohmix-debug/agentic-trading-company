"""Checking the arrows between stages, not the boxes.

Every component reported healthy while the company was not learning from
anything new: sixteen sources ACTIVE with ten unreadable, an ingest pass that
aborted on the first of them before reaching the working feeds, and a planner
whose token budget sat below the cost of its own context.  A pipeline does not
fail as a component; it fails at the handoffs.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from atc.core.pipeline_health import (
    BROKEN,
    IDLE,
    OK,
    audit_pipeline,
    check_news_is_reaching_research,
    check_sources_are_readable,
    check_the_planner_is_deciding,
    check_trades_are_closing,
    record_pipeline_audit,
)
from atc.storage.repository import Repository

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
SINCE = NOW - timedelta(hours=24)


def iso(moment):
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


@pytest.fixture()
def repo(tmp_path):
    return Repository(tmp_path / "atc.db")


def source(repo, slug, kind, status, endpoint="https://example.org/feed"):
    repo.put("data_sources", {
        "id": slug, "slug": slug, "kind": kind, "endpoint": endpoint,
        "tos_ok": 1, "legality_note": "ok", "status": status,
    }, writer="system" if status == "PROPOSED" else "source_registry")


# ---------------------------------------------------------------------------
# sources -> fetchers
# ---------------------------------------------------------------------------

def test_an_active_source_nobody_can_read_is_reported_broken(repo):
    """ACTIVE means allowed. It does not mean we have code that can read it."""
    source(repo, "finnhub", "free_api", "ACTIVE", "https://finnhub.io/api/v1")

    check = check_sources_are_readable(repo)
    assert check.status == BROKEN
    assert "no fetcher" in check.detail
    assert "finnhub" in check.measured["unreadable"]


def test_readable_sources_pass(repo):
    source(repo, "coindesk_rss", "rss", "ACTIVE")
    assert check_sources_are_readable(repo).status == OK


def test_no_sources_is_idle_not_broken(repo):
    assert check_sources_are_readable(repo).status == IDLE


# ---------------------------------------------------------------------------
# news -> research
# ---------------------------------------------------------------------------

def test_active_feeds_that_have_never_produced_a_document_are_broken(repo):
    source(repo, "coindesk_rss", "rss", "ACTIVE")
    check = check_news_is_reaching_research(repo, SINCE)
    assert check.status == BROKEN
    assert "no news document" in check.detail


def test_no_feed_at_all_is_idle(repo):
    assert check_news_is_reaching_research(repo, SINCE).status == IDLE


# ---------------------------------------------------------------------------
# trades -> closed
# ---------------------------------------------------------------------------

def test_positions_that_never_close_teach_nothing(repo):
    repo.put("strategies", {"id": "s1", "version": 1, "status": "PAPER",
                            "config_json": {}}, writer="system")
    for index in range(3):
        repo.put("paper_trades", {"strategy_id": "s1", "instrument": "BTCUSDT",
                                  "pnl_cents": 0, "opened": iso(NOW), "closed": None,
                                  "quantity": "1", "fill_cents": 1}, writer="system")
    check = check_trades_are_closing(repo)
    assert check.status == BROKEN
    assert "none has ever closed" in check.detail


# ---------------------------------------------------------------------------
# cycle -> plan
# ---------------------------------------------------------------------------

def _orchestrator_runs(repo, succeeded, failed):
    from atc.agents.seed import seed_agent_classes

    seed_agent_classes(repo)
    index = 0
    for status, count in (("SUCCEEDED", succeeded), ("FAILED", failed)):
        for _ in range(count):
            repo.put("agent_runs", {
                "id": f"r{index}", "agent_class_id": "orchestrator",
                "work_item_type": "plan", "work_item_id": f"w{index}",
                "status": status, "retry_count": 0,
                "fail_reason": None if status == "SUCCEEDED" else "BUDGET_EXCEEDED",
                "dedup_key": f"run:orchestrator:w{index}",
                "created_at": iso(NOW - timedelta(hours=1)),
            }, writer="system")
            index += 1


def test_a_planner_that_almost_never_completes_is_broken(repo):
    """The first version of this check passed a planner that had succeeded once
    in sixty-two attempts, because one is not zero.  A health check that says a
    2% success rate is fine is worse than no health check."""
    _orchestrator_runs(repo, succeeded=1, failed=61)

    check = check_the_planner_is_deciding(repo, SINCE)
    assert check.status == BROKEN
    assert "1 of 62" in check.detail
    assert "BUDGET_EXCEEDED" in check.detail


def test_a_healthy_planner_passes(repo):
    _orchestrator_runs(repo, succeeded=8, failed=2)
    assert check_the_planner_is_deciding(repo, SINCE).status == OK


def test_a_planner_that_has_never_run_is_idle(repo):
    assert check_the_planner_is_deciding(repo, SINCE).status == IDLE


# ---------------------------------------------------------------------------
# the audit as a whole
# ---------------------------------------------------------------------------

def test_a_quiet_company_reports_no_faults(repo):
    """An empty queue is idle, never broken: crying wolf about a quiet Sunday
    is how a health check gets ignored."""
    audit = audit_pipeline(repo, now=NOW)
    assert audit["broken"] == 0
    assert audit["summary"] == "every stage is passing work to the next"


def test_every_broken_handoff_raises_one_incident(repo):
    source(repo, "finnhub", "free_api", "ACTIVE", "https://finnhub.io/api/v1")
    audit = audit_pipeline(repo, now=NOW)
    assert audit["broken"] >= 1

    raised = record_pipeline_audit(repo, audit)
    incidents = repo.filter("incidents", code="PIPELINE_STAGE_BROKEN")
    assert raised == len(incidents) == audit["broken"]
    assert any("no fetcher" in str(row.get("description")) for row in incidents)


def test_a_check_that_cannot_run_is_a_finding_not_a_crash():
    class Broken:
        def filter(self, *a, **k):
            raise RuntimeError("database on fire")

    audit = audit_pipeline(Broken(), now=NOW)
    assert isinstance(audit["broken"], int)
    assert audit["checks"]


# ---------------------------------------------------------------------------
# detection without action is how a finding becomes wallpaper
# ---------------------------------------------------------------------------

def test_an_unreadable_source_is_suspended_automatically(repo):
    from atc.core.pipeline_health import remediate_pipeline

    source(repo, "finnhub", "free_api", "ACTIVE", "https://finnhub.io/api/v1")
    source(repo, "coindesk_rss", "rss", "ACTIVE")

    actions = remediate_pipeline(repo, audit_pipeline(repo, now=NOW))
    assert any("finnhub" in a for a in actions)
    assert repo.get("data_sources", "finnhub")["status"] == "SUSPENDED"
    # The readable one is untouched.
    assert repo.get("data_sources", "coindesk_rss")["status"] == "ACTIVE"


def test_what_cannot_be_mended_is_escalated_loudly(repo):
    """An unfixable stall needs a person, and the way to get one is to be loud,
    not persistent."""
    from atc.core.pipeline_health import remediate_pipeline

    repo.put("strategies", {"id": "s1", "version": 1, "status": "PAPER",
                            "config_json": {}}, writer="system")
    repo.put("paper_trades", {"strategy_id": "s1", "instrument": "BTCUSDT",
                              "pnl_cents": 0, "opened": iso(NOW), "closed": None,
                              "quantity": "1", "fill_cents": 1}, writer="system")

    actions = remediate_pipeline(repo, audit_pipeline(repo, now=NOW))
    stalled = repo.filter("incidents", code="PIPELINE_STALLED")
    assert any("escalated" in a for a in actions)
    assert stalled and all(row["severity"] == "CRITICAL" for row in stalled)


def test_a_healthy_pipeline_is_left_entirely_alone(repo):
    from atc.core.pipeline_health import remediate_pipeline

    assert remediate_pipeline(repo, audit_pipeline(repo, now=NOW)) == []
    assert repo.filter("incidents") == []


# ---------------------------------------------------------------------------
# budgets -> work
# ---------------------------------------------------------------------------

def _calls(repo, cls, tokens, count=12):
    from atc.agents.seed import seed_agent_classes

    seed_agent_classes(repo)
    for index in range(count):
        run_id = f"{cls}-{index}"
        repo.put("agent_runs", {
            "id": run_id, "agent_class_id": cls, "work_item_type": "plan",
            "work_item_id": f"w{index}", "status": "SUCCEEDED", "retry_count": 0,
            "dedup_key": f"run:{cls}:w{index}", "created_at": iso(NOW),
        }, writer="system")
        repo.put("model_calls", {
            "id": f"mc-{cls}-{index}", "agent_run_id": run_id, "model": "ollama/x",
            "provider": "ollama", "tokens_in": tokens, "tokens_out": 0,
            "cost_cents": 0, "latency_ms": 10, "status": "OK",
            "dedup_key": f"mc:{cls}:{index}", "created_at": iso(NOW),
        }, writer="system")


def test_a_class_whose_ceiling_is_under_its_own_work_is_broken(repo):
    """max_tokens is checked against REPORTED usage, so a ceiling below the
    typical call fails every honest run.  The orchestrator lost 38 that way."""
    from atc.core.pipeline_health import check_budgets_cover_the_work

    _calls(repo, "literature", tokens=50_000)   # seeded ceiling is far below
    check = check_budgets_cover_the_work(repo)
    assert check.status == BROKEN
    assert "literature" in check.detail
    assert "honest runs fail" in check.detail


def test_a_class_that_fits_its_ceiling_passes(repo):
    from atc.core.pipeline_health import check_budgets_cover_the_work

    _calls(repo, "literature", tokens=1_000)
    assert check_budgets_cover_the_work(repo).status == OK


def test_too_few_calls_is_not_evidence_about_a_budget(repo):
    from atc.core.pipeline_health import check_budgets_cover_the_work

    _calls(repo, "literature", tokens=50_000, count=3)
    assert check_budgets_cover_the_work(repo).status == OK


# ---------------------------------------------------------------------------
# suggestions -> orders
# ---------------------------------------------------------------------------

def _decisions(repo, approved, rejected, reason="F_NO_MARKET"):
    repo.put("strategies", {"id": "gs1", "version": 1, "status": "PAPER",
                            "config_json": {}}, writer="system")
    index = 0
    for decision, count in (("APPROVED", approved), ("REJECTED", rejected)):
        for _ in range(count):
            repo.put("suggestions", {
                "id": f"s{index}", "venue": "paper", "instrument": "BTCUSDT",
                "direction": "LONG_ENTRY", "quantity": "0.001",
                "price_limit_cents": 100, "strategy_id": "gs1",
                "rationale": "test", "status": "PENDING",
                "dedup_key": f"sug:{index}",
                "created_at": iso(NOW - timedelta(hours=1)),
            }, writer="system")
            repo.put("risk_decisions", {
                "id": f"rd{index}", "suggestion_id": f"s{index}", "attempt": 1,
                "decision": decision,
                "reason_code": "" if decision == "APPROVED" else reason,
                "worst_case_cost_cents": 0, "policy_version": "v1",
                "checks_json": {},
                "created_at": iso(NOW - timedelta(hours=1)),
            }, writer="system")
            index += 1


def test_a_gate_that_refuses_almost_everything_is_broken(repo):
    """Four thousand refusals and eighty approvals raised no fault: every
    component was healthy and the company could not place an order."""
    from atc.core.pipeline_health import check_the_gate_is_approving

    _decisions(repo, approved=80, rejected=4199)
    check = check_the_gate_is_approving(repo, SINCE)
    assert check.status == BROKEN
    assert "F_NO_MARKET" in check.detail
    assert check.measured["approved"] == 80


def test_a_gate_that_refuses_the_bad_ones_passes(repo):
    from atc.core.pipeline_health import check_the_gate_is_approving

    _decisions(repo, approved=60, rejected=40)
    assert check_the_gate_is_approving(repo, SINCE).status == OK


def test_a_handful_of_decisions_says_nothing_about_a_gate(repo):
    from atc.core.pipeline_health import check_the_gate_is_approving

    _decisions(repo, approved=0, rejected=10)
    assert check_the_gate_is_approving(repo, SINCE).status == IDLE


# ---------------------------------------------------------------------------
# classes -> models
# ---------------------------------------------------------------------------

def _model(repo, model_id, status="ACTIVE"):
    repo.put("config_models", {
        "id": model_id, "provider": model_id.split("/")[0],
        "model_name": model_id.split("/")[-1], "local": 1,
        "status": status, "cost_in_per_1M": 0, "cost_out_per_1M": 0,
    }, writer="operator")


def test_a_fallback_that_names_no_model_is_broken(repo):
    from atc.agents.seed import seed_agent_classes
    from atc.core.pipeline_health import check_agent_defaults_name_real_models

    seed_agent_classes(repo)
    _model(repo, "ollama/qwen3:8b")
    check = check_agent_defaults_name_real_models(repo)
    assert check.status == BROKEN
    assert "orchestrator" in check.detail or check.measured["dangling"]


def test_a_dangling_fallback_is_repointed_at_a_real_model(repo):
    from atc.agents.seed import seed_agent_classes
    from atc.core.pipeline_health import audit_pipeline, remediate_pipeline

    seed_agent_classes(repo)
    _model(repo, "ollama/qwen3:8b")
    actions = remediate_pipeline(repo, audit_pipeline(repo, now=NOW))

    assert any("repointed" in action for action in actions)
    assert repo.get("agent_classes", "orchestrator")["default_model"] == "ollama/qwen3:8b"


# ---------------------------------------------------------------------------
# news -> read
# ---------------------------------------------------------------------------

def _documents(repo, arrived, read):
    source(repo, "hn", "rss", "ACTIVE")
    from atc.agents.seed import seed_agent_classes

    seed_agent_classes(repo)
    for index in range(arrived):
        doc_id = f"doc{index}"
        repo.put("literature", {
            "id": doc_id, "source_id": "hn", "title": f"t{index}",
            "url": f"https://example.org/{index}", "abstract": "a",
            "status": "SUMMARIZED" if index < read else "NEW",
            "dedup_key": f"lit:{doc_id}",
            "created_at": iso(NOW - timedelta(hours=2)),
        }, writer="system")
        if index < read:
            repo.put("agent_runs", {
                "id": f"lit{index}", "agent_class_id": "literature",
                "work_item_type": "summarize", "work_item_id": doc_id,
                "status": "SUCCEEDED", "retry_count": 0,
                "dedup_key": f"run:literature:{doc_id}",
                "created_at": iso(NOW - timedelta(hours=2)),
            }, writer="system")


def test_a_reading_queue_that_falls_behind_is_broken(repo):
    """Eleven read against ninety-one arrived, eighty-six unread.  A first
    draft called that healthy because 86 is less than 91."""
    from atc.core.pipeline_health import check_reading_keeps_up_with_ingest

    _documents(repo, arrived=91, read=11)
    check = check_reading_keeps_up_with_ingest(repo, SINCE)
    assert check.status == BROKEN
    assert check.measured["unread"] == 80


def test_a_queue_that_keeps_up_passes(repo):
    from atc.core.pipeline_health import check_reading_keeps_up_with_ingest

    _documents(repo, arrived=30, read=28)
    assert check_reading_keeps_up_with_ingest(repo, SINCE).status == OK


def test_a_small_queue_is_not_a_backlog(repo):
    from atc.core.pipeline_health import check_reading_keeps_up_with_ingest

    _documents(repo, arrived=10, read=0)
    assert check_reading_keeps_up_with_ingest(repo, SINCE).status == OK


def test_a_repaired_fallback_survives_the_next_boot(repo):
    """The audit repointed the twelve dangling defaults and the seed wrote the
    dead names straight back over them on restart, undoing the repair every
    time the company was started."""
    from atc.agents.seed import seed_agent_classes
    from atc.core.pipeline_health import audit_pipeline, remediate_pipeline

    seed_agent_classes(repo)
    _model(repo, "ollama/qwen3:8b")
    remediate_pipeline(repo, audit_pipeline(repo, now=NOW))
    assert repo.get("agent_classes", "orchestrator")["default_model"] == "ollama/qwen3:8b"

    seed_agent_classes(repo)

    assert repo.get("agent_classes", "orchestrator")["default_model"] == "ollama/qwen3:8b"
    assert audit_pipeline(repo, now=NOW)["checks"]


# ---------------------------------------------------------------------------
# a fault that keeps shouting after it is mended
# ---------------------------------------------------------------------------

def _orchestrator_history(repo, older, recent):
    """`older` and `recent` are lists of statuses, oldest first."""
    from atc.agents.seed import seed_agent_classes

    seed_agent_classes(repo)
    index = 0
    for offset_h, statuses in ((20, older), (1, recent)):
        for position, status in enumerate(statuses):
            repo.put("agent_runs", {
                "id": f"o{index}", "agent_class_id": "orchestrator",
                "work_item_type": "plan", "work_item_id": f"w{index}",
                "status": status, "retry_count": 0,
                "fail_reason": None if status == "SUCCEEDED" else "STALE_CYCLE",
                "dedup_key": f"run:orchestrator:w{index}",
                "created_at": iso(NOW - timedelta(hours=offset_h, minutes=-position)),
            }, writer="system")
            index += 1


def test_a_planner_mended_this_afternoon_is_not_still_broken(repo):
    """101 failures overnight on a local model that could not hold the output
    contract, then eight successes out of eight after moving to one that can.
    The daily average says 7%; the planner is working."""
    from atc.core.pipeline_health import check_the_planner_is_deciding

    _orchestrator_history(repo, older=["FAILED"] * 101, recent=["SUCCEEDED"] * 8)
    check = check_the_planner_is_deciding(repo, SINCE)

    assert check.status == OK
    assert "recovering" in check.detail
    assert check.measured["recent_succeeded"] == 8


def test_a_planner_still_failing_now_stays_broken(repo):
    from atc.core.pipeline_health import check_the_planner_is_deciding

    _orchestrator_history(repo, older=["FAILED"] * 50, recent=["FAILED"] * 12)
    assert check_the_planner_is_deciding(repo, SINCE).status == BROKEN


def test_two_lucky_runs_do_not_clear_a_broken_planner(repo):
    """Recovery has to be shown over enough runs to mean anything."""
    from atc.core.pipeline_health import check_the_planner_is_deciding

    _orchestrator_history(repo, older=["FAILED"] * 60, recent=["SUCCEEDED"] * 2)
    assert check_the_planner_is_deciding(repo, SINCE).status == BROKEN


def test_a_gate_mended_at_lunch_is_not_still_broken(repo):
    """The same lag that made the planner look broken after it was mended: a
    day's refusals include the ones caused by a fault fixed hours ago."""
    from atc.core.pipeline_health import check_the_gate_is_approving

    _decisions(repo, approved=0, rejected=4200)
    repo.put("strategies", {"id": "gs2", "version": 1, "status": "PAPER",
                            "config_json": {}}, writer="system")
    for index in range(200):
        repo.put("suggestions", {
            "id": f"late{index}", "venue": "paper", "instrument": "BTCUSDT",
            "direction": "LONG_ENTRY", "quantity": "0.001",
            "price_limit_cents": 100, "strategy_id": "gs2",
            "rationale": "test", "status": "EXECUTED",
            "dedup_key": f"sug:late:{index}",
            "created_at": iso(NOW - timedelta(minutes=10)),
        }, writer="system")
        repo.put("risk_decisions", {
            "id": f"rdlate{index}", "suggestion_id": f"late{index}", "attempt": 1,
            "decision": "APPROVED", "reason_code": "",
            "worst_case_cost_cents": 0, "policy_version": "v1",
            "checks_json": {},
            "created_at": iso(NOW - timedelta(minutes=10)),
        }, writer="system")

    check = check_the_gate_is_approving(repo, SINCE)
    assert check.status == OK
    assert "recovering" in check.detail


def test_documents_set_aside_count_as_leaving_the_queue(repo):
    """Sixty-four entries with no abstract were set aside in one pass and the
    check still called the backlog growing, because none was summarised."""
    from atc.core.pipeline_health import check_reading_keeps_up_with_ingest

    source(repo, "hn", "rss", "ACTIVE")
    for index in range(100):
        repo.put("literature", {
            "id": f"d{index}", "source_id": "hn", "title": "t",
            "url": f"https://example.org/{index}", "abstract": "a",
            "status": "IRRELEVANT" if index < 70 else "NEW",
            "dedup_key": f"lit:d{index}",
            "created_at": iso(NOW - timedelta(hours=2)),
        }, writer="system")

    check = check_reading_keeps_up_with_ingest(repo, SINCE)
    assert check.status == OK
    assert check.measured["handled"] == 70


# ---------------------------------------------------------------------------
# a gate refusing by policy is a gate, not a stall
# ---------------------------------------------------------------------------

def test_a_gate_refusing_because_the_book_is_full_is_not_broken(repo):
    """ETHUSDT sat at 197822 of a 200000 cap and BTCUSDT carried 866 USDT of
    unrealized loss, so the gate refused every entry: E_INSTRUMENT_CAP and
    H_DAILY_LOSS_LIMIT, no F_NO_MARKET at all.  That is the gate doing the one
    job it has, and an audit that paints it red until someone lifts a risk
    limit is training its reader to ignore it."""
    from atc.core.pipeline_health import check_the_gate_is_approving

    _decisions(repo, approved=0, rejected=300, reason="H_DAILY_LOSS_LIMIT")
    check = check_the_gate_is_approving(repo, SINCE)

    assert check.status == IDLE
    assert "by policy, not by fault" in check.detail
    assert check.measured["faults"] == 0


def test_a_gate_refusing_for_want_of_a_price_is_broken(repo):
    from atc.core.pipeline_health import check_the_gate_is_approving

    _decisions(repo, approved=0, rejected=300, reason="F_NO_MARKET")
    check = check_the_gate_is_approving(repo, SINCE)

    assert check.status == BROKEN
    assert check.measured["faults"] == 300


def test_a_gate_refusing_malformed_suggestions_is_broken(repo):
    """A_* means an agent is producing garbage, which is plumbing, not policy."""
    from atc.core.pipeline_health import check_the_gate_is_approving

    _decisions(repo, approved=0, rejected=300, reason="A_QUANTITY")
    assert check_the_gate_is_approving(repo, SINCE).status == BROKEN


# ---------------------------------------------------------------------------
# a planner that is not dispatched at all
# ---------------------------------------------------------------------------

def _standard_cycles(repo, count, *, after_hours=0.5):
    for index in range(count):
        repo.put("cycles", {
            "id": f"c{index}", "kind": "standard", "status": "DONE",
            "goal": "standard cycle", "stage_log_json": [],
            "created_at": iso(NOW - timedelta(hours=after_hours, minutes=-index)),
        }, writer="system")


def test_a_planner_that_stopped_being_dispatched_is_broken(repo):
    """It spent its daily call allowance by 18:25 and was silent for six hours
    while eighteen standard cycles ran unsteered -- and the check reported OK
    the whole time, because the runs it did manage had mostly succeeded.
    Silence is the failure mode a success rate is blindest to."""
    from atc.core.pipeline_health import check_the_planner_is_deciding

    _orchestrator_history(repo, older=["SUCCEEDED"] * 9, recent=[])
    _standard_cycles(repo, 18)

    check = check_the_planner_is_deciding(repo, SINCE)
    assert check.status == BROKEN
    assert "has been silent for" in check.detail
    assert check.measured["unsteered_cycles"] >= 18


def _paced_plans(repo, *, gap_h, last_h_ago, count=6):
    from atc.agents.seed import seed_agent_classes

    seed_agent_classes(repo)
    for index in range(count):
        at = NOW - timedelta(hours=last_h_ago + gap_h * (count - 1 - index))
        repo.put("agent_runs", {
            "id": f"pp{index}", "agent_class_id": "orchestrator",
            "work_item_type": "plan", "work_item_id": f"pw{index}",
            "status": "SUCCEEDED", "retry_count": 0,
            "dedup_key": f"run:orchestrator:pw{index}", "created_at": iso(at),
        }, writer="system")


def test_an_ordinary_gap_between_plans_is_not_a_fault(repo):
    """The planner is paced to spend its allowance across the day: a plan
    every ~2h45 with a standard cycle every ~19 minutes.  The first version
    counted five unsteered cycles as silence, so every healthy gap tripped it
    and raised a CRITICAL at 16:15 between plans at 14:21 and 17:05."""
    from atc.core.pipeline_health import check_the_planner_is_deciding

    _paced_plans(repo, gap_h=2.75, last_h_ago=2.0)
    _standard_cycles(repo, 9, after_hours=1.9)

    assert check_the_planner_is_deciding(repo, SINCE).status == OK


def test_silence_well_beyond_the_planners_own_rhythm_is_a_fault(repo):
    from atc.core.pipeline_health import check_the_planner_is_deciding

    _paced_plans(repo, gap_h=2.75, last_h_ago=9.0)
    check = check_the_planner_is_deciding(repo, SINCE)

    assert check.status == BROKEN
    assert check.measured["usual_gap_s"] == pytest.approx(2.75 * 3600, rel=0.01)


# ---------------------------------------------------------------------------
# a finding raised once, not every cycle
# ---------------------------------------------------------------------------

def test_the_same_finding_is_not_refiled_every_cycle(repo):
    """The audit runs every standard cycle.  A planner silent for a known
    reason -- it had spent its daily call allowance and the rolling window
    takes hours to clear -- filed two CRITICAL incidents every fifteen
    minutes: twenty for one fact in two hours.  That is exactly how a finding
    becomes wallpaper, which this module learned once today from the other
    side."""
    source(repo, "finnhub", "free_api", "ACTIVE", "https://finnhub.io/api/v1")
    audit = audit_pipeline(repo, now=NOW)

    first = record_pipeline_audit(repo, audit)
    second = record_pipeline_audit(repo, audit)

    assert first >= 1
    assert second == 0
    assert len(repo.filter("incidents", code="PIPELINE_STAGE_BROKEN")) == first


def test_a_finding_speaks_again_once_it_has_gone_quiet(repo):
    """Silencing a repeat is not silencing the finding: after the quiet period
    it is raised again, so a condition nobody fixed does not disappear."""
    from atc.core.pipeline_health import REPEAT_INCIDENT_QUIET_H

    source(repo, "finnhub", "free_api", "ACTIVE", "https://finnhub.io/api/v1")
    record_pipeline_audit(repo, audit_pipeline(repo, now=NOW))
    later = NOW + timedelta(hours=REPEAT_INCIDENT_QUIET_H + 1)

    assert record_pipeline_audit(repo, audit_pipeline(repo, now=later)) >= 1


def test_two_different_findings_both_get_heard(repo):
    """Quieting a repeat must not quiet a different stage."""
    source(repo, "finnhub", "free_api", "ACTIVE", "https://finnhub.io/api/v1")
    _orchestrator_history(repo, older=["SUCCEEDED"] * 9, recent=[])
    _standard_cycles(repo, 18)

    raised = record_pipeline_audit(repo, audit_pipeline(repo, now=NOW))
    components = {row["component"] for row
                  in repo.filter("incidents", code="PIPELINE_STAGE_BROKEN")}

    assert raised >= 2
    assert {"sources->fetchers", "cycle->plan"} <= components




def test_an_incident_is_closed_when_its_check_passes(tmp_path):
    """The 02:59 stall of 27 September stayed open twelve hours after trading resumed."""
    from atc.core.pipeline_health import resolve_cleared

    repo = Repository(tmp_path / "atc.db")
    repo.run_migrations()
    for code, stage in (("PIPELINE_STALLED", "paper->positions"), ("PIPELINE_STAGE_BROKEN", "paper->positions"),
                        ("PIPELINE_STALLED", "literature->read")):
        repo.put("incidents", {"ts": "2026-09-27T02:59:18.278Z", "severity": "CRITICAL", "code": code,
                               "component": stage, "description": "437 PAPER strategies opened no position",
                               "blocks_live": 0}, writer="system")
    audit = {"ts": "2026-09-27T18:00:00Z", "checks": [
        {"stage": "paper->positions", "status": "OK"}, {"stage": "literature->read", "status": "BROKEN"}]}
    assert resolve_cleared(repo, audit) == 2
    still_open = [row["component"] for row in repo.filter("incidents") if not row.get("resolution")]
    assert still_open == ["literature->read"]


def test_an_incident_about_a_check_that_no_longer_exists_is_closed(tmp_path):
    from atc.core.pipeline_health import resolve_cleared

    repo = Repository(tmp_path / "atc.db")
    repo.run_migrations()
    repo.put("incidents", {"ts": "2026-09-16T04:38:32.102Z", "severity": "CRITICAL",
                           "code": "PIPELINE_STALLED", "component": "strategies->evidence",
                           "description": "195 of 360 paper strategies hold longer than 1.5 days",
                           "blocks_live": 0}, writer="system")
    assert resolve_cleared(repo, {"ts": "2026-09-28T12:00:00Z",
                                  "checks": [{"stage": "paper->trades", "status": "OK"}]}) == 1
