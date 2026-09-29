PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS archival_runs (
    month TEXT PRIMARY KEY,
    archived_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS strategy_ideas (
    id TEXT PRIMARY KEY,
    title TEXT,
    description TEXT,
    market TEXT CHECK(market IN ('crypto_spot','crypto_futures','equities_it','macro')),
    instruments_json TEXT,
    timeframe TEXT,
    evidence_refs TEXT NOT NULL CHECK(length(trim(evidence_refs)) > 2),
    novelty TEXT,
    falsifiability TEXT,
    critic_status TEXT CHECK(critic_status IN ('PENDING','PASSED','REJECTED','NEEDS_REVISION')),
    critic_score INTEGER CHECK(critic_score BETWEEN 0 AND 100),
    status TEXT CHECK(status IN ('NEW','CRITIC_PENDING','CANDIDATE','TESTED','FUNDED','DEAD')),
    fingerprint TEXT,
    family TEXT,
    indicators_json TEXT,
    params_json TEXT
);

CREATE TABLE IF NOT EXISTS strategies (
    id TEXT PRIMARY KEY,
    idea_id TEXT REFERENCES strategy_ideas(id),
    version INTEGER,
    config_json TEXT,
    parent_strategy_id TEXT REFERENCES strategies(id),
    capital_cap_cents INTEGER CHECK(capital_cap_cents IS NULL OR typeof(capital_cap_cents)='integer'),
    max_daily_loss_cents INTEGER CHECK(max_daily_loss_cents IS NULL OR typeof(max_daily_loss_cents)='integer'),
    status TEXT CHECK(status IN ('BACKTESTING','PAPER','LIVE','FROZEN','DEAD','ARCHIVED')),
    evidence_bars TEXT,
    expected_value_bps REAL,
    sharpe REAL,
    win_rate REAL,
    failure_reason TEXT
);

CREATE TABLE IF NOT EXISTS portfolio (
    venue TEXT PRIMARY KEY CHECK(venue IN ('bybit','directa','paper')),
    cash_available_cents INTEGER NOT NULL CHECK(typeof(cash_available_cents)='integer'),
    cash_reserved_cents INTEGER NOT NULL DEFAULT 0 CHECK(typeof(cash_reserved_cents)='integer'),
    equity_cents INTEGER NOT NULL CHECK(typeof(equity_cents)='integer'),
    currency TEXT NOT NULL CHECK(currency IN ('EUR','USDT')),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_classes (
    id TEXT PRIMARY KEY,
    prompt_folder TEXT,
    output_contract TEXT,
    allowed_keys TEXT,
    allowed_tables TEXT,
    default_model TEXT,
    max_concurrency INTEGER,
    max_wallclock_s INTEGER,
    max_tokens INTEGER,
    budget_class TEXT CHECK(budget_class IN ('research','trading','ops')),
    spawn_policy_json TEXT,
    enabled INTEGER CHECK(enabled IN (0,1)),
    quarantined INTEGER NOT NULL DEFAULT 0 CHECK(quarantined IN (0,1))
);

CREATE TABLE IF NOT EXISTS agent_runs (
    id TEXT PRIMARY KEY,
    agent_class_id TEXT REFERENCES agent_classes(id),
    work_item_type TEXT,
    work_item_id TEXT,
    model TEXT,
    input_snapshot_json TEXT,
    status TEXT CHECK(status IN ('QUEUED','RUNNING','SUCCEEDED','FAILED','KILLED','QUOTA_WAIT')),
    fail_reason TEXT,
    tokens_in INTEGER,
    tokens_out INTEGER,
    cost_cents INTEGER CHECK(cost_cents IS NULL OR typeof(cost_cents)='integer'),
    wallclock_s REAL,
    retry_count INTEGER,
    heartbeat_ts TEXT,
    pid INTEGER,
    dedup_key TEXT NOT NULL UNIQUE,
    payload_json TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_calls (
    id TEXT PRIMARY KEY,
    agent_run_id TEXT REFERENCES agent_runs(id),
    model TEXT,
    provider TEXT,
    tokens_in INTEGER,
    tokens_out INTEGER,
    cost_cents INTEGER CHECK(cost_cents IS NULL OR typeof(cost_cents)='integer'),
    latency_ms INTEGER,
    status TEXT CHECK(status IN ('OK','RATE_LIMITED','TIMEOUT','INVALID_OUTPUT','REJECTED_POLICY','ERROR')),
    retry_of TEXT REFERENCES model_calls(id),
    dedup_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS data_sources (
    id TEXT PRIMARY KEY,
    slug TEXT UNIQUE,
    kind TEXT CHECK(kind IN ('exchange_api','free_api','file','rss','paper_repo')),
    endpoint TEXT CHECK(endpoint IS NULL OR lower(endpoint) LIKE 'https://%'),
    legality_note TEXT,
    tos_ok INTEGER CHECK(tos_ok IN (0,1)),
    rate_limit TEXT,
    cost_model_json TEXT,
    status TEXT CHECK(status IN ('ACTIVE','PROPOSED','REJECTED','SUSPENDED')),
    reviewed_at TEXT,
    review_expires_at TEXT
);

CREATE TABLE IF NOT EXISTS datasets (
    id TEXT PRIMARY KEY,
    instrument TEXT,
    timeframe TEXT,
    source_id TEXT REFERENCES data_sources(id),
    cleaning_status TEXT CHECK(cleaning_status IN ('RAW','CLEANING','CLEAN','REJECTED')),
    quality_score REAL CHECK(quality_score IS NULL OR quality_score BETWEEN 0 AND 1),
    rows INTEGER,
    last_updated TEXT
);

CREATE TABLE IF NOT EXISTS market_data (
    id TEXT PRIMARY KEY,
    dataset_id TEXT REFERENCES datasets(id),
    path TEXT,
    format TEXT CHECK(format IN ('parquet','csv','jsonl')),
    start_ts TEXT,
    end_ts TEXT,
    rows INTEGER,
    checksum TEXT,
    status TEXT CHECK(status IN ('STAGING','READY','CORRUPT','ARCHIVED'))
);

CREATE TABLE IF NOT EXISTS data_quality (
    id TEXT PRIMARY KEY,
    dataset_id TEXT REFERENCES datasets(id),
    agent_run_id TEXT REFERENCES agent_runs(id),
    issues_json TEXT,
    correction_policy TEXT CHECK(correction_policy IN ('drop','interpolate','flag')),
    quality_score REAL CHECK(quality_score IS NULL OR quality_score BETWEEN 0 AND 1),
    status TEXT CHECK(status IN ('PASS','WARN','FAIL')),
    dedup_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS literature (
    id TEXT PRIMARY KEY,
    dedup_key TEXT NOT NULL UNIQUE,
    kind TEXT CHECK(kind IN ('paper','news','blog','tweet','regulation')),
    title TEXT,
    source_id TEXT REFERENCES data_sources(id),
    url TEXT,
    authors_json TEXT,
    published_at TEXT,
    abstract TEXT,
    summary TEXT,
    critique TEXT,
    relevance_score REAL CHECK(relevance_score IS NULL OR relevance_score BETWEEN 0 AND 1),
    evidence_strength TEXT CHECK(evidence_strength IN ('strong','weak','conflicting')),
    status TEXT CHECK(status IN ('NEW','SUMMARIZED','CRITIQUED','APPLIED','SUPERSEDED','IRRELEVANT')),
    superseded_by TEXT REFERENCES literature(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    id TEXT PRIMARY KEY,
    venue TEXT NOT NULL REFERENCES portfolio(venue),
    instrument TEXT NOT NULL,
    side TEXT CHECK(side IN ('LONG','SHORT','FLAT')),
    quantity REAL,
    avg_entry_price_cents INTEGER CHECK(avg_entry_price_cents IS NULL OR typeof(avg_entry_price_cents)='integer'),
    notional_cents INTEGER CHECK(notional_cents IS NULL OR typeof(notional_cents)='integer'),
    unrealized_pnl_cents INTEGER CHECK(unrealized_pnl_cents IS NULL OR typeof(unrealized_pnl_cents)='integer'),
    strategy_id TEXT REFERENCES strategies(id),
    status TEXT CHECK(status IN ('OPEN','CLOSING','CLOSED'))
);

CREATE TABLE IF NOT EXISTS ledger (
    id TEXT PRIMARY KEY,
    dedup_key TEXT NOT NULL UNIQUE,
    type TEXT CHECK(type IN ('DEPOSIT','WITHDRAWAL','FEE','EXECUTION_BUY','EXECUTION_SELL','REALIZED_PNL','FUNDING','MODEL_COST','COMPUTE_COST','DATA_COST','TAX_RESERVE','CORRECTION')),
    amount_cents INTEGER NOT NULL CHECK(typeof(amount_cents)='integer'),
    order_id TEXT REFERENCES orders(id),
    strategy_id TEXT REFERENCES strategies(id),
    venue TEXT,
    instrument TEXT,
    currency TEXT CHECK(currency IS NULL OR currency IN ('EUR','USDT')),
    category TEXT CHECK(category IN ('trading','operating')),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fx_rates (
    id TEXT PRIMARY KEY,
    pair TEXT NOT NULL,
    rate REAL NOT NULL CHECK(rate > 0),
    source TEXT NOT NULL,
    ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS suggestions (
    id TEXT PRIMARY KEY,
    agent_run_id TEXT REFERENCES agent_runs(id),
    venue TEXT NOT NULL,
    instrument TEXT NOT NULL,
    direction TEXT NOT NULL CHECK(direction IN ('LONG_ENTRY','LONG_EXIT','SHORT_ENTRY','SHORT_EXIT')),
    quantity REAL NOT NULL,
    price_limit_cents INTEGER NOT NULL CHECK(typeof(price_limit_cents)='integer'),
    slippage_tolerance_bps INTEGER,
    strategy_id TEXT NOT NULL REFERENCES strategies(id),
    rationale TEXT,
    estimated_notional_cents INTEGER CHECK(estimated_notional_cents IS NULL OR typeof(estimated_notional_cents)='integer'),
    live INTEGER NOT NULL DEFAULT 0 CHECK(live IN (0,1)),
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','APPROVED','REJECTED','EXECUTED','STALE','EXPIRED')),
    reject_reason TEXT,
    gate_payload_json TEXT,
    dedup_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS risk_decisions (
    id TEXT PRIMARY KEY,
    suggestion_id TEXT NOT NULL REFERENCES suggestions(id),
    attempt INTEGER NOT NULL DEFAULT 1 CHECK(attempt > 0),
    decision TEXT NOT NULL CHECK(decision IN ('APPROVED','REJECTED')),
    reason_code TEXT CHECK(reason_code IS NULL OR reason_code IN ('','A_SCHEMA','A_DIRECTION','A_QUANTITY','A_NO_PRICE_LIMIT','A_ORPHAN_STRATEGY','A_FORBIDDEN_PRODUCER','B_KILL_SWITCH','B_VENUE_DISABLED','B_FUTURES_DISABLED','B_RECON_STALE','B_BLOCKING_INCIDENT','C_CASH_COVERAGE','C_MAX_NOTIONAL','D_SHORTING_DISABLED','D_UNCOVERED_SHORT','D_EXIT_EXCEEDS_POSITION','D_LEVERAGE_DISABLED','D_MARGIN_COVERAGE','D_LEVERAGE_LIMIT','E_STRATEGY_CAP','E_PORTFOLIO_CAP','E_INSTRUMENT_CAP','E_MAX_POSITIONS','F_NO_MARKET','F_STALE_PRICE','G_RATE_LIMIT','G_VENUE_RATE_LIMIT','H_INSTRUMENT_NOT_ALLOWED','H_DAILY_LOSS_LIMIT','H_DRAWDOWN_HALT','H_REVIEW_EXPIRED')),
    checks_json TEXT NOT NULL,
    worst_case_cost_cents INTEGER CHECK(worst_case_cost_cents IS NULL OR typeof(worst_case_cost_cents)='integer'),
    policy_version TEXT NOT NULL,
    approval_token_hash TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(suggestion_id, attempt)
);

CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    dedup_key TEXT NOT NULL UNIQUE,
    suggestion_id TEXT NOT NULL REFERENCES suggestions(id),
    risk_decision_id TEXT NOT NULL REFERENCES risk_decisions(id),
    client_order_id TEXT NOT NULL UNIQUE,
    venue TEXT NOT NULL,
    instrument TEXT NOT NULL,
    direction TEXT NOT NULL CHECK(direction IN ('LONG_ENTRY','LONG_EXIT','SHORT_ENTRY','SHORT_EXIT')),
    side TEXT CHECK(side IN ('BUY','SELL')),
    order_type TEXT CHECK(order_type IN ('MARKET','LIMIT')),
    quantity REAL NOT NULL,
    limit_price_cents INTEGER CHECK(limit_price_cents IS NULL OR typeof(limit_price_cents)='integer'),
    slippage_tolerance_bps INTEGER CHECK(slippage_tolerance_bps IS NULL OR (slippage_tolerance_bps >= 0 AND slippage_tolerance_bps < 10000)),
    status TEXT CHECK(status IN ('PENDING','SUBMITTED','FILLED','PARTIAL','CANCELLED','REJECTED_GATE','REJECTED_EXCHANGE','ERROR')),
    fees_cents INTEGER CHECK(fees_cents IS NULL OR typeof(fees_cents)='integer'),
    filled_avg_price_cents INTEGER CHECK(filled_avg_price_cents IS NULL OR typeof(filled_avg_price_cents)='integer'),
    exchange_order_id TEXT,
    live INTEGER NOT NULL DEFAULT 0 CHECK(live IN (0,1)),
    payload_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fills (
    id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES orders(id),
    venue_fill_id TEXT NOT NULL,
    quantity REAL NOT NULL,
    price_cents INTEGER NOT NULL CHECK(typeof(price_cents)='integer'),
    fee_cents INTEGER NOT NULL DEFAULT 0 CHECK(typeof(fee_cents)='integer'),
    slippage_bps REAL,
    ts TEXT NOT NULL,
    dedup_key TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS paper_trades (
    id TEXT PRIMARY KEY,
    strategy_id TEXT REFERENCES strategies(id),
    suggestion_id TEXT REFERENCES suggestions(id),
    fill_cents INTEGER CHECK(fill_cents IS NULL OR typeof(fill_cents)='integer'),
    slippage_cents INTEGER CHECK(slippage_cents IS NULL OR typeof(slippage_cents)='integer'),
    fees_cents INTEGER CHECK(fees_cents IS NULL OR typeof(fees_cents)='integer'),
    pnl_cents INTEGER CHECK(pnl_cents IS NULL OR typeof(pnl_cents)='integer'),
    opened TEXT,
    closed TEXT
);

CREATE TABLE IF NOT EXISTS backtests (
    id TEXT PRIMARY KEY,
    strategy_id TEXT REFERENCES strategies(id),
    dataset_id TEXT REFERENCES datasets(id),
    engine_version TEXT,
    params_json TEXT,
    success_criteria_json TEXT,
    in_sample_range TEXT,
    out_sample_range TEXT,
    total_return_pct REAL,
    sharpe REAL,
    max_drawdown_pct REAL,
    win_rate REAL,
    num_trades INTEGER,
    pnl_cents INTEGER CHECK(pnl_cents IS NULL OR typeof(pnl_cents)='integer'),
    walk_forward_passed INTEGER CHECK(walk_forward_passed IN (0,1)),
    overfit_flag INTEGER CHECK(overfit_flag IN (0,1)),
    lookahead_flag INTEGER CHECK(lookahead_flag IN (0,1)),
    status TEXT CHECK(status IN ('RUNNING','PASSED','FAILED','INCONCLUSIVE')),
    critique_id TEXT REFERENCES critiques(id),
    failure_reason TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    dedup_key TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS critiques (
    id TEXT PRIMARY KEY,
    target_type TEXT CHECK(target_type IN ('strategy_idea','backtest','paper_trade_set','literature')),
    target_id TEXT,
    verdict TEXT CHECK(verdict IN ('PASS','REJECT','REVISE')),
    score INTEGER CHECK(score BETWEEN 0 AND 100),
    flaws_json TEXT,
    suggested_fixes TEXT,
    cost_cents INTEGER CHECK(cost_cents IS NULL OR typeof(cost_cents)='integer')
);

CREATE TABLE IF NOT EXISTS config_general (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_by TEXT NOT NULL DEFAULT 'operator' CHECK(updated_by IN ('operator','system'))
);

CREATE TABLE IF NOT EXISTS config_venues (
    id TEXT PRIMARY KEY,
    enabled INTEGER CHECK(enabled IN (0,1)),
    live_trading_allowed INTEGER CHECK(live_trading_allowed IN (0,1)),
    instruments_json TEXT,
    credentials_key TEXT
);

CREATE TABLE IF NOT EXISTS config_keys (
    id TEXT PRIMARY KEY,
    category TEXT CHECK(category IN ('bybit','directa','models','data')),
    encrypted_value TEXT NOT NULL,
    mask TEXT,
    status TEXT
);

CREATE TABLE IF NOT EXISTS config_models (
    id TEXT PRIMARY KEY,
    provider TEXT,
    provider_config_id TEXT,
    model_name TEXT,
    base_url TEXT,
    api_key_env TEXT,
    credentials_key TEXT,
    cost_in_per_1M INTEGER CHECK(typeof(cost_in_per_1M)='integer'),
    cost_out_per_1M INTEGER CHECK(typeof(cost_out_per_1M)='integer'),
    local INTEGER CHECK(local IN (0,1)),
    free_tier INTEGER NOT NULL DEFAULT 0 CHECK(free_tier IN (0,1)),
    context_window INTEGER,
    capability_ratings_json TEXT,
    status TEXT CHECK(status IN ('ACTIVE','SUSPENDED','QUOTA')),
    cooldown_until TEXT,
    last_error TEXT,
    discovered_at TEXT
);

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

CREATE TABLE IF NOT EXISTS config_allowlist (
    slug TEXT NOT NULL,
    host TEXT NOT NULL,
    added_by TEXT NOT NULL DEFAULT 'operator' CHECK(added_by IN ('operator','system')),
    note TEXT,
    PRIMARY KEY (slug, host)
);

CREATE TABLE IF NOT EXISTS cycles (
    id TEXT PRIMARY KEY,
    kind TEXT CHECK(kind IN ('micro','standard','deep','report')),
    status TEXT CHECK(status IN ('RUNNING','DONE','CRASHED','RESUMED')),
    last_stage TEXT,
    goal TEXT,
    stage_log_json TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS cycle_stages (
    id TEXT PRIMARY KEY,
    cycle_id TEXT NOT NULL REFERENCES cycles(id),
    stage TEXT NOT NULL,
    dedup_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK(status IN ('COMMITTED')),
    committed_at TEXT NOT NULL,
    details_json TEXT
);

CREATE TABLE IF NOT EXISTS heartbeats (
    id TEXT PRIMARY KEY,
    component TEXT NOT NULL,
    cycle_id TEXT REFERENCES cycles(id),
    ts TEXT NOT NULL,
    dedup_key TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS degraded_states (
    name TEXT PRIMARY KEY,
    active INTEGER NOT NULL CHECK(active IN (0,1)),
    reason TEXT,
    entered_at TEXT,
    cleared_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS improvement_log (
    id TEXT PRIMARY KEY,
    cycle_id TEXT REFERENCES cycles(id),
    kind TEXT CHECK(kind IN ('plan','decision','result','defect')),
    action TEXT,
    target_type TEXT,
    target_id TEXT,
    evidence_refs TEXT,
    expected_outcome TEXT,
    actual_outcome TEXT,
    status TEXT,
    cost_cents INTEGER CHECK(cost_cents IS NULL OR typeof(cost_cents)='integer')
);

CREATE TABLE IF NOT EXISTS experiences (
    id TEXT PRIMARY KEY,
    type TEXT CHECK(type IN ('win','loss','incident')),
    scope TEXT CHECK(scope IN ('strategy','data','model','risk','tool','process')),
    agent_kind TEXT,
    summary TEXT,
    evidence_ref TEXT,
    payload_json TEXT,
    confidence REAL CHECK(confidence IS NULL OR confidence BETWEEN 0 AND 1)
);

CREATE TABLE IF NOT EXISTS directives (
    id TEXT PRIMARY KEY,
    scope TEXT,
    agent_kind TEXT,
    kind TEXT CHECK(kind IN ('DO','DONT')),
    predicate_json TEXT,
    human_text TEXT,
    source_experience_id TEXT REFERENCES experiences(id),
    active INTEGER CHECK(active IN (0,1))
);

CREATE TABLE IF NOT EXISTS incidents (
    id TEXT PRIMARY KEY,
    ts TEXT,
    severity TEXT CHECK(severity IN ('INFO','WARN','CRITICAL')),
    code TEXT,
    component TEXT,
    description TEXT,
    root_cause TEXT,
    resolution TEXT,
    blocks_live INTEGER CHECK(blocks_live IN (0,1)),
    linked_experience_id TEXT REFERENCES experiences(id)
);

CREATE TABLE IF NOT EXISTS compliance_rules (
    id TEXT PRIMARY KEY,
    rule_code TEXT,
    scope TEXT CHECK(scope IN ('venue','product','instrument','data_source','strategy','global')),
    jurisdiction TEXT,
    decision TEXT CHECK(decision IN ('allowed','rejected','human_review_required')),
    source_url TEXT,
    reviewed_at TEXT,
    expires_at TEXT
);

CREATE TABLE IF NOT EXISTS tools (
    id TEXT PRIMARY KEY,
    name TEXT,
    path TEXT,
    description TEXT,
    cost_cents INTEGER CHECK(cost_cents IS NULL OR typeof(cost_cents)='integer'),
    test_status TEXT,
    status TEXT CHECK(status IN ('PROPOSED','TESTED','ACTIVE','RETIRED'))
);

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

CREATE TABLE IF NOT EXISTS reports (
    id TEXT PRIMARY KEY,
    kind TEXT CHECK(kind IN ('cycle','daily','weekly','tax')),
    summary TEXT,
    sections_json TEXT
);

CREATE TABLE IF NOT EXISTS alerts (
    id TEXT PRIMARY KEY,
    severity TEXT,
    code TEXT,
    message TEXT,
    status TEXT CHECK(status IN ('OPEN','ACKNOWLEDGED','RESOLVED'))
);

CREATE TABLE IF NOT EXISTS operator_messages (
    id TEXT PRIMARY KEY,
    kind TEXT,
    handled INTEGER CHECK(handled IN (0,1)),
    payload_json TEXT
);

CREATE TABLE IF NOT EXISTS config_strategies (
    id TEXT PRIMARY KEY,
    live_trading_allowed INTEGER NOT NULL DEFAULT 0 CHECK(live_trading_allowed IN (0,1)),
    frozen INTEGER NOT NULL DEFAULT 0 CHECK(frozen IN (0,1)),
    updated_by TEXT NOT NULL DEFAULT 'operator' CHECK(updated_by IN ('operator','system')),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS metrics (
    id TEXT PRIMARY KEY,
    dedup_key TEXT NOT NULL UNIQUE,
    day TEXT,
    hour TEXT,
    venue TEXT,
    equity_cents INTEGER CHECK(equity_cents IS NULL OR typeof(equity_cents)='integer'),
    cash_cents INTEGER CHECK(cash_cents IS NULL OR typeof(cash_cents)='integer'),
    realized_pnl_cents INTEGER CHECK(realized_pnl_cents IS NULL OR typeof(realized_pnl_cents)='integer'),
    unrealized_pnl_cents INTEGER CHECK(unrealized_pnl_cents IS NULL OR typeof(unrealized_pnl_cents)='integer'),
    drawdown_from_peak_pct REAL,
    exposure_cents INTEGER CHECK(exposure_cents IS NULL OR typeof(exposure_cents)='integer'),
    open_positions INTEGER,
    active_strategies INTEGER,
    cost_month_cents INTEGER CHECK(cost_month_cents IS NULL OR typeof(cost_month_cents)='integer'),
    net_pnl_month_cents INTEGER CHECK(net_pnl_month_cents IS NULL OR typeof(net_pnl_month_cents)='integer')
);

CREATE TABLE IF NOT EXISTS audit_events (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    entity_type TEXT,
    entity_id TEXT,
    before_hash TEXT,
    after_hash TEXT,
    details_json TEXT,
    dedup_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS venue_freezes (
    venue TEXT PRIMARY KEY,
    frozen INTEGER NOT NULL CHECK(frozen IN (0,1)),
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
    matched INTEGER NOT NULL CHECK(matched IN (0,1)),
    mismatch_json TEXT,
    dedup_key TEXT NOT NULL UNIQUE
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_status_created ON agent_runs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_model_calls_created ON model_calls(created_at);
CREATE INDEX IF NOT EXISTS idx_literature_status ON literature(status);
CREATE INDEX IF NOT EXISTS idx_quality_status ON data_quality(status);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_events(created_at);
CREATE INDEX IF NOT EXISTS idx_suggestions_status ON suggestions(status);
CREATE INDEX IF NOT EXISTS idx_suggestions_strategy ON suggestions(strategy_id);
CREATE INDEX IF NOT EXISTS idx_suggestions_instrument ON suggestions(instrument);
CREATE INDEX IF NOT EXISTS idx_orders_status_created ON orders(status, created_at);
CREATE INDEX IF NOT EXISTS idx_orders_venue ON orders(venue);
CREATE INDEX IF NOT EXISTS idx_positions_instrument ON positions(instrument);
CREATE INDEX IF NOT EXISTS idx_positions_venue ON positions(venue);
CREATE INDEX IF NOT EXISTS idx_fills_order ON fills(order_id);
CREATE INDEX IF NOT EXISTS idx_ledger_created ON ledger(created_at);
CREATE INDEX IF NOT EXISTS idx_ledger_venue ON ledger(venue);
CREATE INDEX IF NOT EXISTS idx_cycles_kind_created ON cycles(kind, created_at);
CREATE INDEX IF NOT EXISTS idx_ideas_fingerprint ON strategy_ideas(fingerprint);

CREATE VIEW IF NOT EXISTS v_portfolio_overview AS SELECT * FROM portfolio;
CREATE VIEW IF NOT EXISTS v_strategy_funnel AS SELECT status, count(*) AS count FROM strategy_ideas GROUP BY status;
CREATE VIEW IF NOT EXISTS v_cost_breakdown AS SELECT category, sum(amount_cents) AS cents FROM ledger GROUP BY category;
CREATE VIEW IF NOT EXISTS v_recent_agent_activity AS SELECT * FROM agent_runs ORDER BY created_at DESC;
CREATE VIEW IF NOT EXISTS v_open_suggestions AS SELECT * FROM suggestions WHERE status IN ('PENDING','APPROVED');
CREATE VIEW IF NOT EXISTS v_strategy_performance AS SELECT strategy_id, sum(pnl_cents) pnl_cents FROM paper_trades GROUP BY strategy_id;
CREATE VIEW IF NOT EXISTS v_is_improving AS
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
    FROM pnl_bounds p, plan_bounds pb, bt, ideas;
