"""Does the work actually get from one stage to the next?

``component_health`` answers "is each subsystem up": agents, models, data,
conductor, trading, storage.  Every one of them reported healthy while the
company was, in fact, not learning from anything new.

The reason is that a pipeline does not fail as a component.  It fails at the
HANDOFFS -- the places where one stage is supposed to pass work to the next and
quietly does not:

* sixteen sources were ACTIVE and ten of them had no fetcher registered, so
  they were decoration.  Every component reported healthy: the sources table
  was full, the registry was working, the ingestor was running;
* the ingestor aborted its whole pass on the first of those, so the two news
  feeds that DID work were never reached.  "data" looked fine, because it was
  reporting on datasets that already existed;
* the orchestrator's token budget sat below the cost of its own context, so
  every honest run failed.  "agents" reported the failures as a rate, not as a
  cause.

Each check here is one arrow between two stages, and asks the only question
that matters about an arrow: is anything crossing it, and if not, why not.  A
stage with nothing to do is not a fault -- an empty queue is reported as idle,
never as broken, because crying wolf about a quiet Sunday is how a health check
gets ignored.

The output goes two places on purpose: into the incident log, so a human sees
it in the UI, and into the orchestrator's context, so the company can act on it
without waiting for a human to read anything.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

logger = logging.getLogger("atc.pipeline_health")

OK = "OK"
IDLE = "IDLE"          # nothing to do -- not a fault
BROKEN = "BROKEN"      # work is arriving and not crossing

#: How far back "recently" reaches for flow checks.
WINDOW_H = 24

#: Share of orchestrator runs that must complete for the planner to count as
#: working.  The company plans once per cycle, so a rate below this means most
#: cycles are steered by the deterministic fallback rather than by a decision.
MIN_PLAN_SUCCESS_RATE = 0.2

#: How many of the most recent planner runs decide whether a bad daily
#: average is a fault that is still happening or one already mended.
RECENT_PLAN_RUNS = 12
#: Silence counts as a fault once it lasts this many usual gaps between
#: plans, and never before this floor.
SILENCE_MULTIPLE = 2.0
MIN_SILENCE_S = 3 * 3600

#: How long the same finding stays quiet after it has been raised once.
#: The audit runs every standard cycle; a condition that takes hours to
#: clear must not file an incident every fifteen minutes.
REPEAT_INCIDENT_QUIET_H = 6
MIN_RECENT_PLAN_RUNS = 6

#: Share of risk decisions that must approve before the gate counts as a gate
#: rather than a wall.  A gate exists to refuse the orders that deserve
#: refusing; one that refuses 98 in 100 is not applying judgement, it is
#: misconfigured, and the company goes on generating suggestions nobody will
#: ever fill.
MIN_GATE_APPROVAL_RATE = 0.05

#: Below this many decisions the rate says nothing.
MIN_GATE_SAMPLE = 50

#: How many of the most recent decisions decide whether a bad daily rate
#: is a fault still happening or one already mended.
RECENT_GATE_DECISIONS = 200

#: Refusal codes that mean the plumbing is broken rather than the policy
#: binding.  A_* is a malformed suggestion -- an agent producing garbage --
#: and F_* is missing or stale market data.  Everything else (cash coverage,
#: leverage, exposure caps, rate limits, loss and drawdown limits) is the gate
#: deciding, which is the whole reason it exists.  A gate refusing because the
#: book is full and drawing down is not a stalled pipeline, and an audit that
#: paints it red until someone lifts a risk limit is training its reader to
#: ignore it.
GATE_FAULT_PREFIXES = ("A_", "F_")

#: Below this many unread documents a slow pass is just a quiet day.
MIN_UNREAD_TO_JUDGE = 20


@dataclass(frozen=True)
class StageCheck:
    """One handoff between two stages."""

    stage: str
    status: str
    detail: str
    measured: dict[str, Any] = field(default_factory=dict)

    @property
    def broken(self) -> bool:
        return self.status == BROKEN

    def as_dict(self) -> dict[str, Any]:
        return {"stage": self.stage, "status": self.status,
                "detail": self.detail, "measured": self.measured}


def _config_number(repo: Any, key: str, default: float) -> float:
    try:
        row = repo.get("config_general", key)
    except Exception:
        return default
    if not isinstance(row, Mapping):
        return default
    raw = row.get("value")
    try:
        return float(json.loads(raw) if isinstance(raw, str) else raw)
    except (TypeError, ValueError):
        return default


def _rows(repo: Any, table: str, **where: Any) -> list[dict[str, Any]]:
    try:
        return [dict(row) for row in repo.filter(table, **where)]
    except Exception:
        return []


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _recent(rows: Sequence[Mapping[str, Any]], field_name: str, since: datetime) -> int:
    count = 0
    for row in rows:
        stamp = _parse(row.get(field_name))
        if stamp is not None and stamp >= since:
            count += 1
    return count


# -- the handoffs ------------------------------------------------------------

def check_sources_are_readable(repo: Any) -> StageCheck:
    """ACTIVE means allowed; it does not mean we have code that can read it."""
    from ..adapters.fetchers import fetcher_for_source

    active = [row for row in _rows(repo, "data_sources") if row.get("status") == "ACTIVE"]
    unreadable: list[str] = []
    for source in active:
        try:
            fetcher_for_source(dict(source), fetch=lambda url: "")
        except Exception:
            unreadable.append(str(source.get("slug") or source.get("id")))
    if not active:
        return StageCheck("sources->fetchers", IDLE, "no active sources yet")
    if unreadable:
        return StageCheck(
            "sources->fetchers", BROKEN,
            f"{len(unreadable)} of {len(active)} ACTIVE sources have no fetcher "
            f"registered, so they yield nothing: {', '.join(sorted(unreadable)[:6])}",
            {"active": len(active), "unreadable": sorted(unreadable)})
    return StageCheck("sources->fetchers", OK, f"all {len(active)} active sources are readable",
                      {"active": len(active)})


def check_ingest_is_flowing(repo: Any, since: datetime) -> StageCheck:
    """Are the readable sources actually producing anything?"""
    datasets = _rows(repo, "datasets")
    literature = _rows(repo, "literature")
    fresh_data = _recent(datasets, "last_updated", since)
    fresh_lit = _recent(literature, "created_at", since)
    active = [row for row in _rows(repo, "data_sources") if row.get("status") == "ACTIVE"]
    if not active:
        return StageCheck("ingest->store", IDLE, "no active sources")
    if fresh_data == 0 and fresh_lit == 0:
        return StageCheck(
            "ingest->store", BROKEN,
            f"{len(active)} sources active but nothing stored in {WINDOW_H}h: "
            f"the ingest pass is not completing",
            {"active_sources": len(active), "datasets_{}h".format(WINDOW_H): 0,
             "literature_{}h".format(WINDOW_H): 0})
    return StageCheck("ingest->store", OK,
                      f"{fresh_data} datasets and {fresh_lit} documents in {WINDOW_H}h",
                      {f"datasets_{WINDOW_H}h": fresh_data, f"literature_{WINDOW_H}h": fresh_lit})


def check_news_is_reaching_research(repo: Any, since: datetime) -> StageCheck:
    """News and social only earn their place if the research loop reads them."""
    news = [row for row in _rows(repo, "literature") if row.get("kind") in {"news", "blog", "tweet"}]
    feeds = [row for row in _rows(repo, "data_sources")
             if row.get("kind") == "rss" and row.get("status") == "ACTIVE"]
    if not feeds:
        return StageCheck("news->research", IDLE, "no news or social feed is active")
    if not news:
        return StageCheck("news->research", BROKEN,
                          f"{len(feeds)} feeds active but no news document has ever been stored",
                          {"active_feeds": len(feeds), "documents": 0})
    unread = sum(1 for row in news if str(row.get("status") or "") == "NEW")
    return StageCheck("news->research", OK,
                      f"{len(news)} news documents stored, {unread} awaiting a reader",
                      {"documents": len(news), "unread": unread, "active_feeds": len(feeds)})


def check_backtests_are_running(repo: Any, since: datetime) -> StageCheck:
    backtests = _rows(repo, "backtests")
    fresh = _recent(backtests, "created_at", since)
    waiting = [row for row in _rows(repo, "strategies") if row.get("status") == "BACKTESTING"]
    if not waiting and not fresh:
        return StageCheck("strategies->backtests", IDLE, "nothing is waiting to be tested")
    if waiting and fresh == 0:
        return StageCheck("strategies->backtests", BROKEN,
                          f"{len(waiting)} strategies waiting and no backtest ran in {WINDOW_H}h",
                          {"waiting": len(waiting), f"ran_{WINDOW_H}h": 0})
    return StageCheck("strategies->backtests", OK, f"{fresh} backtests in {WINDOW_H}h",
                      {f"ran_{WINDOW_H}h": fresh, "waiting": len(waiting)})


def check_paper_is_trading(repo: Any, since: datetime) -> StageCheck:
    paper = [row for row in _rows(repo, "strategies") if row.get("status") == "PAPER"]
    trades = _rows(repo, "paper_trades")
    fresh = _recent(trades, "opened", since)
    if not paper:
        return StageCheck("paper->trades", IDLE, "no strategy has reached PAPER")
    if fresh == 0:
        return StageCheck("paper->trades", BROKEN,
                          f"{len(paper)} PAPER strategies opened no position in {WINDOW_H}h",
                          {"paper_strategies": len(paper), f"opened_{WINDOW_H}h": 0})
    return StageCheck("paper->trades", OK, f"{fresh} positions opened in {WINDOW_H}h",
                      {"paper_strategies": len(paper), f"opened_{WINDOW_H}h": fresh})


def check_trades_are_closing(repo: Any) -> StageCheck:
    """An open position teaches nothing.  Learning starts when one closes."""
    trades = _rows(repo, "paper_trades")
    if not trades:
        return StageCheck("trades->closed", IDLE, "no positions opened yet")
    closed = [row for row in trades if row.get("closed")]
    if not closed:
        return StageCheck("trades->closed", BROKEN,
                          f"{len(trades)} positions opened and none has ever closed, "
                          f"so there is no realised result to learn from",
                          {"open": len(trades), "closed": 0})
    return StageCheck("trades->closed", OK, f"{len(closed)} of {len(trades)} positions closed",
                      {"open": len(trades) - len(closed), "closed": len(closed)})


def check_results_become_lessons(repo: Any) -> StageCheck:
    """Closed trades have to turn into something the next proposal can read."""
    closed = [row for row in _rows(repo, "paper_trades") if row.get("closed")]
    if not closed:
        return StageCheck("closed->lessons", IDLE, "nothing has closed yet")
    attributions = [row for row in _rows(repo, "experiences")
                    if str(row.get("id") or "").startswith("exp-attrib:")]
    per_strategy: dict[str, int] = {}
    for row in closed:
        key = str(row.get("strategy_id") or "")
        per_strategy[key] = per_strategy.get(key, 0) + 1
    most = max(per_strategy.values()) if per_strategy else 0
    if not attributions:
        # Not broken: attribution needs enough closed trades on one strategy,
        # with contemporaries, before it can separate a rule from a regime.
        return StageCheck("closed->lessons", IDLE,
                          f"{len(closed)} trades closed, most on any one strategy is {most}; "
                          f"attribution needs a longer record before it can rule",
                          {"closed": len(closed), "max_per_strategy": most, "verdicts": 0})
    return StageCheck("closed->lessons", OK, f"{len(attributions)} attribution verdicts recorded",
                      {"closed": len(closed), "verdicts": len(attributions)})


def check_the_planner_is_deciding(repo: Any, since: datetime) -> StageCheck:
    """The orchestrator is the company's reasoning; a silent one is a stopped one."""
    runs = [row for row in _rows(repo, "agent_runs", agent_class_id="orchestrator")]
    fresh = [row for row in runs if (_parse(row.get("created_at")) or datetime.min.replace(
        tzinfo=timezone.utc)) >= since]
    if not runs:
        return StageCheck("cycle->plan", IDLE, "the orchestrator has never been dispatched")

    # A planner that is not dispatched at all produces no failures, so a
    # success-rate check cannot see it.  The orchestrator spent its daily call
    # allowance by 18:25 and was silent for fourteen hours while standard
    # cycles ran unsteered -- and this check reported OK the whole time.
    # Silence is the failure mode a rate is blindest to.
    #
    # Measured in time against the planner's OWN rhythm, not in cycles.  The
    # first version counted five unsteered cycles as silence; then the planner
    # was paced to spend its allowance across the day, a plan every ~2h45 with
    # a standard cycle every ~19 minutes, and every ordinary gap between two
    # healthy plans tripped it.  A check that fires on the schedule it was
    # built to protect is the wallpaper this module keeps warning about.
    ordered = sorted(str(row.get("created_at") or "") for row in runs)
    stamps = [value for value in (_parse(stamp) for stamp in ordered) if value is not None]
    latest_run = ordered[-1] if ordered else ""
    now_point = since + timedelta(hours=WINDOW_H)
    if stamps:
        recent = stamps[-7:]
        gaps = sorted((later - earlier).total_seconds()
                      for earlier, later in zip(recent, recent[1:]))
        usual = gaps[len(gaps) // 2] if gaps else 0.0
        allowed = max(MIN_SILENCE_S, SILENCE_MULTIPLE * usual)
        silent_for = (now_point - stamps[-1]).total_seconds()
        if silent_for > allowed:
            cycles = [row for row in _rows(repo, "cycles")
                      if str(row.get("kind") or "") == "standard"
                      and str(row.get("created_at") or "") > latest_run]
            return StageCheck(
                "cycle->plan", BROKEN,
                f"the orchestrator has been silent for {silent_for / 3600:.1f}h "
                f"({len(cycles)} standard cycles) since {latest_run[:19]}, against "
                f"a usual gap of {usual / 3600:.1f}h",
                {"silent_s": int(silent_for), "usual_gap_s": int(usual),
                 "unsteered_cycles": len(cycles), "last_plan": latest_run})

    succeeded = sum(1 for row in fresh if row.get("status") == "SUCCEEDED")
    failed = sum(1 for row in fresh if row.get("status") in {"FAILED", "KILLED"})
    # Not "none succeeded": the first version of this check passed a planner
    # that had succeeded once in sixty-two attempts, because one is not zero.
    # A stage that almost never completes is broken whether or not it has ever
    # completed, and a health check that says otherwise is worse than none.
    if fresh and succeeded / len(fresh) < MIN_PLAN_SUCCESS_RATE:
        reasons = sorted({str(row.get("fail_reason") or "")[:60] for row in fresh
                          if row.get("fail_reason")})
        # A day's average cannot tell "broken now" from "was broken this
        # morning".  The planner failed 101 times overnight on a local 8B model
        # that answered with extra top-level keys or prose instead of an
        # object, and has succeeded eight times out of eight since moving to a
        # model that can hold the contract -- and this check would have gone on
        # calling it broken until tomorrow.  A fault that keeps shouting after
        # it is mended teaches everyone to stop reading it, which is the exact
        # failure this module was written against.
        latest = sorted(fresh, key=lambda row: str(row.get("created_at") or ""))[-RECENT_PLAN_RUNS:]
        recent_ok = sum(1 for row in latest if row.get("status") == "SUCCEEDED")
        measured = {"runs": len(fresh), "succeeded": succeeded, "failed": failed,
                    "recent_runs": len(latest), "recent_succeeded": recent_ok}
        if len(latest) >= MIN_RECENT_PLAN_RUNS and recent_ok / len(latest) >= MIN_PLAN_SUCCESS_RATE:
            return StageCheck("cycle->plan", OK,
                              f"recovering: {recent_ok} of the last {len(latest)} runs "
                              f"succeeded, against {succeeded} of {len(fresh)} over {WINDOW_H}h",
                              measured)
        return StageCheck("cycle->plan", BROKEN,
                          f"only {succeeded} of {len(fresh)} orchestrator runs succeeded in "
                          f"{WINDOW_H}h ({succeeded / len(fresh):.0%}): "
                          + ("; ".join(reasons[:3]) or "no reason recorded"),
                          measured)
    if not fresh:
        return StageCheck("cycle->plan", BROKEN,
                          f"the orchestrator has not run at all in {WINDOW_H}h",
                          {"runs": 0})
    return StageCheck("cycle->plan", OK, f"{succeeded} of {len(fresh)} runs succeeded in {WINDOW_H}h",
                      {"runs": len(fresh), "succeeded": succeeded, "failed": failed})


def check_agent_output_is_accepted(repo: Any, since: datetime) -> StageCheck:
    """A model that answers and is always rejected is spend without product."""
    calls = _rows(repo, "model_calls")
    fresh = [row for row in calls
             if (_parse(row.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc)) >= since]
    if not fresh:
        return StageCheck("model->contract", IDLE, f"no model call in {WINDOW_H}h")
    bad = sum(1 for row in fresh if str(row.get("status") or "") != "OK")
    share = bad / len(fresh)
    if share > 0.4:
        return StageCheck("model->contract", BROKEN,
                          f"{share:.0%} of model answers were rejected in {WINDOW_H}h "
                          f"({bad} of {len(fresh)}): the contract is not being met",
                          {"calls": len(fresh), "rejected": bad, "share": round(share, 3)})
    return StageCheck("model->contract", OK,
                      f"{share:.0%} of {len(fresh)} model answers rejected",
                      {"calls": len(fresh), "rejected": bad, "share": round(share, 3)})


def check_budgets_cover_the_work(repo: Any) -> StageCheck:
    """A token ceiling below what the class actually costs fails every honest run.

    max_tokens is checked against REPORTED usage at ingestion, so a class whose
    ceiling sits under its own typical call is set up to fail for telling the
    truth.  The orchestrator lost 38 runs that way before anyone noticed, and
    the same defect was sitting in two other classes unremarked -- which is the
    argument for checking it here instead of measuring by hand once a crisis
    makes somebody look.
    """
    usage: dict[str, list[int]] = {}
    for row in _rows(repo, "model_calls"):
        total = int(row.get("tokens_in") or 0) + int(row.get("tokens_out") or 0)
        if total <= 0:
            continue
        run = row.get("agent_run_id")
        if run:
            usage.setdefault(str(run), []).append(total)
    by_class: dict[str, list[int]] = {}
    for run in _rows(repo, "agent_runs"):
        totals = usage.get(str(run.get("id")))
        if totals:
            by_class.setdefault(str(run.get("agent_class_id") or ""), []).extend(totals)

    starved: list[str] = []
    for row in _rows(repo, "agent_classes"):
        cls = str(row.get("id") or "")
        totals = sorted(by_class.get(cls) or [])
        budget = int(row.get("max_tokens") or 0)
        if len(totals) < 10 or budget <= 0:
            continue
        p95 = totals[int(0.95 * (len(totals) - 1))]
        if p95 > budget:
            starved.append(f"{cls} (budget {budget:,}, p95 {p95:,})")
    if not by_class:
        return StageCheck("budgets->work", IDLE, "no measured model usage yet")
    if starved:
        return StageCheck("budgets->work", BROKEN,
                          "token ceiling below the work it measures, so honest runs fail: "
                          + "; ".join(sorted(starved)),
                          {"starved": sorted(starved)})
    return StageCheck("budgets->work", OK,
                      f"all {len(by_class)} measured classes fit their token ceiling")


def _reason_counts(decisions: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in decisions:
        if str(row.get("decision") or "").upper().startswith("APPROVE"):
            continue
        code = str(row.get("reason_code") or "") or "UNKNOWN"
        counts[code] = counts.get(code, 0) + 1
    return counts


def check_the_gate_is_approving(repo: Any, since: datetime) -> StageCheck:
    """suggestion -> order: is the risk gate letting anything through?

    Every component was healthy while the gate refused 4199 orders and
    approved 80 in a day.  Nothing raised a fault: the agents ran, the
    strategies signalled, the gate answered every single time -- with no.
    The dominant reason was F_NO_MARKET on BTCUSDT and ETHUSDT, the two
    instruments whose prices the company fetches most often, because the
    window that accepted a quote was six minutes and the window that would
    trade on it was sixty seconds.  A refusal rate is a measurement the
    company can take of itself, and this is it.
    """
    decisions = [row for row in _rows(repo, "risk_decisions")
                 if (_parse(row.get("created_at")) or since) >= since]
    total = len(decisions)
    if total < MIN_GATE_SAMPLE:
        return StageCheck("suggestions->orders", IDLE,
                          f"{total} risk decisions in {WINDOW_H}h: too few to judge",
                          {"decisions": total})
    approved = sum(1 for row in decisions
                   if str(row.get("decision") or "").upper().startswith("APPROVE"))
    rate = approved / total
    reasons: dict[str, int] = {}
    for row in decisions:
        if not str(row.get("decision") or "").upper().startswith("APPROVE"):
            code = str(row.get("reason_code") or "UNKNOWN")
            reasons[code] = reasons.get(code, 0) + 1
    top = sorted(reasons.items(), key=lambda item: item[1], reverse=True)[:3]
    faults = sum(count for code, count in reasons.items()
                 if code.startswith(GATE_FAULT_PREFIXES))
    measured = {"approved": approved, "decisions": total,
                "approval_rate": round(rate, 4), "top_reasons": dict(top),
                "faults": faults, "policy_refusals": total - approved - faults}
    if rate < MIN_GATE_APPROVAL_RATE:
        # The same lag that made the planner look broken after it was mended:
        # a day's refusals include the ones caused by a fault fixed at lunch.
        # Judge on the most recent decisions, and report both numbers.
        latest = sorted(decisions, key=lambda row: str(row.get("created_at") or ""))[-RECENT_GATE_DECISIONS:]
        recent_ok = sum(1 for row in latest
                        if str(row.get("decision") or "").upper().startswith("APPROVE"))
        measured["recent_decisions"] = len(latest)
        measured["recent_approved"] = recent_ok
        listed = ", ".join(f"{code} x{count}" for code, count in top)
        if len(latest) >= MIN_GATE_SAMPLE and recent_ok / len(latest) >= MIN_GATE_APPROVAL_RATE:
            return StageCheck("suggestions->orders", OK,
                              f"recovering: {recent_ok} of the last {len(latest)} approved, "
                              f"against {approved} of {total} over {WINDOW_H}h ({listed})",
                              measured)
        recent_faults = sum(
            1 for row in latest
            if str(row.get("reason_code") or "").startswith(GATE_FAULT_PREFIXES))
        recent_listed = ", ".join(
            f"{code} x{count}" for code, count in sorted(
                _reason_counts(latest).items(), key=lambda item: item[1], reverse=True)[:3])
        if len(latest) >= MIN_GATE_SAMPLE and recent_faults * 2 < len(latest) - recent_ok:
            # The refusals are the policy binding, not the plumbing failing.
            return StageCheck("suggestions->orders", IDLE,
                              f"the gate is refusing by policy, not by fault: {recent_listed} "
                              f"in the last {len(latest)} decisions", measured)
        return StageCheck("suggestions->orders", BROKEN,
                          f"the gate approved {approved} of {total} orders "
                          f"({rate:.1%}); mostly {listed}", measured)
    return StageCheck("suggestions->orders", OK,
                      f"{approved} of {total} orders approved", measured)


def check_agent_defaults_name_real_models(repo: Any) -> StageCheck:
    """class -> model: the fallback has to name something that can be dialled.

    All twelve seeded defaults named models absent from config_models.  It
    stayed invisible because the router answers first and the default is only
    consulted when the router has nothing -- precisely the moment the company
    most needs a model to fall back to.
    """
    classes = _rows(repo, "agent_classes")
    if not classes:
        return StageCheck("classes->models", IDLE, "no agent classes registered")
    known = {str(row.get("id") or "") for row in _rows(repo, "config_models")}
    dangling = sorted(
        str(row.get("id") or "") for row in classes
        if str(row.get("default_model") or "") and str(row.get("default_model")) not in known
    )
    measured = {"dangling": dangling, "known_models": len(known)}
    if dangling:
        return StageCheck("classes->models", BROKEN,
                          f"{len(dangling)} classes fall back to a model that does not "
                          f"exist: {', '.join(dangling[:6])}", measured)
    return StageCheck("classes->models", OK,
                      f"all {len(classes)} classes name a registered model", measured)


def check_reading_keeps_up_with_ingest(repo: Any, since: datetime) -> StageCheck:
    """news -> read: documents must leave the queue faster than they enter it.

    Reading five per cycle while ninety arrive a day is not a backlog, it is a
    queue that never empties -- and the oldest items, the ones at the head, are
    the ones read again and again.
    """
    docs = _rows(repo, "literature")
    if not docs:
        return StageCheck("news->read", IDLE, "no documents ingested")
    unread = sum(1 for row in docs if str(row.get("status") or "") == "NEW")
    arrived = _recent(docs, "created_at", since)
    runs = [row for row in _rows(repo, "agent_runs", agent_class_id="literature")
            if str(row.get("status") or "") == "SUCCEEDED"]
    read = _recent(runs, "created_at", since)
    # A document leaves the queue by being read OR by being set aside, and
    # counting only the first made a draining queue look stuck: sixty-four
    # Hacker News entries with no abstract were set aside in one pass and the
    # check still reported the backlog "growing" because they were never
    # summarised.  What matters is whether the intake is being dealt with, so
    # measure the arrivals that are still waiting.
    pending = sum(1 for row in docs
                  if str(row.get("status") or "") == "NEW"
                  and (_parse(row.get("created_at")) or since) >= since)
    handled = arrived - pending
    measured = {"unread": unread, "arrived": arrived, "read": read,
                "pending_arrivals": pending, "handled": handled}
    if arrived == 0 and unread == 0:
        return StageCheck("news->read", IDLE, "nothing new to read", measured)
    # "Growing" is not "larger than a day's intake": eleven read against
    # ninety-one arrived, with eighty-six unread, was a queue falling behind by
    # any honest reading, and a first draft of this check called it healthy
    # because 86 is less than 91.
    if unread > MIN_UNREAD_TO_JUDGE and pending * 2 > arrived:
        return StageCheck("news->read", BROKEN,
                          f"{arrived} documents arrived in {WINDOW_H}h and {pending} of them "
                          f"are still waiting; {unread} unread and growing", measured)
    return StageCheck("news->read", OK,
                      f"{handled} of {arrived} arrivals dealt with ({read} read), "
                      f"{unread} still in the queue", measured)


def audit_pipeline(repo: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """Run every handoff check and summarise what is not crossing."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(hours=WINDOW_H)
    checks: list[StageCheck] = []
    for run_check in (
        lambda: check_sources_are_readable(repo),
        lambda: check_ingest_is_flowing(repo, since),
        lambda: check_news_is_reaching_research(repo, since),
        lambda: check_backtests_are_running(repo, since),
        lambda: check_paper_is_trading(repo, since),
        lambda: check_trades_are_closing(repo),
        lambda: check_results_become_lessons(repo),
        lambda: check_the_planner_is_deciding(repo, since),
        lambda: check_agent_output_is_accepted(repo, since),
        lambda: check_budgets_cover_the_work(repo),
        lambda: check_the_gate_is_approving(repo, since),
        lambda: check_agent_defaults_name_real_models(repo),
        lambda: check_reading_keeps_up_with_ingest(repo, since),
    ):
        try:
            checks.append(run_check())
        except Exception as exc:
            # A check that cannot run is itself a finding, not a crash.
            checks.append(StageCheck("unknown", BROKEN, f"check failed: {type(exc).__name__}: {exc}"))
    broken = [check for check in checks if check.broken]
    return {
        "ts": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "broken": len(broken),
        "checks": [check.as_dict() for check in checks],
        "summary": ("every stage is passing work to the next"
                    if not broken else
                    "; ".join(f"{c.stage}: {c.detail}" for c in broken)),
    }


def _already_raised(repo: Any, code: str, component: str, now: datetime) -> bool:
    """Is this exact finding already open and recent?

    The audit runs every standard cycle.  A condition that persists for a known
    reason -- the planner silent because it had spent its daily call allowance,
    which takes until the rolling window clears -- raised two CRITICAL
    incidents every fifteen minutes: twenty for one fact in two hours, on
    course for forty more before it cleared on its own.

    That is precisely how a finding becomes wallpaper, and this module already
    learned it once today from the other side: the planner-broken incident that
    sat unread behind six hundred others.  Repeating a finding does not make it
    louder, it makes the log unreadable.
    """
    cutoff = now - timedelta(hours=REPEAT_INCIDENT_QUIET_H)
    for row in _rows(repo, "incidents", code=code, component=component):
        if row.get("resolution"):
            continue            # cleared since; a recurrence is news
        stamp = _parse(row.get("ts"))
        if stamp is not None and stamp >= cutoff:
            return True
    return False


def record_pipeline_audit(repo: Any, audit: Mapping[str, Any]) -> int:
    """Raise one incident per broken handoff so a human sees it in the UI.

    One per finding, not one per audit: see _already_raised.
    """
    raised = 0
    now = _parse(audit.get("ts")) or datetime.now(timezone.utc)
    for check in audit.get("checks", []):
        if check.get("status") != BROKEN:
            continue
        component = str(check.get("stage") or "pipeline")
        if _already_raised(repo, "PIPELINE_STAGE_BROKEN", component, now):
            continue
        try:
            repo.put("incidents", {
                "ts": audit.get("ts"),
                # incidents.severity is INFO/WARN/CRITICAL -- there is no ERROR.
                # A broken handoff is not merely informational: work is arriving
                # at a stage and not leaving it, and nothing downstream will say
                # so on its own.
                "severity": "CRITICAL",
                "code": "PIPELINE_STAGE_BROKEN",
                "component": str(check.get("stage") or "pipeline"),
                "description": str(check.get("detail") or "")[:1000],
                "blocks_live": 0,
            }, writer="system")
            raised += 1
        except Exception as exc:
            # Never silent.  The first version swallowed this and reported zero
            # incidents raised while the audit had found two -- a health check
            # that cannot report is indistinguishable from a healthy system.
            logger.warning("could not record broken handoff %s: %s",
                           check.get("stage"), exc)
    if raised:
        logger.warning("pipeline audit: %d broken handoffs: %s", raised, audit.get("summary"))
    return raised


PIPELINE_INCIDENTS = ("PIPELINE_STAGE_BROKEN", "PIPELINE_STALLED")


def resolve_cleared(repo: Any, audit: Mapping[str, Any]) -> int:
    """Close the pipeline incidents whose check now passes.

    They were opened and never closed, so an incident said only that a stage
    HAD stalled.  "437 PAPER strategies opened no position in 24h", filed at
    02:59 on 27 September, was still open -- and still reported by the watchdog
    -- twelve hours after trading had resumed.  An open incident now means the
    condition holds as of the last audit.  A stage the audit no longer checks
    at all is not broken either: ten incidents about "strategies->evidence", a
    check removed with the minimum holding period on 19 September, would
    otherwise stay open for good.
    """
    stamp = str(audit.get("ts") or "")
    broken = {str(check.get("stage") or "") for check in audit.get("checks", [])
              if check.get("status") == BROKEN}
    closed = 0
    for code in PIPELINE_INCIDENTS:
        for row in _rows(repo, "incidents", code=code):
            if row.get("resolution") or str(row.get("component") or "") in broken:
                continue
            try:
                repo.update("incidents", row["id"],
                            {"resolution": f"cleared: check passed at {stamp}"}, writer="system")
                closed += 1
            except Exception as exc:
                logger.warning("could not close incident %s: %s", row.get("id"), exc)
    return closed


def remediate_pipeline(repo: Any, audit: Mapping[str, Any]) -> list[str]:
    """Apply the fixes that are mechanical, and shout about the ones that are not.

    Detection without action is how a finding becomes wallpaper.  This audit
    correctly reported the planner broken for five hours while the cause -- a
    spending ceiling tripped by free local calls -- sat in the incident log
    behind six hundred others, and the loop only restarted because a human
    asked how things were going.

    Only unambiguous, reversible remediations belong here.  A healer that
    guesses does more damage than the fault: suspending a source that yields
    nothing costs nothing and is undone by activating it again, whereas
    "restart the thing that looks stuck" is how a healer destroys work.
    Anything this cannot fix is escalated as a CRITICAL incident rather than
    quietly retried, because an unfixable stall needs a person, and the way to
    get one is to be loud, not persistent.
    """
    actions: list[str] = []
    mended: set[str] = set()
    escalated_at = _parse(audit.get("ts")) or datetime.now(timezone.utc)
    by_stage = {str(c.get("stage")): c for c in audit.get("checks", [])
                if c.get("status") == BROKEN}

    # An ACTIVE source with no fetcher produces nothing and, before the ingest
    # was made resilient, could stop the sources that work.  Suspending it is
    # pure cleanup: the row survives and activating it again is one update.
    check = by_stage.get("sources->fetchers")
    if check:
        for slug in check.get("measured", {}).get("unreadable", []):
            row = None
            for candidate in _rows(repo, "data_sources"):
                if str(candidate.get("slug")) == slug:
                    row = candidate
                    break
            if row is None:
                continue
            try:
                repo.update("data_sources", row["id"], {"status": "SUSPENDED"},
                            writer="source_registry")
                actions.append(f"suspended unreadable source {slug}")
                mended.add("sources->fetchers")
            except Exception:
                continue

    # A class whose fallback names a model nobody can dial is repointed at one
    # that exists.  Mechanical and reversible: it changes which model answers
    # when the router has nothing left, never what the class is allowed to do.
    check = by_stage.get("classes->models")
    if check:
        usable = sorted(
            str(row.get("id") or "") for row in _rows(repo, "config_models")
            if str(row.get("status") or "") == "ACTIVE" and not str(row.get("id") or "").strip() == ""
        )
        local_first = [m for m in usable if m.startswith("ollama/")] + [m for m in usable if not m.startswith("ollama/")]
        if local_first:
            replacement = local_first[0]
            for class_id in check.get("measured", {}).get("dangling", []):
                try:
                    repo.update("agent_classes", class_id,
                                {"default_model": replacement}, writer="system")
                    actions.append(f"repointed {class_id} fallback to {replacement}")
                    mended.add("classes->models")
                except Exception:
                    continue

    # Everything else: escalate.  A stalled handoff this cannot mend is exactly
    # the thing that must not be absorbed quietly.
    for stage, check in by_stage.items():
        if stage in mended:
            continue
        # Escalating is being loud once, not being loud repeatedly: see
        # _already_raised.  This one shouted twenty times in two hours about a
        # planner whose silence had a known cause and a known end.
        if _already_raised(repo, "PIPELINE_STALLED", stage, escalated_at):
            continue
        try:
            repo.put("incidents", {
                "ts": audit.get("ts"),
                "severity": "CRITICAL",
                "code": "PIPELINE_STALLED",
                "component": stage,
                "description": (f"{check.get('detail')} -- no mechanical remedy; "
                                f"this needs a change, not a retry")[:1000],
                "blocks_live": 0,
            }, writer="system")
            actions.append(f"escalated {stage}")
        except Exception as exc:
            logger.warning("could not escalate %s: %s", stage, exc)

    if actions:
        logger.warning("pipeline remediation: %s", "; ".join(actions))
    return actions


__all__ = [
    "BROKEN",
    "IDLE",
    "OK",
    "StageCheck",
    "audit_pipeline",
    "check_budgets_cover_the_work",
    "record_pipeline_audit",
    "resolve_cleared",
    "remediate_pipeline",
]
