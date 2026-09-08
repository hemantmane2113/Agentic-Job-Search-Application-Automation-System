"""
Naukri CSS selectors and URLs — the ONLY file in this codebase that
should contain a raw selector string. Every other module in browser/
refers to a selector by name from here; a site change should only
ever require editing this file.

***************************** PARTIALLY VERIFIED ***********************
Most selectors below are still best-effort placeholders based on
common patterns for job-portal login/profile/search pages, NOT
confirmed against Naukri's actual DOM — this codebase has no live
network access to naukri.com and no real account to test against.
Six selectors (see their VERIFIED comments below) were confirmed
against a real Stage 1 inspection capture on 2026-09-08 and updated
accordingly; treat every entry still marked UNVERIFIED the same as
before.

To verify remaining selectors for real:
  1. Run `naukri-agent inspect` against your own account, locally,
     where you have real browser + network access.
  2. It saves HTML snapshots and screenshots to
     settings.inspection_output_dir (default ./inspection_output/).
  3. Compare the saved HTML against the selectors below and update
     this file with what you actually find. Nothing else in browser/
     needs to change.

Until a given selector is marked VERIFIED, treat its Stage 1 read
result as unreliable — a selector silently matching nothing is
indistinguishable from "that element genuinely isn't there" without a
human checking the saved HTML.
**************************************************************************
"""

# --- Login ---
LOGIN_URL = "https://www.naukri.com/nlogin/login"

LOGIN_EMAIL_INPUT = "#usernameField"  # UNVERIFIED
LOGIN_PASSWORD_INPUT = "#passwordField"  # UNVERIFIED
LOGIN_SUBMIT_BUTTON = "button[type='submit']"  # UNVERIFIED

CAPTCHA_INDICATOR = "iframe[src*='captcha'], .g-recaptcha, #captcha"  # UNVERIFIED
MFA_INDICATOR = "input[name*='otp'], input[placeholder*='OTP']"  # UNVERIFIED
LOGIN_SUCCESS_URL_FRAGMENT = "naukri.com/mnjuser"  # UNVERIFIED

# --- Profile / resume ---
PROFILE_URL = "https://www.naukri.com/mnjuser/profile"

# VERIFIED 2026-09-08 against a real profile page capture (see
# inspection_output/20260909_013616/02_profile.html). Confirmed
# structure: filename lives in a <div title="..." class="... exten">
# inside .resume-name-inline; last-updated text is in .updateOn;
# the resume file input has id="attachCV" (there is also an unrelated
# profile-photo file input elsewhere on the page, so the id-scoped
# selector matters — a bare input[type='file'] would match either);
# the delete control has no distinguishing class, only a
# data-title="delete-resume" attribute.
RESUME_FILENAME = ".resume-name-inline .exten"
RESUME_LAST_UPDATED = ".updateOn"
RESUME_UPLOAD_BUTTON = "#attachCV"
RESUME_REMOVE_BUTTON = "[data-title='delete-resume']"

# --- Job search ---
SEARCH_URL_TEMPLATE = "https://www.naukri.com/{query}-jobs-in-{location}"  # UNVERIFIED

# VERIFIED 2026-09-08 against a real search-results page capture (see
# inspection_output/20260909_013616/03_search_results.html). The
# previous JOB_CARD value never matched anything: real job cards use
# lowercase "job-tuple"/"jobtuple" class names (case-sensitive
# attribute-substring matching means "jobTuple" never matched) inside
# a <div>, never an <article>.
JOB_CARD = ".srp-jobtuple-wrapper[data-job-id]"
JOB_CARD_TITLE = "a[class*='title']"  # UNVERIFIED — untested; likely correct (real title anchor has class="title"), but not confirmed against a matched JOB_CARD yet
JOB_CARD_COMPANY = "a.comp-name"
JOB_CARD_LOCATION = "[class*='location']"  # UNVERIFIED — untested; likely correct (matches "...-srp-location" class), but not confirmed against a matched JOB_CARD yet

# --- Job listing / apply workflow ---
APPLY_BUTTON = "#apply-button, button[class*='apply']"  # UNVERIFIED
RESUME_SELECTION_CONTROLS = "[class*='resume'][class*='modal'], [class*='chooseResume']"  # UNVERIFIED

# --- Authentication state (any authenticated page) ---
# VERIFIED 2026-09-08 against real captures of the post-login,
# profile, and search-results pages (all three showed this element).
# This is the profile avatar in the persistent global navigation
# header, present on every authenticated page regardless of which
# specific page it is — a far more robust "am I logged in right now"
# signal than any single page's URL, which is why login.py's
# is_authenticated() check uses this rather than a URL fragment.
AUTHENTICATED_NAV_INDICATOR = "img.nI-gNb-icon-img[alt='naukri user profile image']"
