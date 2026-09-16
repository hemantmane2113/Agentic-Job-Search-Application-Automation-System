"""
Daily match-digest orchestration.

`pipeline.run_daily_recommendations` is the business-logic entry point.
It contains NO scheduling — an OS cron / Task Scheduler (or, in Stage B,
an opt-in in-process scheduler) invokes it.
"""
