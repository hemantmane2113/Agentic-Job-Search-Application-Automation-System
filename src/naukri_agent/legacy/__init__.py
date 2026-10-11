"""
Older tools that still work but are not part of the daily flow.

Nothing here runs on a schedule, and the daily run, the digest, the research, the phone control and
`telegram-apply` never import this package. Each module is reachable only from its own command:

  apply_runner.py        `naukri-agent apply`: a supervised, terminal-only apply with a human at the keyboard. Replaced
                         by `telegram-apply`, which asks you on your phone instead.
  profile_inspection.py  `naukri-agent inspect-profile-edit`: a one-off, read-only look at Naukri's profile-edit page, used
                         once to learn how to upload a resume. `profile-refresh` is what runs daily.

They are kept, with their tests, in case that knowledge is needed again, and can be deleted without touching anything else.
(`browser/apply_inspection.py` is NOT here: its network guard is used by live applying.)
"""
