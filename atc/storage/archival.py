from datetime import datetime, timedelta, timezone


HOT = ("agent_runs", "model_calls", "literature", "data_quality", "audit_events")


def archive_old(repo, now=None):
    """Archive hot evidence through the Repository boundary."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    cutoff = current.astimezone(timezone.utc) - timedelta(days=90)
    repo.archive_old(cutoff, run_month=current.astimezone(timezone.utc).strftime("%Y-%m"))
