# Autonomous Trading Company — showcase

A multi-agent system that runs a paper-trading operation on its own: it reads, researches, proposes strategies, backtests them, trades the survivors on paper, and learns from the results. It has been running continuously in production with a single operator and simulated capital.

**To date:** ~3,800 agent runs · ~14,900 model calls · ~10,100 backtests · 840+ tests in the full system · **$3.23 total model spend**.

> **This repository is a curated subset.** It contains the agent contracts, the data model, and three modules that show how the system is designed to fail safely — with their tests. The runtime, venue adapters, operator UI and operations tooling are kept private. A full walkthrough is available on request.

---

## Architecture

**Agents propose, the deterministic core disposes.**

A deterministic conductor drives an eight-stage cycle:

```
RECOVER → RETROSPECT → PLAN → EXECUTE → VALIDATE → DECIDE_GATE → EVALUATE → LEARN
```

Twelve LLM agent classes do the open-ended work — orchestrator, strategy proposer, critic, literature, data researcher, data cleaner, feature engineer, backtest analyst, paper trader, trade decider, feedback, toolsmith. Each has a contract in this repo: an `AGENTS.md` brief, a JSON output schema, and worked examples (`atc/agents/*/`).

Nothing an agent returns is acted on directly. It is validated against its schema and applied by deterministic code:

- **Declared write-sets.** An agent can only write the tables its class is authorised for. State lives in SQLite (WAL mode) with versioned migrations and per-writer authorisation (`atc/storage/`).
- **An unbypassable risk gate.** Every order passes the same gate, paper or live — literally the same function.
- **External text is data, never instruction** (`atc/core/untrusted.py`).
- **A model router** reserves a metered model for orchestrator, strategy proposer and critic; everything else runs on local Ollama.

## What is in this repo

### 1. `atc/core/pipeline_health.py` — monitor the handoffs, not the components

The central lesson of the project came from an incident in which **every subsystem reported healthy while the risk gate refused 3,900 of 3,920 orders.** No alarm fired, because no component was broken.

The cause was two freshness windows that disagreed — 360 s upstream, 60 s downstream — plus a cached quote aging out mid-pass across 335 strategies. Each component was correct on its own terms. The failure lived in the arrow between them.

This module audits the arrows: thirteen checks, each asking of one boundary *is work crossing it, and if not, why not?* Automatic remediation where the fix is mechanical and reversible, escalation where it is not. Three principles are enforced by its tests:

1. **A success rate cannot see silence.** A planner that is never dispatched produces no failures.
2. **A 24-hour window cannot tell "broken now" from "broken this morning."** Recent runs decide whether a fault is current or already mended.
3. **An alarm that keeps firing after the fix teaches everyone to ignore it.** A persistent condition is raised once.

### 2. `atc/core/untrusted.py` — external text carries no authority

The company reads papers, news and feeds written by strangers. Filtering cannot make that text safe to obey — a stripper that removes "ignore previous instructions" is beaten by a paraphrase. So this module does not try. It makes the **origin** unambiguous and the bytes inspectable: every fragment is labelled with its source, invisible characters are removed, fragments are truncated, and delimiters that would let a fragment close its own envelope are neutralised. The boundary is architectural, not a filter.

### 3. `atc/core/self_tuning.py` — a bar the system cannot lower

The system tunes its own operating parameters within bounds. It may not touch its standard of proof or its risk limits: `FROZEN_KEYS` refuses them structurally. The evidence bar for promotion — 20 closed trades, Sharpe ≥ 1, drawdown ≤ 15%, all net of costs — is not something the company can renegotiate with itself. A company allowed to lower its own bar will lower it until everything passes.

### Also included

- `atc/agents/` — contracts for all twelve agent classes, plus the shared handbook
- `atc/storage/` — schema, migrations and repository layer
- `atc/adapters/fetchers.py` — read-only public data fetchers (market, macro, research), which fail closed for unregistered sources

## Cost discipline (full system)

- Budget classes with per-class token ceilings, and a daily call ceiling **paced across the day** rather than spent by evening.
- The orchestrator's context was bounded from 272k to 23k tokens after the first metered call.
- Metered spend is budgeted at the provider's **peak** rate on purpose: a budget that flatters itself is not a budget.

## Running the tests

```bash
pip install -e ".[test]"
pytest -q
```

68 tests, no external dependencies.

## How it was built

The architecture, constraints and failure analysis are mine. The code was written at speed with Claude Code and Codex under close review — which is exactly why the handoff audit, the evidence bar and the frozen limits exist: velocity without those constraints produces systems nobody can trust or debug.

---

© Marco Asproni. Published for review; all rights reserved.
