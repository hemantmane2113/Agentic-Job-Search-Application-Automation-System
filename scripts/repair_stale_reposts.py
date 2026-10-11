"""
One-time repair for stale `Job.repost_of_job_id` links.

Background: if the FIRST fetch of two distinct listings returned
empty/identical detail content, they were wrongly linked as reposts
(same content_fingerprint at insert time). A later fetch refreshes each
row's fingerprint to its real, distinct value but does NOT revisit the
historical link, so canonical_job_id() keeps collapsing them and the
daily digest under-reports.

`database.repositories.upsert_job` now repairs such links automatically
on the NEXT re-sighting of each URL. This script fixes rows that are
already in the database, without waiting for that re-sighting.

Criterion (identical to the in-code rule — exact fingerprint equality,
never fuzzy): a link `child.repost_of_job_id = parent.id` is STALE when
`child.content_fingerprint != parent.content_fingerprint` (or the
parent row is gone). Only such links are cleared. Nothing is merged or
deleted; no row outside the `jobs` table is touched; application /
recommendation / match rows keep the `job_id` they were written with.

Usage:
    python -m scripts.repair_stale_reposts             # DRY RUN (default) - shows changes, writes nothing
    python -m scripts.repair_stale_reposts --apply     # actually clears the stale links
    python -m scripts.repair_stale_reposts --database-url sqlite:///./data/naukri_agent.db
"""

from __future__ import annotations

import argparse
import sys

from naukri_agent.config import get_settings
from naukri_agent.database.base import init_db, session_scope
from naukri_agent.database.models import Job


def _find_stale(session) -> list[tuple[int, int, str, str]]:
    """Returns (child_id, parent_id, child_fp8, parent_fp8-or-'MISSING')."""
    stale: list[tuple[int, int, str, str]] = []
    for child in session.query(Job).filter(Job.repost_of_job_id.isnot(None)).all():
        parent = session.get(Job, child.repost_of_job_id)
        if parent is None:
            stale.append((child.id, child.repost_of_job_id, child.content_fingerprint[:8], "MISSING"))
        elif parent.content_fingerprint != child.content_fingerprint:
            stale.append(
                (child.id, parent.id, child.content_fingerprint[:8], parent.content_fingerprint[:8])
            )
    return stale


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    parser.add_argument("--database-url", default=None, help="override settings.database_url")
    args = parser.parse_args(argv)

    settings = get_settings()
    if args.database_url:
        settings = settings.model_copy(update={"database_url": args.database_url})
    print(f"database: {settings.database_url}")

    factory = init_db(settings)
    with session_scope(factory) as session:
        stale = _find_stale(session)
        if not stale:
            print("No stale repost links found. Nothing to do.")
            return 0

        print(f"\n{len(stale)} stale repost link(s) would be cleared "
              "(set jobs.repost_of_job_id = NULL):\n")
        for child_id, parent_id, cfp, pfp in stale:
            print(f"  job {child_id}: repost_of_job_id {parent_id} -> NULL   "
                  f"(child fp {cfp}… != parent fp {pfp}…)")

        if not args.apply:
            print("\nDRY RUN — no changes written. Re-run with --apply to perform the repair.")
            return 0

        for child_id, _parent_id, _cfp, _pfp in stale:
            session.get(Job, child_id).repost_of_job_id = None
        print(f"\nApplied: cleared {len(stale)} stale repost link(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
