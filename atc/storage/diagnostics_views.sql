-- Diagnostic views (operator UI /diagnostics).
--
-- These answer "is the company learning?", which the cumulative
-- v_is_improving cannot: it compares the first metric ever against the last
-- one and divides over every backtest in history, so after one bad week the
-- ratios are dominated by the past and stop moving.  Everything here is
-- windowed, so the operator sees the derivative, not the level.
--
-- All timestamps in this database are UTC ISO-8601 with a Z suffix, which is
-- lexicographically ordered: string comparison against strftime('%Y-%m-%dT%H:%M:%fZ')
-- is exact, and julianday() parses the same format for gap arithmetic.
-- Rows with a NULL created_at (written before the column existed) fall out of
-- every window and stay in the all-time counts.

DROP VIEW IF EXISTS v_diag_windows;
CREATE VIEW v_diag_windows AS
SELECT strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-24 hours') AS since_24h,
       strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-7 days')   AS since_7d,
       strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-30 days')  AS since_30d;

-- Completed backtests only: a RUNNING request row carries no verdict, and
-- counting requests as backtests inflated every ratio.
DROP VIEW IF EXISTS v_diag_backtests;
CREATE VIEW v_diag_backtests AS
SELECT b.id, b.strategy_id, b.dataset_id, b.status, b.sharpe, b.num_trades,
       b.pnl_cents, b.overfit_flag, b.lookahead_flag, b.walk_forward_passed,
       b.failure_reason, b.created_at,
       CASE WHEN b.created_at >= w.since_7d  THEN 1 ELSE 0 END AS in_7d,
       CASE WHEN b.created_at >= w.since_30d THEN 1 ELSE 0 END AS in_30d
FROM backtests b, v_diag_windows w
WHERE b.num_trades IS NOT NULL;

-- "Is it discovering anything?" — the same ratios over 7d / 30d / all time.
DROP VIEW IF EXISTS v_diag_discovery;
CREATE VIEW v_diag_discovery AS
SELECT
    (SELECT count(*) FROM v_diag_backtests WHERE in_7d = 1)  AS backtests_7d,
    (SELECT count(*) FROM v_diag_backtests WHERE in_30d = 1) AS backtests_30d,
    (SELECT count(*) FROM v_diag_backtests)                  AS backtests_all,
    (SELECT count(*) FROM v_diag_backtests WHERE in_7d = 1 AND status = 'PASSED')  AS passed_7d,
    (SELECT count(*) FROM v_diag_backtests WHERE in_30d = 1 AND status = 'PASSED') AS passed_30d,
    (SELECT count(*) FROM v_diag_backtests WHERE status = 'PASSED')                AS passed_all,
    (SELECT 1.0 * sum(overfit_flag) / nullif(count(*), 0)
       FROM v_diag_backtests WHERE in_7d = 1)  AS overfit_share_7d,
    (SELECT 1.0 * sum(overfit_flag) / nullif(count(*), 0)
       FROM v_diag_backtests WHERE in_30d = 1) AS overfit_share_30d,
    (SELECT 1.0 * sum(overfit_flag) / nullif(count(*), 0)
       FROM v_diag_backtests)                  AS overfit_share_all,
    (SELECT 1.0 * sum(walk_forward_passed) / nullif(count(*), 0)
       FROM v_diag_backtests WHERE in_7d = 1)  AS oos_share_7d,
    (SELECT 1.0 * sum(walk_forward_passed) / nullif(count(*), 0)
       FROM v_diag_backtests WHERE in_30d = 1) AS oos_share_30d,
    (SELECT 1.0 * sum(walk_forward_passed) / nullif(count(*), 0)
       FROM v_diag_backtests)                  AS oos_share_all,
    -- A look-ahead flag is never acceptable: it means the engine caught a
    -- configuration reading the future.  Any non-zero count is a red light.
    (SELECT coalesce(sum(lookahead_flag), 0) FROM v_diag_backtests) AS lookahead_all,
    (SELECT avg(sharpe) FROM v_diag_backtests WHERE in_7d = 1)  AS avg_sharpe_7d,
    (SELECT avg(sharpe) FROM v_diag_backtests WHERE in_30d = 1) AS avg_sharpe_30d,
    (SELECT max(sharpe) FROM v_diag_backtests)                  AS best_sharpe_all;

-- The funnel as conversions, not as a pile of counts.
DROP VIEW IF EXISTS v_diag_funnel;
CREATE VIEW v_diag_funnel AS
SELECT
    (SELECT count(*) FROM strategy_ideas)                              AS ideas_total,
    (SELECT count(*) FROM strategy_ideas WHERE status = 'NEW')         AS ideas_new,
    (SELECT count(*) FROM strategy_ideas WHERE status = 'CRITIC_PENDING') AS ideas_critic_pending,
    (SELECT count(*) FROM strategy_ideas WHERE status = 'CANDIDATE')   AS ideas_candidate,
    (SELECT count(*) FROM strategy_ideas WHERE status = 'TESTED')      AS ideas_tested,
    (SELECT count(*) FROM strategy_ideas WHERE status = 'FUNDED')      AS ideas_funded,
    (SELECT count(*) FROM strategy_ideas WHERE status = 'DEAD')        AS ideas_dead,
    (SELECT count(*) FROM strategies)                                  AS strategies_total,
    (SELECT count(*) FROM strategies WHERE status = 'BACKTESTING')     AS strategies_backtesting,
    (SELECT count(*) FROM strategies WHERE status = 'PAPER')           AS strategies_paper,
    (SELECT count(*) FROM strategies WHERE status = 'FROZEN')          AS strategies_frozen,
    (SELECT count(*) FROM strategies WHERE status = 'DEAD')            AS strategies_dead,
    (SELECT count(*) FROM v_diag_backtests)                            AS backtests_done,
    (SELECT count(*) FROM v_diag_backtests WHERE status = 'PASSED')    AS backtests_passed,
    (SELECT count(*) FROM backtests WHERE status = 'RUNNING')          AS backtests_running,
    (SELECT count(*) FROM datasets)                                    AS datasets_total,
    (SELECT count(*) FROM datasets WHERE cleaning_status = 'CLEAN')    AS datasets_clean,
    (SELECT count(*) FROM datasets WHERE cleaning_status = 'REJECTED') AS datasets_rejected,
    (SELECT count(*) FROM paper_trades)                                AS paper_trades_total,
    (SELECT count(*) FROM paper_trades WHERE closed IS NOT NULL AND closed <> '') AS paper_trades_closed,
    (SELECT coalesce(sum(pnl_cents), 0) FROM paper_trades
      WHERE closed IS NOT NULL AND closed <> '')                       AS paper_net_pnl_cents,
    -- Distinct subjects, so the funnel cannot show a conversion above 100%:
    -- one idea legitimately spawns several strategies (every tuned variant is
    -- a strategy row), and counting rows at one step against subjects at the
    -- previous one produced nonsense like "250% became a strategy".
    (SELECT count(DISTINCT idea_id) FROM strategies WHERE idea_id IS NOT NULL) AS ideas_with_strategy,
    -- "Ever got past the critic", not "is currently past it": status is the
    -- CURRENT state, so an idea that was promoted and later died would leave
    -- the funnel's denominator and make the next step exceed 100%.
    (SELECT count(*) FROM strategy_ideas i
      WHERE i.critic_status = 'PASSED'
         OR i.status IN ('CANDIDATE', 'TESTED', 'FUNDED')
         OR EXISTS (SELECT 1 FROM strategies s WHERE s.idea_id = i.id)) AS ideas_past_critic,
    (SELECT count(DISTINCT strategy_id) FROM v_diag_backtests)         AS strategies_backtested,
    (SELECT count(DISTINCT strategy_id) FROM v_diag_backtests WHERE status = 'PASSED')
                                                                       AS strategies_passed,
    (SELECT count(DISTINCT strategy_id) FROM paper_trades
      WHERE closed IS NOT NULL AND closed <> '')                       AS strategies_with_closed_trade,
    (SELECT count(*) FROM strategies WHERE parent_strategy_id IS NOT NULL) AS variants_spawned;

-- Where things die, and why.  This is the panel that replaces grepping the
-- watch log: every terminal state in the company, with its recorded reason.
DROP VIEW IF EXISTS v_diag_reasons;
CREATE VIEW v_diag_reasons AS
    SELECT 'backtest' AS source, coalesce(failure_reason, '(unrecorded)') AS reason, count(*) AS n
      FROM backtests WHERE status = 'FAILED' GROUP BY reason
UNION ALL
    SELECT 'strategy', coalesce(failure_reason, '(unrecorded)'), count(*)
      FROM strategies WHERE failure_reason IS NOT NULL AND failure_reason <> '' GROUP BY failure_reason
UNION ALL
    SELECT 'gate', coalesce(reason_code, '(unrecorded)'), count(*)
      FROM risk_decisions WHERE decision = 'REJECTED' GROUP BY reason_code
UNION ALL
    SELECT 'suggestion', coalesce(reject_reason, '(unrecorded)'), count(*)
      FROM suggestions WHERE status = 'REJECTED' GROUP BY reject_reason
UNION ALL
    SELECT 'agent_run', coalesce(fail_reason, '(unrecorded)'), count(*)
      FROM agent_runs WHERE status IN ('FAILED', 'KILLED') GROUP BY fail_reason
UNION ALL
    SELECT 'incident_open', code, count(*)
      FROM incidents WHERE resolution IS NULL OR resolution = '' GROUP BY code
UNION ALL
    SELECT 'data_quality', coalesce(status, '(unrecorded)'), count(*)
      FROM data_quality GROUP BY status;

-- Cohorts: does generation N beat generation N-1?  With no paper PnL yet this
-- is the only real evidence that the search is going somewhere rather than
-- reshuffling the same parameters.
DROP VIEW IF EXISTS v_diag_cohorts;
CREATE VIEW v_diag_cohorts AS
SELECT coalesce(s.version, 1) AS generation,
       count(DISTINCT s.id)   AS strategies,
       count(b.id)            AS backtests,
       coalesce(sum(CASE WHEN b.status = 'PASSED' THEN 1 ELSE 0 END), 0) AS passed,
       avg(b.sharpe)          AS avg_sharpe,
       max(b.sharpe)          AS best_sharpe,
       avg(b.pnl_cents)       AS avg_pnl_cents,
       avg(b.num_trades)      AS avg_trades
FROM strategies s
LEFT JOIN v_diag_backtests b ON b.strategy_id = s.id
GROUP BY generation
ORDER BY generation;

-- Cost per unit of discovery.  On a local model the numerator is zero and the
-- meaningful column becomes cycles_per_discovery: the signal for whether the
-- scheduler is running hotter than the research can use.
DROP VIEW IF EXISTS v_diag_cost;
CREATE VIEW v_diag_cost AS
SELECT
    (SELECT coalesce(-sum(amount_cents), 0) FROM ledger WHERE type = 'MODEL_COST')    AS model_cost_cents,
    (SELECT coalesce(-sum(amount_cents), 0) FROM ledger WHERE category = 'operating') AS operating_cents,
    (SELECT count(*) FROM v_diag_backtests WHERE status = 'PASSED')                   AS discoveries,
    (SELECT count(*) FROM v_diag_backtests)                                           AS backtests_done,
    (SELECT count(*) FROM cycles WHERE kind = 'standard')                             AS standard_cycles,
    (SELECT count(*) FROM agent_runs)                                                 AS agent_runs_total,
    (SELECT count(*) FROM agent_runs WHERE status = 'SUCCEEDED')                      AS agent_runs_ok;

-- Liveness over the last 24h, by cycle kind.
DROP VIEW IF EXISTS v_diag_activity;
CREATE VIEW v_diag_activity AS
SELECT c.kind,
       count(*) AS cycles_24h,
       coalesce(sum(CASE WHEN c.status = 'DONE' THEN 1 ELSE 0 END), 0)    AS done_24h,
       coalesce(sum(CASE WHEN c.status = 'CRASHED' THEN 1 ELSE 0 END), 0) AS crashed_24h,
       max(c.created_at) AS last_created_at
FROM cycles c, v_diag_windows w
WHERE c.created_at >= w.since_24h
GROUP BY c.kind;

-- Daily cycle throughput (sparkline source, and the shape of the uptime).
DROP VIEW IF EXISTS v_diag_daily_cycles;
CREATE VIEW v_diag_daily_cycles AS
SELECT substr(created_at, 1, 10) AS day,
       count(*) AS cycles,
       coalesce(sum(CASE WHEN kind = 'standard' THEN 1 ELSE 0 END), 0) AS standard_cycles
FROM cycles
WHERE created_at IS NOT NULL AND created_at <> ''
GROUP BY day
ORDER BY day;

-- Downtime: consecutive micro cycles more than 30 minutes apart.  The micro
-- cadence is the company's heartbeat, so a gap here is time the process was
-- not running (a deliberate stop, a crash, or a reboot) — which is exactly
-- what an operator who does not leave the machine on 24/7 needs to see.
DROP VIEW IF EXISTS v_diag_gaps;
CREATE VIEW v_diag_gaps AS
SELECT gap_start, gap_end, gap_hours
FROM (
    SELECT lag(created_at) OVER (ORDER BY created_at) AS gap_start,
           created_at AS gap_end,
           (julianday(created_at) - julianday(lag(created_at) OVER (ORDER BY created_at))) * 24.0 AS gap_hours
    FROM cycles
    WHERE kind = 'micro' AND created_at IS NOT NULL AND created_at <> ''
)
WHERE gap_start IS NOT NULL AND gap_hours > 0.5
ORDER BY gap_end DESC;
