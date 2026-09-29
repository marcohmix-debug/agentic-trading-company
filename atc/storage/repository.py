from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from numbers import Integral
from pathlib import Path
from typing import Any, Iterator

from .migrations import migrate


_DEFAULT_WRITER = object()
_OPERATOR_TABLES = {
    "config_general",
    "config_venues",
    "config_keys",
    "config_models",
    "config_model_providers",
    "config_strategies",
    "config_allowlist",
    "operator_messages",
}
_AGENT_ONLY_TABLES = {
    "suggestions",
    "strategy_ideas",
    "critiques",
    "data_sources",
    "datasets",
    "data_quality",
    "literature",
    "improvement_log",
    "experiences",
    "directives",
    "reports",
    "alerts",
    "tools",
    "backtests",
    "feature_sets",
}
#: Fields of ``backtests`` an agent may write.  An agent REQUESTS a backtest;
#: it does not conduct one.  The verdict columns -- status, the measured
#: figures, and the integrity flags -- belong to the backtester alone, because
#: the entire company rests on the claim that a PASSED row means a measurement
#: was taken.  Without this the storage layer accepted a fabricated
#: ``status=PASSED, sharpe=3.0, num_trades=500`` from an agent, and such a row
#: counts as evidence everywhere: it makes a strategy unkillable, it skews
#: every pruning decision, and it is indistinguishable from a real result.
_BACKTEST_REQUEST_FIELDS = frozenset({
    "id", "strategy_id", "dataset_id", "engine_version", "params_json",
    "success_criteria_json", "in_sample_range", "out_sample_range",
    "critique_id", "dedup_key", "created_at", "experiment_key",
})

_PROTECTED_AGENT_TABLES = {
    "portfolio",
    "positions",
    "ledger",
    "risk_decisions",
    "orders",
    "fills",
    "audit_events",
    "venue_freezes",
    "reconciliation_snapshots",
    "degraded_states",
}
_MODEL_DISCOVERY_RUNTIME_FIELDS = {
    "status", "cooldown_until", "last_error", "last_discovered_at", "updated_at",
}
_MONEY_FIELDS = {
    "portfolio": {"cash_available_cents", "cash_reserved_cents", "equity_cents"},
    "positions": {"avg_entry_price_cents", "notional_cents", "unrealized_pnl_cents"},
    "ledger": {"amount_cents"},
    "suggestions": {"price_limit_cents", "estimated_notional_cents"},
    "risk_decisions": {"worst_case_cost_cents"},
    "orders": {"limit_price_cents", "fees_cents", "filled_avg_price_cents"},
    "fills": {"price_cents", "fee_cents"},
    "paper_trades": {"fill_cents", "slippage_cents", "fees_cents", "pnl_cents"},
    "shadow_trades": {"entry_cents", "exit_cents"},
    "backtests": {"pnl_cents"},
    "model_calls": {"cost_cents"},
    "agent_runs": {"cost_cents"},
    "critiques": {"cost_cents"},
    "improvement_log": {"cost_cents"},
    "tools": {"cost_cents"},
    "metrics": {"equity_cents", "cash_cents", "realized_pnl_cents", "unrealized_pnl_cents", "exposure_cents", "cost_month_cents", "net_pnl_month_cents"},
}
_ENUMS = {
    ("portfolio", "venue"): {"bybit", "directa", "paper"},
    ("portfolio", "currency"): {"EUR", "USDT"},
    ("positions", "side"): {"LONG", "SHORT", "FLAT"},
    ("positions", "status"): {"OPEN", "CLOSING", "CLOSED"},
    ("ledger", "type"): {"DEPOSIT", "WITHDRAWAL", "FEE", "EXECUTION_BUY", "EXECUTION_SELL", "REALIZED_PNL", "FUNDING", "MODEL_COST", "COMPUTE_COST", "DATA_COST", "TAX_RESERVE", "CORRECTION"},
    ("ledger", "category"): {"trading", "operating"},
    ("suggestions", "direction"): {"LONG_ENTRY", "LONG_EXIT", "SHORT_ENTRY", "SHORT_EXIT"},
    ("suggestions", "status"): {"PENDING", "APPROVED", "REJECTED", "EXECUTED", "STALE", "EXPIRED"},
    ("risk_decisions", "decision"): {"APPROVED", "REJECTED"},
    ("orders", "side"): {"BUY", "SELL"},
    ("orders", "order_type"): {"MARKET", "LIMIT"},
    ("orders", "status"): {"PENDING", "SUBMITTED", "FILLED", "PARTIAL", "CANCELLED", "REJECTED_GATE", "REJECTED_EXCHANGE", "ERROR"},
    ("strategy_ideas", "market"): {"crypto_spot", "crypto_futures", "equities_it", "macro"},
    ("strategy_ideas", "critic_status"): {"PENDING", "PASSED", "REJECTED", "NEEDS_REVISION"},
    ("strategy_ideas", "status"): {"NEW", "CRITIC_PENDING", "CANDIDATE", "TESTED", "FUNDED", "DEAD"},
    ("strategies", "status"): {"BACKTESTING", "PAPER", "LIVE", "FROZEN", "DEAD", "ARCHIVED"},
    ("backtests", "status"): {"RUNNING", "PASSED", "FAILED", "INCONCLUSIVE"},
    ("literature", "status"): {"NEW", "SUMMARIZED", "CRITIQUED", "APPLIED", "SUPERSEDED", "IRRELEVANT"},
    ("agent_runs", "status"): {"QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "KILLED", "QUOTA_WAIT"},
    ("model_calls", "status"): {"OK", "RATE_LIMITED", "TIMEOUT", "INVALID_OUTPUT", "REJECTED_POLICY", "ERROR"},
    ("cycles", "kind"): {"micro", "standard", "deep", "report"},
    ("cycles", "status"): {"RUNNING", "DONE", "CRASHED", "RESUMED"},
    ("compliance_rules", "decision"): {"allowed", "rejected", "human_review_required"},
    ("alerts", "status"): {"OPEN", "ACKNOWLEDGED", "RESOLVED"},
    ("config_model_providers", "status"): {"ACTIVE", "SUSPENDED", "RETIRED"},
}
_BOOLEAN_FIELDS = {
    "enabled",
    "live_trading_allowed",
    "live",
    "local",
    "walk_forward_passed",
    "overfit_flag",
    "lookahead_flag",
    "tos_ok",
    "active",
    "free_tier",
    "free_only",
    "legal_confirmed",
    "handled",
    "blocks_live",
    "frozen",
    "matched",
    "quarantined",
}
REQUIRED_DEDUP_TABLES = {
    "agent_runs",
    "model_calls",
    "literature",
    "data_quality",
    "audit_events",
    "suggestions",
    "orders",
    "fills",
    "metrics",
    "cycle_stages",
    "heartbeats",
    "reconciliation_snapshots",
    "orderbook_snapshots",
}
_RISK_REASONS = {
    "",
    "A_SCHEMA", "A_DIRECTION", "A_QUANTITY", "A_NO_PRICE_LIMIT", "A_ORPHAN_STRATEGY", "A_FORBIDDEN_PRODUCER",
    "B_KILL_SWITCH", "B_VENUE_DISABLED", "B_FUTURES_DISABLED", "B_RECON_STALE", "B_BLOCKING_INCIDENT",
    "C_CASH_COVERAGE", "C_MAX_NOTIONAL", "D_SHORTING_DISABLED", "D_UNCOVERED_SHORT", "D_EXIT_EXCEEDS_POSITION",
    "D_LEVERAGE_DISABLED", "D_MARGIN_COVERAGE", "D_LEVERAGE_LIMIT", "E_STRATEGY_CAP", "E_PORTFOLIO_CAP",
    "E_INSTRUMENT_CAP", "E_MAX_POSITIONS", "F_NO_MARKET", "F_STALE_PRICE", "G_RATE_LIMIT", "G_VENUE_RATE_LIMIT", "G_STRATEGY_RATE_LIMIT",
    "H_INSTRUMENT_NOT_ALLOWED", "H_DAILY_LOSS_LIMIT", "H_DRAWDOWN_HALT", "H_REVIEW_EXPIRED",
}
_CONFIG_TYPES = {
    "live_trading_enabled": "bool",
    "bybit_testnet": "bool",
    "bybit_fx_rate": "float",
    "bybit_futures_enabled": "bool",
    "shorting_allowed": "bool",
    "leverage_allowed": "bool",
    "max_leverage": "float",
    "max_daily_loss_pct": "float",
    "max_drawdown_halt_pct": "float",
    "max_order_notional_cents": "int",
    "strategy_cap_cents": "int",
    "portfolio_cap_cents": "int",
    "instrument_cap_cents": "int",
    "max_open_positions": "int",
    "cash_buffer_pct": "float",
    "max_orders_per_instrument_day": "int",
    "max_orders_per_venue_day": "int",
    "min_seconds_between_live_orders_per_symbol": "int",
    "recon_max_age_s": "int",
    "paper_slippage_bps": "int",
    "paper_quote_max_age_s": "int",
    "min_position_notional_cents": "int",
    "evidence_bar_paper_days": "int",
    "evidence_bar_min_trades": "int",
    "evidence_bar_min_sharpe": "float",
    "evidence_bar_max_dd_pct": "float",
    "evidence_bar_net_of_costs": "bool",
    "model_monthly_budget_cents": "int",
    "research_monthly_budget_cents": "int",
    "trading_monthly_budget_cents": "int",
    "ops_monthly_budget_cents": "int",
    "log_level": "str",
    "tax_output_dir": "str",
    # -- operationally self-tunable (see core/self_tuning.py) ----------------
    # How fast and how much the company runs.  These carry an objective
    # feedback signal -- cycle duration, queue depth, the observed spread
    # distribution -- so the company can measure whether its own setting is
    # wrong and correct it.  Deliberately NOT the evidence bar or the risk
    # caps: a company that may lower its own standard of proof until
    # everything passes is not learning, and one that may raise its own loss
    # limit has no limit.
    "agent_workers": "int",
    "cycle_interval_standard_s": "int",
    # How the company spends its risk budget, never how large that budget is:
    # every size this produces is still clamped by the per-order, per-instrument,
    # per-strategy and portfolio caps above, which stay frozen.
    "position_risk_bps": "float",
    "max_spread_bps": "float",
    "min_book_depth_multiple": "float",
    "orchestrator_max_calls_per_day": "int",
    "ui_token": "str",
    "model_status": "json",
    "model_fallback_chain": "json",
    "self_heal_code_patches": "bool",
    # Written by AgentManager._suspend_model whenever a model is parked (rate
    # limit, or an endpoint that refused the connection).  It was missing from
    # this map, so the suspension path raised "unknown config_general key"
    # *inside dispatch* — the quota handling could never actually run.
    "model_suspensions": "json",
    "paper_promotion_mode": "str",
    "market_universe_json": "json",
}


class Repository:
    """The sole database owner.

    SQL identifiers are resolved from the SQLite schema before interpolation;
    values are always bound parameters.  ``conn`` is a read-only compatibility
    facade; application writes go through this class and its writer policy.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._conn = sqlite3.connect(str(path), timeout=5, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._write_mutex = threading.RLock()
        self._lock_handle = None
        self._lock_depth = 0
        self.conn = _ReadOnlyConnection(self)
        with self._write_scope():
            migrate(self._conn)
            self._ensure_archive_structures()

    @contextmanager
    def _write_scope(self) -> Iterator[None]:
        """Serialize writers in-process and, where available, across processes.

        The OS lock is taken on ONE persistent file descriptor and guarded by a
        depth counter, so nested scopes (e.g. ``put`` inside ``transaction``)
        neither re-block on themselves nor release the lock early.
        """
        with self._write_mutex:
            acquired = False
            try:
                if self.path.name != ":memory:":
                    try:
                        import fcntl

                        if self._lock_handle is None:
                            lock_path = self.path.with_name(self.path.name + ".writer.lock")
                            self._lock_handle = lock_path.open("a+")
                        if self._lock_depth == 0:
                            fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_EX)
                            acquired = True
                    except (ImportError, OSError):
                        acquired = False
                self._lock_depth += 1
                yield
            finally:
                self._lock_depth -= 1
                if acquired:
                    try:
                        import fcntl

                        fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_UN)
                    except (ImportError, OSError):
                        pass

    @contextmanager
    def _read_scope(self) -> Iterator[None]:
        """Serialize connection use in-process for readers.

        The runtime shares ONE Repository (one sqlite3 connection,
        ``check_same_thread=False``) between the conductor thread and the
        background agent-worker thread.  Unsynchronized concurrent
        ``execute``/``fetch`` on that connection intermittently surfaced as
        ``IndexError: tuple index out of range`` inside schema reads,
        crashing VALIDATE cycles.  This scope holds only the in-process
        RLock (re-entrant, no OS lock): writers already hold the same mutex
        via :meth:`_write_scope`, so every connection use is mutually
        exclusive in-process.  Cross-process readers (the UI) keep their own
        connections and are unaffected.
        """
        with self._write_mutex:
            yield

    def close(self) -> None:
        """Close the underlying connection (restore tooling quiesces writers)."""
        self._conn.close()
    def reopen(self) -> None:
        """Re-open after a database file swap (restore)."""
        self._conn = sqlite3.connect(str(self.path), timeout=5, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._write_scope():
            migrate(self._conn)

    def run_migrations(self) -> None:
        """Bring the schema to code level (idempotent).

        Used by the ops self-healer to repair schema drift (``unknown
        column`` / ``unknown table``) without touching data.  Migrations
        only ever ADD columns/tables/views, never drop evidence.
        """
        with self._write_scope():
            migrate(self._conn)
            self._ensure_archive_structures()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Atomic multi-row writes: all ``put``s inside commit or none do.

        Used by the Agent Manager so a run's output rows ingest atomically —
        partial outputs of a failed ingestion leave no trace (02 §5.5).
        ``put``/``update_where`` issued inside participate in this transaction
        because they execute on the same connection.
        """
        with self._write_scope():
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def _writer(self, table: str, writer: str | object) -> str:
        if writer is _DEFAULT_WRITER:
            return "system"
        return str(writer)

    def _table_info(self, table: str) -> list[sqlite3.Row]:
        if not isinstance(table, str) or not table or not table.replace("_", "").isalnum():
            raise ValueError("invalid table name")
        with self._read_scope():
            known = self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            if table not in {row[0] for row in known}:
                raise ValueError("unknown table")
            return list(self._conn.execute(f'PRAGMA table_info("{table}")'))

    def columns(self, table: str) -> set[str]:
        """Column names of a table (read-only introspection for validators)."""
        return self._column_set(self._table_info(table))

    @staticmethod
    def _column_set(info: list[sqlite3.Row]) -> set[str]:
        return {str(row[1]) for row in info}

    def _validate_columns(self, table: str, columns: Any, info: list[sqlite3.Row] | None = None) -> None:
        info = info or self._table_info(table)
        available = self._column_set(info)
        for column in columns:
            if not isinstance(column, str) or not column.replace("_", "").isalnum() or column not in available:
                raise ValueError(f"unknown column for {table}: {column}")

    def _authorize(self, table: str, writer: str) -> None:
        if table.startswith("archive_"):
            raise PermissionError("archive tables are managed by the archival job")
        if writer in {"operator", "ui", "human:ui"}:
            if table not in _OPERATOR_TABLES:
                raise PermissionError("writer is not allowed to modify this table")
            return
        if writer == "model_discovery":
            if table not in {"config_models", "config_model_providers"}:
                raise PermissionError("model discovery writer is restricted to model configuration")
            return
        if writer == "source_validator":
            # The host allowlist was operator-owned, which made it the one gate
            # no amount of agent work could pass: 90 proposed sources were
            # deferred "pending human review" 3079 times and never used.  The
            # validator earns the entry with a probe (DNS, TLS, robots, no
            # redirect elsewhere) and writes it under its own name, so the row
            # says who vouched for the host and on what evidence.  Narrow on
            # purpose: this writer can grant a host and nothing else -- it still
            # cannot activate a source, which remains the registry's alone.
            if table != "config_allowlist":
                raise PermissionError("source validator writer is restricted to the host allowlist")
            return
        if writer.startswith("agent:"):
            class_id = writer.split(":", 1)[1]
            row = self.find_one("agent_classes", id=class_id)
            if not row:
                raise PermissionError("unknown agent writer")
            allowed = _db_json(row.get("allowed_tables"), [])
            if table not in allowed or table not in _AGENT_ONLY_TABLES or table in _PROTECTED_AGENT_TABLES:
                raise PermissionError("agent write-set denied")
            return
        if writer in {"trade_decider", "paper_trader"}:
            if table != "suggestions":
                raise PermissionError("agent write-set denied")
            return
        if writer in {"system", "core", "gateway", "reconciler", "archiver", "source_registry"}:
            if table == "config_models":
                # Runtime status flips are system-writable per 07 §1; the
                # status-only column restriction is enforced in _validate.
                return
            if table in _OPERATOR_TABLES:
                raise PermissionError("operator-owned configuration is not system writable")
            return
        raise PermissionError("unknown writer")

    def _validate(self, table: str, row: dict[str, Any], writer: str, *, require_dedup: bool = True,
                  partial: bool = False) -> None:
        self._authorize(table, writer)
        if table == "config_models" and writer == "model_discovery":
            if "free_tier" in row and row.get("free_tier") not in (1, True):
                raise PermissionError("model discovery may register free-tier models only")
            allowed = {
                "id", "provider", "provider_config_id", "model_name", "base_url", "api_key_env",
                "credentials_key", "cost_in_per_1M", "cost_out_per_1M", "local", "free_tier",
                "context_window", "capability_ratings_json", "status", "cooldown_until", "last_error",
                "discovered_at",
            }
            if set(row) - allowed:
                raise PermissionError("model discovery supplied an unsupported model field")
        elif table == "config_models" and writer not in {"operator", "ui", "human:ui"}:
            # 07 §1 mandates system-driven SUSPENDED/QUOTA/ACTIVE flips, but the
            # model *definitions* (pricing, ratings, key indirection) stay
            # operator-owned: deterministic services may touch status only.
            if set(row) - _MODEL_DISCOVERY_RUNTIME_FIELDS:
                raise PermissionError("only config_models runtime fields are system writable")
        if table == "backtests" and writer.startswith("agent:"):
            # A request may name itself RUNNING and nothing else; any other
            # status, and every measured field, is the backtester's to write.
            extra = set(row) - _BACKTEST_REQUEST_FIELDS - {"status"}
            if extra:
                raise PermissionError(
                    "an agent requests a backtest, it does not report one: "
                    f"{sorted(extra)}")
            if "status" in row and row.get("status") != "RUNNING":
                raise PermissionError(
                    "only the backtester may set a backtest status other than RUNNING")
        info = self._table_info(table)
        self._validate_columns(table, row, info)

        for field in _MONEY_FIELDS.get(table, set()):
            value = row.get(field)
            if value is not None and (isinstance(value, bool) or not isinstance(value, Integral)):
                raise ValueError(f"{field} must be integer cents")
        for field in _BOOLEAN_FIELDS:
            if field in row and row[field] is not None and row[field] not in (0, 1, False, True):
                raise ValueError(f"{field} must be 0 or 1")
        for (enum_table, field), values in _ENUMS.items():
            if enum_table == table and field in row and row[field] is not None and row[field] not in values:
                raise sqlite3.IntegrityError(f"invalid {table}.{field}")
        if require_dedup and table in REQUIRED_DEDUP_TABLES and not row.get("dedup_key"):
            raise ValueError(f"{table}.dedup_key is required")
        if table == "strategy_ideas" and (not partial or "evidence_refs" in row) and not _nonempty_json(row.get("evidence_refs")):
            raise ValueError("evidence_refs required")
        if table == "data_sources":
            # M7: agents propose sources; the deterministic Source Registry
            # alone flips PROPOSED -> ACTIVE.
            status = row.get("status")
            if writer.startswith("agent:"):
                if status not in (None, "PROPOSED"):
                    raise PermissionError("agents may only propose data sources (status PROPOSED)")
                if status is None:
                    row["status"] = "PROPOSED"
            if status == "ACTIVE" and writer != "source_registry":
                raise PermissionError("only the Source Registry may activate data sources")
        if table == "strategy_ideas" and row.get("fingerprint") and row.get("status") != "DEAD":
            # 08 §5: a re-proposal whose fingerprint equals a DEAD idea's is
            # rejected at ingestion (reason FINGERPRINT_BLACKLISTED).
            fingerprint = row["fingerprint"]
            dead_match = next(
                (other for other in self.filter("strategy_ideas", status="DEAD")
                 if other.get("fingerprint") == fingerprint and other.get("id") != row.get("id")),
                None,
            )
            if dead_match:
                raise ValueError(f"FINGERPRINT_BLACKLISTED: fingerprint {fingerprint} matches DEAD idea {dead_match['id']}")
        if table == "improvement_log" and (not partial or "evidence_refs" in row) and row.get("kind") == "plan" \
                and row.get("action") != "hold" and not _nonempty_json(row.get("evidence_refs")):
            raise ValueError("evidence_refs required")
        if table == "risk_decisions" and "reason_code" in row and row.get("reason_code") not in _RISK_REASONS:
            raise sqlite3.IntegrityError("invalid risk decision reason code")
        if table == "config_keys":
            encrypted = row.get("encrypted_value")
            if encrypted is not None and not str(encrypted).startswith("gAAAA"):
                raise ValueError("config_keys.encrypted_value must be a Fernet token")
        if table == "config_general" and "key" in row:
            key = str(row["key"])
            if key not in _CONFIG_TYPES:
                raise ValueError("unknown config_general key")
            if "value" in row:
                _validate_config_value(_CONFIG_TYPES[key], row["value"])
        if table == "config_model_providers":
            from urllib.parse import urlparse

            # A provider running on this machine (Ollama, LM Studio, vLLM) is
            # reached over plain HTTP on loopback.  That traffic never leaves
            # the host, so requiring https there would only rule out the one
            # configuration that sends no data anywhere.  Everything else still
            # has to be https.
            local_hosts = {"127.0.0.1", "localhost", "::1"}
            for field in ("base_url", "catalog_url", "terms_url"):
                value = row.get(field)
                if value is None:
                    continue
                parsed = urlparse(str(value))
                if parsed.scheme == "https" and parsed.netloc:
                    continue
                if (field != "terms_url" and parsed.scheme == "http"
                        and parsed.hostname in local_hosts):
                    continue
                raise ValueError(f"config_model_providers.{field} must be an https URL")
            if writer == "model_discovery" and set(row) - _MODEL_DISCOVERY_RUNTIME_FIELDS:
                raise PermissionError("model discovery may update provider runtime metadata only")
        if table == "suggestions" and writer in {"paper_trader", "agent:paper_trader"} and row.get("live"):
            raise PermissionError("paper_trader cannot produce live suggestions")
        if table in {"ledger", "audit_events"} and writer.startswith("agent:"):
            raise PermissionError("append-only core table is not agent writable")
        if table == "orders":
            if row.get("live"):
                raise PermissionError("live order persistence is disabled in M3")
            decision_id = row.get("risk_decision_id")
            if decision_id:
                decision = self.find_one("risk_decisions", id=decision_id)
                if not decision or decision.get("decision") != "APPROVED":
                    raise ValueError("order requires an approved risk decision")
                if row.get("suggestion_id") and decision.get("suggestion_id") != row.get("suggestion_id"):
                    raise ValueError("order and risk decision suggestions do not match")

    def put(self, table: str, row: dict[str, Any], *, writer: str | object = _DEFAULT_WRITER) -> str:
        info = self._table_info(table)
        values = dict(row)
        values.setdefault("id", uuid.uuid4().hex)
        columns = self._column_set(info)
        if "id" not in columns:
            values.pop("id", None)
        writer_name = self._writer(table, writer)
        self._validate(table, values, writer_name)
        if not values:
            raise ValueError("row must contain at least one column")
        self._validate_columns(table, values, info)
        names = list(values)
        quoted_names = ",".join('"' + name + '"' for name in names)
        placeholders = ",".join("?" for _ in names)
        sql = f'INSERT INTO "{table}" ({quoted_names}) VALUES ({placeholders})'
        with self._write_scope():
            self._conn.execute(sql, [_db_value(values[name]) for name in names])
        primary_key = next((str(r[1]) for r in info if r[5]), "id")
        return str(values.get("id", values.get(primary_key, "")))

    def get(self, table: str, key: Any) -> dict[str, Any] | None:
        info = self._table_info(table)
        key_column = "id" if any(row[1] == "id" for row in info) else next((row[1] for row in info if row[5]), "id")
        self._validate_columns(table, [key_column], info)
        with self._read_scope():
            row = self._conn.execute(f'SELECT * FROM "{table}" WHERE "{key_column}"=?', (key,)).fetchone()
        return dict(row) if row else None

    def filter(self, table: str, **where: Any) -> list[dict[str, Any]]:
        info = self._table_info(table)
        self._validate_columns(table, where, info)
        sql = f'SELECT * FROM "{table}"'
        args: list[Any] = []
        if where:
            sql += " WHERE " + " AND ".join(f'"{key}"=?' for key in where)
            args = [_db_value(value) for value in where.values()]
        with self._read_scope():
            return [dict(row) for row in self._conn.execute(sql, args)]

    def find_one(self, table: str, **where: Any) -> dict[str, Any] | None:
        rows = self.filter(table, **where)
        return rows[0] if rows else None

    def upsert(
        self,
        table: str,
        row: dict[str, Any],
        *,
        conflict_columns: tuple[str, ...] = ("id",),
        writer: str | object = _DEFAULT_WRITER,
    ) -> str:
        values = dict(row)
        info = self._table_info(table)
        self._validate_columns(table, conflict_columns, info)
        lookup = {key: values[key] for key in conflict_columns if key in values}
        existing = self.find_one(table, **lookup) if lookup else None
        if existing:
            changes = {key: value for key, value in values.items() if key not in conflict_columns and key != "id"}
            self.update_where(table, lookup, changes, writer=writer)
            return str(existing.get("id", next((existing[key] for key in conflict_columns if key in existing), "")))
        return self.put(table, values, writer=writer)

    def update_where(
        self,
        table: str,
        where: dict[str, Any],
        changes: dict[str, Any],
        *,
        writer: str | object = _DEFAULT_WRITER,
    ) -> None:
        info = self._table_info(table)
        if not where or not changes:
            return
        self._validate_columns(table, where, info)
        self._validate_columns(table, changes, info)
        writer_name = self._writer(table, writer)
        self._validate(table, changes, writer_name, require_dedup=False, partial=True)
        if table == "orders":
            existing = self.find_one(table, **where)
            if existing:
                self._validate(table, {**existing, **changes}, writer_name, require_dedup=False)
        if table == "config_general" and "value" in changes:
            existing = self.find_one(table, **where)
            if existing and existing.get("key") in _CONFIG_TYPES:
                _validate_config_value(_CONFIG_TYPES[existing["key"]], changes["value"])
        if table == "config_keys" and "encrypted_value" in changes and not str(changes["encrypted_value"]).startswith("gAAAA"):
            raise ValueError("config_keys.encrypted_value must be a Fernet token")
        if table in {"ledger", "audit_events"}:
            raise PermissionError(f"{table} is append-only")
        assignments = ",".join(f'"{key}"=?' for key in changes)
        predicates = " AND ".join(f'"{key}"=?' for key in where)
        with self._write_scope():
            self._conn.execute(
                f'UPDATE "{table}" SET {assignments} WHERE {predicates}',
                [_db_value(value) for value in changes.values()] + [_db_value(value) for value in where.values()],
            )

    def update(
        self,
        table: str,
        key: Any,
        changes: dict[str, Any],
        *,
        writer: str | object = _DEFAULT_WRITER,
    ) -> None:
        info = self._table_info(table)
        key_column = "id" if any(row[1] == "id" for row in info) else next((row[1] for row in info if row[5]), "id")
        self.update_where(table, {key_column: key}, changes, writer=writer)

    def delete_where(
        self,
        table: str,
        where: dict[str, Any],
        *,
        writer: str | object = _DEFAULT_WRITER,
    ) -> int:
        """Delete operational rows only; financial evidence is immutable."""
        if table not in {"heartbeats", "degraded_states", "config_allowlist", "config_models"}:
            raise PermissionError("deletion is not allowed for this table")
        info = self._table_info(table)
        if not where:
            raise ValueError("a deletion predicate is required")
        self._validate_columns(table, where, info)
        writer_name = self._writer(table, writer)
        self._authorize(table, writer_name)
        predicates = " AND ".join(f'"{key}"=?' for key in where)
        with self._write_scope():
            cursor = self._conn.execute(
                f'DELETE FROM "{table}" WHERE {predicates}',
                [_db_value(value) for value in where.values()],
            )
        return int(cursor.rowcount)

    def set_venue_freeze(self, venue: str, frozen: bool, reason: str = "", ts: str | None = None) -> None:
        ts = ts or _utc_now()
        self._authorize("venue_freezes", "reconciler")
        with self._write_scope():
            self._conn.execute(
                "INSERT INTO venue_freezes(venue,frozen,reason,updated_at,reconciled_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(venue) DO UPDATE SET frozen=excluded.frozen, reason=excluded.reason, "
                "updated_at=excluded.updated_at, reconciled_at=excluded.reconciled_at",
                (venue, int(frozen), reason, ts, ts if not frozen else None),
            )

    def venue_frozen(self, venue: str) -> bool:
        with self._read_scope():
            row = self._conn.execute("SELECT frozen FROM venue_freezes WHERE venue=?", (venue,)).fetchone()
        return bool(row and row[0])

    def backup(self, path: str | Path) -> Path:
        """Write a consistent snapshot of the whole database.

        Uses SQLite's online backup API so callers (the operator UI) never
        touch the database file directly, and serializes against writers so
        the snapshot is a valid, restorable state.
        """
        target_path = Path(path)
        target = sqlite3.connect(str(target_path))
        try:
            with self._write_scope():
                self._conn.backup(target)
        finally:
            target.close()
        return target_path

    def integrity_check(self) -> tuple[bool, str]:
        """Run ``PRAGMA integrity_check`` on the live database file.

        The check opens the file itself (like the backup/restore tooling
        does), so it also reports corruption in pages that the current
        connection has not touched.  Returns ``(ok, message)``.
        """
        check_conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        try:
            rows = check_conn.execute("PRAGMA integrity_check").fetchall()
        except sqlite3.DatabaseError as exc:
            return False, f"integrity check failed: {exc}"
        finally:
            check_conn.close()
        problems = [str(row[0]) for row in rows if str(row[0]) != "ok"]
        if not problems:
            return True, "ok"
        return False, "; ".join(problems)

    @staticmethod
    def verify_file_integrity(path: str | Path) -> tuple[bool, str]:
        """Integrity check for an arbitrary (e.g. backup) database file."""
        target = Path(path)
        if not target.exists():
            return False, f"file not found: {target}"
        check_conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
        try:
            rows = check_conn.execute("PRAGMA integrity_check").fetchall()
        except sqlite3.DatabaseError as exc:
            return False, f"integrity check failed: {exc}"
        finally:
            check_conn.close()
        problems = [str(row[0]) for row in rows if str(row[0]) != "ok"]
        if not problems:
            return True, "ok"
        return False, "; ".join(problems)

    def record_reconciliation(
        self,
        *,
        venue: str,
        ts: str,
        balances_json: object,
        positions_json: object,
        orders_json: object,
        matched: bool,
        mismatch_json: object,
    ) -> str:
        existing = self.filter("reconciliation_snapshots", venue=venue, ts=ts)
        dedup_key = f"recon:{venue}:{ts}:{len(existing) + 1}"
        return self.put(
            "reconciliation_snapshots",
            {
                "venue": venue,
                "ts": ts,
                "balances_json": balances_json,
                "positions_json": positions_json,
                "orders_json": orders_json,
                "matched": int(matched),
                "mismatch_json": mismatch_json,
                "dedup_key": dedup_key,
            },
            writer="reconciler",
        )

    def cycle_stage(self, cycle_id: str, stage: str, details: dict | None = None) -> bool:
        ts = _utc_now()
        dedup = f"cycle:{cycle_id}:stage:{stage}"
        try:
            self.put(
                "cycle_stages",
                {
                    "id": uuid.uuid4().hex,
                    "cycle_id": cycle_id,
                    "stage": stage,
                    "dedup_key": dedup,
                    "status": "COMMITTED",
                    "committed_at": ts,
                    "details_json": details or {},
                },
                writer="system",
            )
        except sqlite3.IntegrityError:
            return False
        return True

    def heartbeat(self, component: str, cycle_id: str | None = None, ts: str | None = None) -> str:
        ts = ts or _utc_now()
        return self.put(
            "heartbeats",
            {"component": component, "cycle_id": cycle_id, "ts": ts, "dedup_key": f"heartbeat:{component}:{ts}"},
            writer="system",
        )

    def set_degraded(self, name: str, active: bool, reason: str = "", ts: str | None = None) -> None:
        if name not in {"degraded_model", "exchange_hold", "cost_hold", "disk_low", "recon_freeze"}:
            raise ValueError("unknown degraded state")
        ts = ts or _utc_now()
        with self._read_scope():
            old = self._conn.execute("SELECT active, entered_at FROM degraded_states WHERE name=?", (name,)).fetchone()
        entered = old[1] if old and old[0] else (ts if active else None)
        cleared = ts if not active else None
        with self._write_scope():
            self._conn.execute(
                "INSERT INTO degraded_states(name,active,reason,entered_at,cleared_at,updated_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET active=excluded.active, reason=excluded.reason, "
                "entered_at=excluded.entered_at, cleared_at=excluded.cleared_at, updated_at=excluded.updated_at",
                (name, int(active), reason, entered, cleared, ts),
            )

    def active_degraded(self) -> set[str]:
        with self._read_scope():
            return {row[0] for row in self._conn.execute("SELECT name FROM degraded_states WHERE active=1")}

    def latest_heartbeat(self, component: str) -> str | None:
        with self._read_scope():
            row = self._conn.execute(
                "SELECT ts FROM heartbeats WHERE component=? ORDER BY ts DESC LIMIT 1", (component,)
            ).fetchone()
        return row[0] if row else None

    def tables(self) -> set[str]:
        with self._read_scope():
            return {
                str(row[0])
                for row in self._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }

    def _ensure_archive_structures(self) -> None:
        for table in ("agent_runs", "model_calls", "literature", "data_quality", "audit_events"):
            info = self._table_info(table)
            columns = [str(row[1]) for row in info]
            quoted = ",".join(f'"{column}"' for column in columns)
            archive = f"archive_{table}"
            self._conn.execute(f'CREATE TABLE IF NOT EXISTS "{archive}" AS SELECT {quoted} FROM "{table}" WHERE 0')
            self._conn.execute(
                f'CREATE VIEW IF NOT EXISTS "{table}_archive_view" AS '
                f'SELECT {quoted} FROM "{table}" UNION ALL SELECT {quoted} FROM "{archive}"'
            )

    def archive_old(self, cutoff: datetime, *, run_month: str | None = None) -> None:
        """Archive hot rows exactly once, then compact the hot database."""
        hot = ("model_calls", "data_quality", "literature", "audit_events", "agent_runs")
        stamp = cutoff.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        with self._write_scope():
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if run_month and self._conn.execute("SELECT 1 FROM archival_runs WHERE month=?", (run_month,)).fetchone():
                    self._conn.commit()
                    return
                for table in hot:
                    info = self._table_info(table)
                    columns = [str(row[1]) for row in info]
                    quoted = ",".join(f'"{column}"' for column in columns)
                    archive = f"archive_{table}"
                    self._conn.execute(f'CREATE TABLE IF NOT EXISTS "{archive}" AS SELECT {quoted} FROM "{table}" WHERE 0')
                    dependency_guard = ""
                    if table == "agent_runs":
                        dependency_guard = (
                            ' AND NOT EXISTS (SELECT 1 FROM "suggestions" WHERE "suggestions"."agent_run_id"="source"."id")'
                            ' AND NOT EXISTS (SELECT 1 FROM "model_calls" WHERE "model_calls"."agent_run_id"="source"."id")'
                            ' AND NOT EXISTS (SELECT 1 FROM "data_quality" WHERE "data_quality"."agent_run_id"="source"."id")'
                        )
                    elif table == "model_calls":
                        dependency_guard = ' AND NOT EXISTS (SELECT 1 FROM "model_calls" child WHERE child."retry_of"="source"."id" AND child."created_at" >= ?)' 
                    elif table == "literature":
                        dependency_guard = ' AND NOT EXISTS (SELECT 1 FROM "literature" child WHERE child."superseded_by"="source"."id" AND child."created_at" >= ?)'
                    insert_params = (stamp, stamp) if table in {"model_calls", "literature"} else (stamp,)
                    self._conn.execute(
                        f'INSERT INTO "{archive}" ({quoted}) SELECT {quoted} FROM "{table}" source '
                        f'WHERE source."created_at" < ? AND NOT EXISTS '
                        f'(SELECT 1 FROM "{archive}" archived WHERE archived."id"=source."id"){dependency_guard}',
                        insert_params,
                    )
                    if table == "agent_runs":
                        dependency_guard = (
                            ' AND NOT EXISTS (SELECT 1 FROM "suggestions" WHERE "suggestions"."agent_run_id"="agent_runs"."id")'
                            ' AND NOT EXISTS (SELECT 1 FROM "model_calls" WHERE "model_calls"."agent_run_id"="agent_runs"."id")'
                            ' AND NOT EXISTS (SELECT 1 FROM "data_quality" WHERE "data_quality"."agent_run_id"="agent_runs"."id")'
                        )
                    elif table == "model_calls":
                        dependency_guard = ' AND NOT EXISTS (SELECT 1 FROM "model_calls" child WHERE child."retry_of"="model_calls"."id" AND child."created_at" >= ?)'
                    elif table == "literature":
                        dependency_guard = ' AND NOT EXISTS (SELECT 1 FROM "literature" child WHERE child."superseded_by"="literature"."id" AND child."created_at" >= ?)'
                    delete_params = (stamp, stamp) if table in {"model_calls", "literature"} else (stamp,)
                    self._conn.execute(
                        f'DELETE FROM "{table}" WHERE "created_at" < ? AND "id" IN '
                        f'(SELECT "id" FROM "{archive}"){dependency_guard}',
                        delete_params,
                    )
                    self._conn.execute(
                        f'CREATE VIEW IF NOT EXISTS "{table}_archive_view" AS '
                        f'SELECT {quoted} FROM "{table}" UNION ALL SELECT {quoted} FROM "{archive}"'
                    )
                if run_month:
                    self._conn.execute(
                        "INSERT INTO archival_runs(month, archived_at) VALUES(?, ?)",
                        (run_month, _utc_now()),
                    )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        self._conn.execute("VACUUM")


def _db_json(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _nonempty_json(value: Any) -> bool:
    parsed = _db_json(value, None) if isinstance(value, str) else value
    return isinstance(parsed, (list, dict)) and bool(parsed)


def _validate_config_value(kind: str, value: Any) -> None:
    if isinstance(value, str):
        stripped = value.strip()
        if kind == "json":
            try:
                json.loads(stripped)
            except (TypeError, ValueError) as exc:
                raise ValueError("configuration json must be valid json") from exc
            return
        if kind == "str":
            if not stripped:
                raise ValueError("configuration string must not be empty")
            return
        if kind == "bool":
            if stripped.lower() not in {"0", "1", "true", "false", "yes", "no", "on", "off", "enabled", "disabled"}:
                raise ValueError("configuration boolean must be explicit")
            return
        try:
            value = int(stripped) if kind == "int" else float(stripped)
        except ValueError:
            raise ValueError("invalid numeric configuration") from None
    if kind == "bool":
        if not (isinstance(value, bool) or (isinstance(value, int) and value in (0, 1))):
            raise ValueError("configuration boolean must be explicit")
    elif kind == "int":
        if isinstance(value, bool) or not isinstance(value, Integral):
            raise ValueError("configuration integer required")
    elif kind == "float":
        if isinstance(value, bool):
            raise ValueError("configuration number required")
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError("configuration number required") from None
        if number != number or number in (float("inf"), float("-inf")):
            raise ValueError("configuration number must be finite")
    elif kind == "str" and not isinstance(value, str):
        raise ValueError("configuration string required")
    elif kind == "json":
        if not isinstance(value, str):
            raise ValueError("configuration json must be a string")
        try:
            json.loads(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("configuration json must be valid json") from exc


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _db_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"), default=str)
    if isinstance(value, Decimal):
        return float(value)
    return value


class _ReadOnlyConnection:
    """Small compatibility facade that cannot bypass Repository write policy."""

    def __init__(self, repo: Repository):
        self._repo = repo

    def execute(self, sql: str, parameters: Any = ()):
        statement = str(sql).lstrip().upper()
        if statement.startswith("PRAGMA") and "=" in statement:
            raise PermissionError("raw database writes are not allowed; use Repository")
        if not (statement.startswith("SELECT") or statement.startswith("PRAGMA") or statement.startswith("EXPLAIN")):
            raise PermissionError("raw database writes are not allowed; use Repository")
        return self._repo._conn.execute(sql, parameters)

    def __getattr__(self, name: str):
        raise AttributeError(f"raw connection operation is unavailable: {name}")
