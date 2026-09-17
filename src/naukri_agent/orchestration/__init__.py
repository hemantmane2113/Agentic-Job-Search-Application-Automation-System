"""
Daily match-digest orchestration.

`pipeline.run_daily_recommendations` is the business-logic entry point.
It contains NO scheduling — an OS cron / Task Scheduler entry, or
Stage B's opt-in in-process scheduler (`naukri-agent scheduler`, see
scheduler/daemon.py), invokes it.
"""
