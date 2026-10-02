"""
Stage B: in-process daily scheduler (`naukri-agent scheduler`).

`daemon.run_scheduler` is the entry point — a cross-platform
alternative to an OS-level cron entry / Task Scheduler task calling
`naukri-agent run-daily` on its own schedule. Neither approach is
required over the other; this package exists for whoever would rather
not depend on OS-level scheduling infrastructure.
"""
