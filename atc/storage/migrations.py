from pathlib import Path
import sqlite3
from datetime import datetime, timezone

_V_IS_IMPROVING_SQL = """
CREATE VIEW v_is_improving AS
    WITH ordered_metrics AS (
        SELECT net_pnl_month_cents,
               row_number() OVER (ORDER BY day, COALESCE(hour, ''), id) AS position,
               count(*) OVER () AS sample_count
        FROM metrics
    ), pnl_bounds AS (
        SELECT max(CASE WHEN position = 1 THEN net_pnl_month_cents END) AS first_net_pnl_cents,
               max(CASE WHEN position = sample_count THEN net_pnl_month_cents END) AS last_net_pnl_cents,
               coalesce(max(sample_count), 0) AS metric_count
        FROM ordered_metrics
    ), closed_plans AS (
        SELECT improvement_log.id,
               cycles.created_at,
               CASE WHEN json_extract(improvement_log.actual_outcome, '$.met_expected') IN (1, 'true')
                    THEN 1 ELSE 0 END AS met,
               CASE WHEN json_extract(improvement_log.actual_outcome, '$.met_expected') IS NULL
                    THEN 1 ELSE 0 END AS unknown
        FROM improvement_log
        LEFT JOIN cycles ON cycles.id = improvement_log.cycle_id
        WHERE improvement_log.kind = 'plan'
          AND improvement_log.status = 'CLOSED'
          AND improvement_log.actual_outcome IS NOT NULL
    ), ordered_plans AS (
        SELECT met, unknown,
               row_number() OVER (ORDER BY created_at, id) AS position,
               count(*) OVER () AS plan_count
        FROM closed_plans
    ), plan_bounds AS (
        SELECT coalesce(sum(met), 0) AS met_count,
               coalesce(sum(1 - met - unknown), 0) AS unmet_count,
               coalesce(max(plan_count), 0) AS closed_plan_count
        FROM ordered_plans
    ), bt AS (
        SELECT count(*) AS total,
               coalesce(sum(overfit_flag), 0) AS overfit,
               coalesce(sum(walk_forward_passed), 0) AS wf_passed,
               coalesce(sum(CASE WHEN status = 'PASSED' THEN 1 ELSE 0 END), 0) AS passed
        FROM backtests
    ), ideas AS (
        SELECT count(*) AS total,
               coalesce(sum(CASE WHEN critic_status = 'PASSED' THEN 1 ELSE 0 END), 0) AS passed
        FROM strategy_ideas
    )
    SELECT CASE WHEN p.metric_count >= 2
                     AND p.last_net_pnl_cents > p.first_net_pnl_cents
                     AND (pb.closed_plan_count < 2 OR pb.met_count > pb.unmet_count)
                THEN 1 ELSE 0 END AS improving,
           p.first_net_pnl_cents,
           p.last_net_pnl_cents,
           p.metric_count,
           pb.met_count,
           pb.unmet_count,
           pb.closed_plan_count,
           CASE WHEN bt.total > 0 THEN 1.0 * bt.overfit / bt.total ELSE 0.0 END AS overfit_share,
           CASE WHEN bt.total > 0 THEN 1.0 * bt.wf_passed / bt.total ELSE 0.0 END AS oos_pass_share,
           CASE WHEN ideas.total > 0 THEN 1.0 * ideas.passed / ideas.total ELSE 0.0 END AS critic_pass_share
    FROM pnl_bounds p, plan_bounds pb, bt, ideas
"""


def diagnostic_views_sql() -> str:
    """The /diagnostics views, kept in their own file so the SQL stays readable
    and a fresh install and a migration cannot drift apart."""
    return Path(__file__).with_name("diagnostics_views.sql").read_text(encoding="utf-8")


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')}


def migrate(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    version = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]
    if version < 1:
        conn.executescript(Path(__file__).with_name("schema.sql").read_text(encoding="utf-8"))
        conn.execute("INSERT INTO schema_migrations VALUES (1, strftime('%Y-%m-%dT%H:%M:%fZ','now'))")
    if version < 2:
        if "created_at" not in _columns(conn, "cycles"):
            conn.execute("ALTER TABLE cycles ADD COLUMN created_at TEXT NOT NULL DEFAULT '1970-01-01T00:00:00.000Z'")
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS cycle_stages (id TEXT PRIMARY KEY, cycle_id TEXT NOT NULL REFERENCES cycles(id), stage TEXT NOT NULL, dedup_key TEXT UNIQUE, status TEXT NOT NULL, committed_at TEXT NOT NULL, details_json TEXT);
        CREATE TABLE IF NOT EXISTS heartbeats (id TEXT PRIMARY KEY, component TEXT NOT NULL, cycle_id TEXT, ts TEXT NOT NULL, dedup_key TEXT UNIQUE);
        CREATE TABLE IF NOT EXISTS degraded_states (name TEXT PRIMARY KEY, active INTEGER NOT NULL, reason TEXT, entered_at TEXT, cleared_at TEXT, updated_at TEXT NOT NULL);
        """)
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        conn.execute("INSERT INTO schema_migrations VALUES (2, ?)", (ts,))
    if version < 3:
        # M3 adds durable reconciliation evidence and enough order metadata
        # for restart recovery.  The guards also upgrade databases created by
        # the pre-M3 schema without touching existing evidence.
        columns = {row[1] for row in conn.execute("PRAGMA table_info(orders)")}
        for name, definition in (
            ("venue", "TEXT"),
            ("instrument", "TEXT"),
            ("quantity", "REAL"),
            ("created_at", "TEXT"),
            ("updated_at", "TEXT"),
        ):
            if name not in columns:
                conn.execute(f"ALTER TABLE orders ADD COLUMN {name} {definition}")
        ledger_columns = {row[1] for row in conn.execute("PRAGMA table_info(ledger)")}
        for name, definition in (("venue", "TEXT"), ("instrument", "TEXT"), ("currency", "TEXT")):
            if name not in ledger_columns:
                conn.execute(f"ALTER TABLE ledger ADD COLUMN {name} {definition}")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS venue_freezes (
                venue TEXT PRIMARY KEY,
                frozen INTEGER NOT NULL,
                reason TEXT,
                updated_at TEXT NOT NULL,
                reconciled_at TEXT
            );
            CREATE TABLE IF NOT EXISTS reconciliation_snapshots (
                id TEXT PRIMARY KEY,
                venue TEXT NOT NULL,
                ts TEXT NOT NULL,
                balances_json TEXT,
                positions_json TEXT,
                orders_json TEXT,
                matched INTEGER NOT NULL,
                mismatch_json TEXT,
                dedup_key TEXT UNIQUE
            );
            """
        )
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        conn.execute("INSERT INTO schema_migrations VALUES (3, ?)", (ts,))
    if version < 4:
        # Risk evaluations became attempt-scoped so every evaluation remains
        # auditable.  ALTER is guarded because fresh installs already include
        # these columns in schema.sql.
        risk_columns = _columns(conn, "risk_decisions")
        if "attempt" not in risk_columns:
            conn.execute("ALTER TABLE risk_decisions ADD COLUMN attempt INTEGER NOT NULL DEFAULT 1")
        if "created_at" not in risk_columns:
            conn.execute("ALTER TABLE risk_decisions ADD COLUMN created_at TEXT NOT NULL DEFAULT '1970-01-01T00:00:00.000Z'")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_risk_decisions_suggestion_attempt ON risk_decisions(suggestion_id, attempt)")
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        conn.execute("INSERT INTO schema_migrations VALUES (4, ?)", (ts,))
    if version < 5:
        conn.execute("CREATE TABLE IF NOT EXISTS archival_runs (month TEXT PRIMARY KEY, archived_at TEXT NOT NULL)")
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        conn.execute("INSERT INTO schema_migrations VALUES (5, ?)", (ts,))
    if version < 6:
        order_columns = _columns(conn, "orders")
        if "direction" not in order_columns:
            conn.execute("ALTER TABLE orders ADD COLUMN direction TEXT")
        if "slippage_tolerance_bps" not in order_columns:
            conn.execute("ALTER TABLE orders ADD COLUMN slippage_tolerance_bps INTEGER")
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        conn.execute("INSERT INTO schema_migrations VALUES (6, ?)", (ts,))
    if version < 7:
        conn.execute("DROP VIEW IF EXISTS v_is_improving")
        conn.executescript(_V_IS_IMPROVING_SQL)
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        conn.execute("INSERT INTO schema_migrations VALUES (7, ?)", (ts,))
    if version < 8:
        # M6: a security-quarantined agent class must not respawn until review.
        class_columns = _columns(conn, "agent_classes")
        if "quarantined" not in class_columns:
            conn.execute(
                "ALTER TABLE agent_classes ADD COLUMN quarantined INTEGER NOT NULL DEFAULT 0 "
                "CHECK(quarantined IN (0,1))"
            )
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        conn.execute("INSERT INTO schema_migrations VALUES (8, ?)", (ts,))
    if version < 9:
        # M8 learning loop: strategy_ideas stores the canonical fingerprint
        # inputs of 08 §5 (family / indicators / params) and v_is_improving
        # exposes the 08 §6 trend KPIs.
        idea_columns = _columns(conn, "strategy_ideas")
        for name, definition in (
            ("family", "TEXT"),
            ("indicators_json", "TEXT"),
            ("params_json", "TEXT"),
        ):
            if name not in idea_columns:
                conn.execute(f"ALTER TABLE strategy_ideas ADD COLUMN {name} {definition}")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ideas_fingerprint ON strategy_ideas(fingerprint)")
        conn.execute("DROP VIEW IF EXISTS v_is_improving")
        conn.executescript(_V_IS_IMPROVING_SQL)
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        conn.execute("INSERT INTO schema_migrations VALUES (9, ?)", (ts,))
    if version < 10:
        # M10: the operator UI channels non-config requests (kill, pause,
        # flatten, source decisions) through operator_messages and owns
        # per-strategy switches in a dedicated config table.
        message_columns = _columns(conn, "operator_messages")
        if "payload_json" not in message_columns:
            conn.execute("ALTER TABLE operator_messages ADD COLUMN payload_json TEXT")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS config_strategies (
                id TEXT PRIMARY KEY,
                live_trading_allowed INTEGER NOT NULL DEFAULT 0 CHECK(live_trading_allowed IN (0,1)),
                frozen INTEGER NOT NULL DEFAULT 0 CHECK(frozen IN (0,1)),
                updated_by TEXT NOT NULL DEFAULT 'operator' CHECK(updated_by IN ('operator','system')),
                updated_at TEXT NOT NULL
            );
            """
        )
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        conn.execute("INSERT INTO schema_migrations VALUES (10, ?)", (ts,))
    if version < 11:
        # Backtest requests/results are replayed across restarts.  Older
        # databases had no idempotency column, so add a nullable unique column
        # to preserve existing evidence while allowing new rows to deduplicate.
        backtest_columns = _columns(conn, "backtests")
        if "dedup_key" not in backtest_columns:
            conn.execute("ALTER TABLE backtests ADD COLUMN dedup_key TEXT")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_backtests_dedup_key ON backtests(dedup_key) WHERE dedup_key IS NOT NULL")
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        conn.execute("INSERT INTO schema_migrations VALUES (11, ?)", (ts,))
    if version < 12:
        # M? — operator-editable host allowlist (B): the deterministic
        # SourceRegistry reads allowed hosts from this table instead of a
        # hard-coded dict.  The rows are seeded by bootstrap_repository.
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS config_allowlist (
                slug TEXT NOT NULL,
                host TEXT NOT NULL,
                added_by TEXT NOT NULL DEFAULT 'operator' CHECK(added_by IN ('operator','system')),
                note TEXT,
                PRIMARY KEY (slug, host)
            );
            """
        )
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        conn.execute("INSERT INTO schema_migrations VALUES (12, ?)", (ts,))
    if version < 13:
        # Feature-engineer output: derived datasets produced by transforming raw
        # or quarantined sources (cleaning / regime filtering / feature
        # derivation / ensembles).  Lets the company turn low-quality data into
        # tradeable features instead of being stuck in a DATA_QUALITY_FAIL loop.
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS feature_sets (
                id TEXT PRIMARY KEY,
                base_dataset_id TEXT REFERENCES datasets(id),
                name TEXT,
                transform_json TEXT NOT NULL,
                feature_columns_json TEXT,
                produced_dataset_id TEXT REFERENCES datasets(id),
                quality_before REAL CHECK(quality_before IS NULL OR quality_before BETWEEN 0 AND 1),
                quality_after REAL CHECK(quality_after IS NULL OR quality_after BETWEEN 0 AND 1),
                status TEXT CHECK(status IN ('PROPOSED','TESTING','ACTIVE','REJECTED')),
                evidence_ref TEXT,
                rationale TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_feature_sets_base ON feature_sets(base_dataset_id);
            CREATE INDEX IF NOT EXISTS idx_feature_sets_status ON feature_sets(status);
            """
        )
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        conn.execute("INSERT INTO schema_migrations VALUES (13, ?)", (ts,))
    if version < 14:
        # Backtest records carry a free-text failure reason so the company can learn
        # *why* a backtest was rejected (critic challenge / integrity floor) instead
        # of the conductor crashing on a write to a missing column.
        bt_columns = _columns(conn, "backtests")
        if "failure_reason" not in bt_columns:
            conn.execute("ALTER TABLE backtests ADD COLUMN failure_reason TEXT")
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        conn.execute("INSERT INTO schema_migrations VALUES (14, ?)", (ts,))
    if version < 15:
        # Model providers are operator-approved connection definitions.  Model
        # rows keep only references to these definitions and runtime cooldown /
        # discovery metadata; credentials remain in config_keys or the process
        # environment.
        model_columns = _columns(conn, "config_models")
        for name, definition in (
            ("provider_config_id", "TEXT"),
            ("model_name", "TEXT"),
            ("credentials_key", "TEXT"),
            ("free_tier", "INTEGER NOT NULL DEFAULT 0 CHECK(free_tier IN (0,1))"),
            ("cooldown_until", "TEXT"),
            ("last_error", "TEXT"),
            ("discovered_at", "TEXT"),
        ):
            if name not in model_columns:
                conn.execute(f"ALTER TABLE config_models ADD COLUMN {name} {definition}")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS config_model_providers (
                id TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                display_name TEXT,
                base_url TEXT NOT NULL,
                catalog_url TEXT,
                api_key_env TEXT,
                credentials_key TEXT,
                terms_url TEXT NOT NULL,
                legal_note TEXT NOT NULL,
                free_only INTEGER NOT NULL DEFAULT 1 CHECK(free_only IN (0,1)),
                legal_confirmed INTEGER NOT NULL DEFAULT 0 CHECK(legal_confirmed IN (0,1)),
                enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
                status TEXT NOT NULL DEFAULT 'SUSPENDED' CHECK(status IN ('ACTIVE','SUSPENDED','RETIRED')),
                last_discovered_at TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_model_providers_status ON config_model_providers(status, enabled);
            CREATE INDEX IF NOT EXISTS idx_models_provider ON config_models(provider, status);
            """
        )
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        conn.execute("INSERT INTO schema_migrations VALUES (15, ?)", (ts,))
    if version < 16:
        # The feature-transform path records its detail payload on the win
        # experience (runtime._apply_feature_sets).  Older databases lack the
        # column, which surfaced as CRITICAL CONDUCTOR_ERROR
        # "unknown column for experiences: payload_json".  Additive only.
        exp_columns = _columns(conn, "experiences")
        if "payload_json" not in exp_columns:
            conn.execute("ALTER TABLE experiences ADD COLUMN payload_json TEXT")
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        conn.execute("INSERT INTO schema_migrations VALUES (16, ?)", (ts,))
    if version < 17:
        # /diagnostics: windowed learning KPIs.  backtests had no created_at at
        # all, which is why every KPI in v_is_improving had to be cumulative —
        # there was no way to ask "what happened this week".  The column is
        # added nullable (SQLite cannot add a column with a non-constant
        # default); rows written before it simply fall outside every window.
        bt_columns = _columns(conn, "backtests")
        if "created_at" not in bt_columns:
            conn.execute("ALTER TABLE backtests ADD COLUMN created_at TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_backtests_created ON backtests(created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cycles_kind_created ON cycles(kind, created_at)")
        conn.executescript(diagnostic_views_sql())
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        conn.execute("INSERT INTO schema_migrations VALUES (17, ?)", (ts,))
    if version < 18:
        # The instrument catalogue: what a venue lists, cached for the
        # researcher to choose from.  It does not belong in config_general —
        # that is operator-owned and not system-writable, and recording a
        # fetched fact as operator configuration would misstate where it came
        # from.  Its own table, written by the ingestor, refreshed on a TTL.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS source_catalog (
                slug         TEXT PRIMARY KEY,
                symbols_json TEXT NOT NULL,
                fetched_at   TEXT NOT NULL
            )
        """)
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        conn.execute("INSERT INTO schema_migrations VALUES (18, ?)", (ts,))
    if version < 19:
        # The feature_engineer's output contract advertises expected_quality —
        # the quality it predicts the transform will produce — but the table had
        # no such column, so the schema validated and the write failed 18 times
        # with "unknown column for feature_sets".  The field is worth keeping
        # rather than dropping: held against quality_after, it says whether the
        # agent's predictions are worth anything.
        if "expected_quality" not in _columns(conn, "feature_sets"):
            conn.execute("ALTER TABLE feature_sets ADD COLUMN expected_quality REAL")
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        conn.execute("INSERT INTO schema_migrations VALUES (19, ?)", (ts,))
    if version < 20:
        # A paper trade did not record WHAT it held or HOW MUCH, so a strategy
        # could not be asked what it was holding and the answer was read off
        # the venue's position book instead.  That book has one net row per
        # instrument, shared by every strategy trading it: seventeen ETHUSDT
        # strategies each believed they held whatever any of them had opened,
        # and closed each other's positions seconds after they were opened.
        # An open lot belongs to one strategy; these two columns are what let
        # it say so, and let it survive a restart.
        pt_columns = _columns(conn, "paper_trades")
        if "instrument" not in pt_columns:
            conn.execute("ALTER TABLE paper_trades ADD COLUMN instrument TEXT")
        if "quantity" not in pt_columns:
            conn.execute("ALTER TABLE paper_trades ADD COLUMN quantity TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_paper_trades_open "
                     "ON paper_trades(strategy_id, instrument, closed)")
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        conn.execute("INSERT INTO schema_migrations VALUES (20, ?)", (ts,))
    if version < 21:
        # What a backtest actually measured: a rule, an instrument, a
        # timeframe.  The funnel deduplicates an IDEA by its prose identity,
        # so four strategies under four different ideas carried the identical
        # config on BTCUSDT 1h and came back with the identical result -- 37
        # trades, sharpe 0.323, four times.  One thing learned, four backtests
        # spent, and four passes counted.  This column is what lets the
        # question be recognised before it is asked again.
        if "experiment_key" not in _columns(conn, "backtests"):
            conn.execute("ALTER TABLE backtests ADD COLUMN experiment_key TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_backtests_experiment "
                     "ON backtests(experiment_key)")
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        conn.execute("INSERT INTO schema_migrations VALUES (21, ?)", (ts,))
    if version < 22:
        # The host allowlist is what the Source Registry consults before it
        # will activate a source, and its added_by column admitted only
        # 'operator' or 'system'.  That made a granted host indistinguishable
        # from a seeded one, and left no way for the validator to sign its own
        # evidence.  The table is a handful of rows, so it is rebuilt rather
        # than worked around; the CHECK is widened, not dropped -- an arbitrary
        # writer still cannot grant a host.
        conn.executescript(
            """
            DROP TABLE IF EXISTS config_allowlist_v22;
            CREATE TABLE IF NOT EXISTS config_allowlist_v22 (
                slug TEXT NOT NULL,
                host TEXT NOT NULL,
                added_by TEXT NOT NULL DEFAULT 'operator'
                    CHECK(added_by IN ('operator','system','source_validator')),
                note TEXT,
                PRIMARY KEY (slug, host)
            );
            INSERT OR IGNORE INTO config_allowlist_v22 (slug, host, added_by, note)
                SELECT slug, host, added_by, note FROM config_allowlist;
            DROP TABLE config_allowlist;
            ALTER TABLE config_allowlist_v22 RENAME TO config_allowlist;
            """
        )
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        conn.execute("INSERT INTO schema_migrations VALUES (22, ?)", (ts,))
    if version < 23:
        # L2 depth, recorded over time.  Bybit's REST book is a snapshot of
        # NOW: there is no historical endpoint, so the only way the company can
        # ever backtest a liquidity rule is to start keeping its own series.
        # The derived columns are what a decision reads; levels_json keeps a
        # truncated ladder so a later feature can be computed from the raw
        # shape rather than only from today's summary.
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS orderbook_snapshots (
                id TEXT PRIMARY KEY,
                instrument TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                mid_cents INTEGER,
                spread_bps REAL,
                bid_depth_cents INTEGER,
                ask_depth_cents INTEGER,
                imbalance REAL,
                levels_json TEXT,
                dedup_key TEXT UNIQUE,
                created_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_orderbook_instrument
                ON orderbook_snapshots(instrument, observed_at);
            """
        )
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        conn.execute("INSERT INTO schema_migrations VALUES (23, ?)", (ts,))
    if version < 24:
        # An experience could only be a win, a loss or an incident.  Loss
        # attribution needs a fourth: "this strategy lost, and the reason was
        # the market, not the rule".  Filing that as a loss would teach the
        # proposer to avoid a rule that did nothing wrong -- the precise
        # mistake attribution exists to prevent -- so the vocabulary has to
        # carry the distinction the analysis draws.
        #
        # A CHECK constraint cannot be altered in place, and two tables point
        # at this one (directives.source_experience_id,
        # incidents.linked_experience_id), so the documented SQLite procedure
        # applies: drop the foreign keys for the swap, rebuild, then VERIFY
        # before turning them back on.  The check is not decorative -- shipping
        # a silently orphaned directive would be worse than the old CHECK.
        conn.commit()
        conn.execute("PRAGMA foreign_keys=OFF")
        try:
            conn.execute("BEGIN")
            # A previous attempt can have died between CREATE and DROP, leaving
            # the scratch table behind; the retry has to be able to start from
            # that state rather than refusing forever.
            conn.execute("DROP TABLE IF EXISTS experiences_v24")
            conn.execute(
                """
                CREATE TABLE experiences_v24 (
                    id TEXT PRIMARY KEY,
                    type TEXT CHECK(type IN ('win','loss','incident','insight')),
                    scope TEXT CHECK(scope IN ('strategy','data','model','risk','tool','process')),
                    agent_kind TEXT,
                    summary TEXT,
                    evidence_ref TEXT,
                    payload_json TEXT,
                    confidence REAL CHECK(confidence IS NULL OR confidence BETWEEN 0 AND 1)
                )
                """
            )
            conn.execute(
                "INSERT INTO experiences_v24 "
                "SELECT id, type, scope, agent_kind, summary, evidence_ref, "
                "payload_json, confidence FROM experiences"
            )
            conn.execute("DROP TABLE experiences")
            conn.execute("ALTER TABLE experiences_v24 RENAME TO experiences")
            # Scoped to the two tables that point at experiences, and to
            # violations whose PARENT is experiences.  A whole-database check
            # reports damage this migration did not cause -- the live database
            # carries 13 data_quality rows orphaned by the archival job -- and
            # blocking on those would refuse a rebuild that was perfectly sound.
            violations = [
                row for table in ("directives", "incidents")
                for row in conn.execute(f'PRAGMA foreign_key_check("{table}")')
                if str(row[2]) == "experiences"
            ]
            if violations:
                raise sqlite3.IntegrityError(
                    f"experiences rebuild would orphan {len(violations)} rows; rolled back")
            ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            conn.execute("INSERT INTO schema_migrations VALUES (24, ?)", (ts,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            # Always restored, including on the failure path: leaving the
            # connection with foreign keys disabled would turn every later
            # write into one that cannot be trusted.
            conn.execute("PRAGMA foreign_keys=ON")
    if version < 25:
        # Relabel, not convert.  The paper book was opened in EUR while every
        # figure in it came from Bybit USDT pairs at an FX rate of 1, so the
        # numbers were always dollars and only the label was wrong.  Rewriting
        # the amounts would invent a conversion that never happened; correcting
        # the label is what makes the existing rows say what they always meant.
        conn.execute(
            "UPDATE portfolio SET currency='USDT' WHERE venue='paper' AND currency='EUR'"
        )
        conn.execute(
            "UPDATE ledger SET currency='USDT' WHERE venue='paper' AND currency='EUR'"
        )
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        conn.execute("INSERT INTO schema_migrations VALUES (25, ?)", (ts,))
    if version < 26:
        # A model dedicated to one agent class.  The router picks by capability
        # fit and cost, which is right for a pool of interchangeable models and
        # wrong for a metered one bought for a single job: without this, paying
        # for a better model to do the company's thinking would hand it to every
        # backtest summariser as well, and the budget would be gone by morning.
        #
        # Reservation cuts both ways, which is why it is one column: the general
        # pool must not offer it to anyone else, AND the class it belongs to
        # should get it without the caller having to remember to ask.
        if "reserved_for_agent_class" not in _columns(conn, "config_models"):
            conn.execute("ALTER TABLE config_models ADD COLUMN reserved_for_agent_class TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_config_models_reserved "
                     "ON config_models(reserved_for_agent_class)")
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        conn.execute("INSERT INTO schema_migrations VALUES (26, ?)", (ts,))
    if version < 27:
        # The trades each PAPER strategy's signal would have made, at the
        # observed mid with the backtester's costs.  Capital goes to one
        # variant per idea; every variant keeps this record, so the one that
        # holds the capital is chosen on evidence (atc/core/shadow.py).
        conn.execute(
            "CREATE TABLE IF NOT EXISTS shadow_trades ("
            " id TEXT PRIMARY KEY,"
            " strategy_id TEXT REFERENCES strategies(id),"
            " instrument TEXT NOT NULL,"
            " opened TEXT NOT NULL,"
            " entry_cents INTEGER NOT NULL CHECK(typeof(entry_cents)='integer'),"
            " closed TEXT,"
            " exit_cents INTEGER CHECK(exit_cents IS NULL OR typeof(exit_cents)='integer'),"
            " net_bps REAL)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_shadow_trades_strategy "
                     "ON shadow_trades(strategy_id, instrument)")
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        conn.execute("INSERT INTO schema_migrations VALUES (27, ?)", (ts,))
    if version < 28:
        # How far a backtest's entry times beat entries drawn at random from
        # the same bars, in standard deviations of that null. NULL means the
        # comparison was not run (the result had already failed) or could not
        # be made. See atc/core/engines/random_entry.py.
        if "random_entry_z" not in _columns(conn, "backtests"):
            conn.execute("ALTER TABLE backtests ADD COLUMN random_entry_z REAL")
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        conn.execute("INSERT INTO schema_migrations VALUES (28, ?)", (ts,))
    if version < 29:
        # How many times dispatch has revived this run after it died.  Separate
        # from retry_count, which is the manager's own in-run ladder and ends
        # at max_retries+1: sharing it meant a run that used up that ladder
        # could never be revived, which is exactly the case reviving is for.
        if "dispatch_revivals" not in _columns(conn, "agent_runs"):
            conn.execute("ALTER TABLE agent_runs ADD COLUMN dispatch_revivals INTEGER DEFAULT 0")
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        conn.execute("INSERT INTO schema_migrations VALUES (29, ?)", (ts,))
    conn.commit()
