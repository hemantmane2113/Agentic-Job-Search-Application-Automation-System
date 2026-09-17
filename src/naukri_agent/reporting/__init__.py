"""
Reporting mirrors generated FROM the database.

The database is the source of truth. The Excel workbook is a read-only
reporting artifact, fully regenerated from DB state on every export —
do not hand-edit it; edits are overwritten. Change state via the CLI
(mark-applied / mark-status / --note).
"""
