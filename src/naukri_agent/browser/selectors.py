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

# Indicators of an ACTIVE, human-solvable CAPTCHA challenge on the page.
# Deliberately does NOT include a bare `.g-recaptcha`: Naukri's normal
# login page ALWAYS ships a static, non-interactive
# `<div class="g-recaptcha" data-sitekey="..." data-size="invisible">`
# container even when no challenge is presented — confirmed against a
# real login-page capture (inspection_output/apply_20260909_115708/
# challenge_detected.html and .../apply_20260909_113420/). Matching that
# container produced a false-positive NaukriCaptchaError on every
# fresh-profile login. A genuine challenge instead surfaces as a
# challenge iframe (generic captcha, or reCAPTCHA's api2/bframe image
# grid), an explicit #captcha, or a `.g-recaptcha` rendered NOT
# `data-size="invisible"` (i.e. an actual visible widget). login.py's
# _captcha_challenge_present() also inspects `data-size` directly as a
# belt-and-suspenders check. The challenge-iframe forms are still
# UNVERIFIED (no real challenge has been captured yet).
CAPTCHA_INDICATOR = (
    "iframe[src*='captcha'], "
    "iframe[src*='recaptcha/api2/bframe'], "
    "iframe[title*='recaptcha challenge'], "
    "#captcha, "
    ".g-recaptcha:not([data-size='invisible' i])"
)  # PARTIALLY VERIFIED (invisible-container false positive confirmed fixed)

# The always-present static reCAPTCHA container on Naukri's login page.
# Presence alone is meaningless — login.py checks its `data-size`.
RECAPTCHA_STATIC_CONTAINER = ".g-recaptcha"

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

# PARTIALLY VERIFIED 2026-09-09 against a real search-results capture
# (inspection_output/20260909_023839/03_search_results.html): every job
# card carries its posted-date label in `<span class="job-post-day ">`
# with relative text such as "Just now", "Few hours ago", "Today",
# "1 Day Ago", "3 Days Ago", "30+ Days Ago". Read (never clicked) by the
# deterministic freshness-first discovery gate in
# orchestration/discovery.py — no LLM. The alternate class-substring form
# covers the `cust-job-tuple` / `lay-4` layout variants that also use it.
JOB_CARD_POSTED = "span.job-post-day, span[class*='job-post-day']"

# --- Job listing / apply workflow ---
# PARTIALLY VERIFIED 2026-09-09 against a real job-listing capture
# (inspection_output/20260909_023839/04_job_listing.html): the pre-click
# page contains exactly `<button id="apply-button" class="styles_apply-
# button__uJI3A apply-button">Apply</button>` — TWICE (duplicate id: once
# in the header card's `.styles_jhc__apply-button-container__5Bqnb`, once
# in the scroll-activated sticky header). `#apply-button` matches the
# header-card one first. The button carries no href / onclick / data-*;
# its behaviour is JS-bound and NOT determinable from static HTML. Left
# marked UNVERIFIED because the CLICK behaviour (what the apply flow
# does) is still unknown — Stage 1.5 (`inspect-apply`) exists to observe
# that safely, without ever clicking it from automation.
APPLY_BUTTON = "#apply-button, button[class*='apply']"  # UNVERIFIED (presence seen; click behaviour unknown)

# UNVERIFIED. The pre-click capture proved these controls do NOT exist
# before Apply is clicked (empty `<div id="chatbot-container"></div>` is
# the only apply-related mount point). Kept for Stage 1's read-only
# `inspect` step; Stage 1.5 uses APPLICATION_ROOT_CANDIDATES instead.
RESUME_SELECTION_CONTROLS = "[class*='resume'][class*='modal'], [class*='chooseResume']"  # UNVERIFIED

# --- Stage 1.5: dynamically-rendered post-Apply UI (READ-ONLY observation) ---
# Tried in order by browser/apply_inspection.py; the FIRST that both
# exists AND has non-empty content is treated as the application root
# whose outerHTML + controls get captured. All UNVERIFIED: the pre-click
# capture only proved `#chatbot-container` is the (then-empty) mount
# point. NOTHING in this list is ever clicked — it is only queried.
APPLICATION_ROOT_CANDIDATES = (
    "#chatbot-container",  # confirmed present (empty) pre-click; likely the populated root post-click
    "div[class*='chatbot']",
    "div[class*='Drawer'][class*='chatbot']",
    "div[class*='drawer'][class*='apply']",
    "[role='dialog']",
)

# Generic HTML/ARIA structure probes for the post-Apply capture (NOT
# Naukri-specific; read-only, never interacted with). Kept here so the
# "selectors live in one file" rule holds.
FRAME_SELECTOR = "iframe, frame"
DIALOG_SELECTOR_CANDIDATES = (
    "[role='dialog']",
    "[role='alertdialog']",
    "[aria-modal='true']",
    "dialog",
)

# --- Job listing DETAIL page (READ-ONLY public listing content) ---
# For the daily match digest: read a job's title / company / location /
# experience / salary / posted text / full description. This is public
# listing content, NOT the application workflow.
#
# PRIMARY SOURCE: the server-rendered schema.org JobPosting block below
# (JOB_DETAIL_LD_JSON). It is present in the initial HTML before client
# hydration and its shape is a public standard, so browser/jobs.
# fetch_job_detail parses it first. The hashed CSS-module selectors that
# follow are a per-field FALLBACK for the visible DOM; their suffixes
# (seen in inspection_output/20260909_023839/04_job_listing.html,
# 2026-09-09) change on Naukri redeploys, so each also has a structural
# alternative. Every field degrades to None independently; never raises.
#
# Live run 1 (2026-09-10) failed because fetch_job_detail read the page
# before it rendered and had no fallback: all five jobs came back empty
# and collapsed to one content fingerprint. The ld+json primary + the
# JOB_DETAIL_READY wait below fix that.
JOB_DETAIL_LD_JSON = 'script[type="application/ld+json"]'

# A cheap "the JD has actually rendered" signal to wait for (bounded;
# a timeout still degrades gracefully) before reading fields. Any one
# of: the structured-data block, the title, or the description body.
JOB_DETAIL_READY = (
    'script[type="application/ld+json"], '
    "h1.styles_jd-header-title__rZwM1, h1[title], "
    "section[class*='job-desc'], div[class*='dang-inner-html']"
)

JOB_DETAIL_TITLE = "h1.styles_jd-header-title__rZwM1, header h1, h1[title]"
JOB_DETAIL_COMPANY = (
    ".styles_jd-header-comp-name__MvqAI a, "
    "div[class*='jd-header-comp-name'] a, a.comp-name"
)
JOB_DETAIL_LOCATION = (
    ".styles_jhc__location__W_pVs, .styles_jhc__loc___Du2H, "
    "div[class*='jhc__loc'], span[class*='location']"
)
JOB_DETAIL_EXPERIENCE = (
    ".styles_jhc__exp__k_giM, div[class*='jhc__exp'] span, span[class*='exp']"
)
JOB_DETAIL_SALARY = (
    ".styles_jhc__salary__jdfEC, div[class*='jhc__salary'] span, "
    "div[class*='exp-salary'] span[class*='salary'], span[class*='salary']"
)
JOB_DETAIL_STATS = ".styles_jhc__jd-stats__KrId0, div[class*='jd-stats']"
JOB_DETAIL_DESCRIPTION = (
    ".styles_JDC__dang-inner-html__h0K4t, "
    "section[class*='job-desc-container'] div[class*='dang-inner-html'], "
    "div[class*='job-desc'], .dang-inner-html"
)

# --- Key Skills chip widget --------------------------------------------
#
# UNVERIFIED — evidenced from exactly ONE real capture
# (inspection_output/20260909_023839/04_job_listing.html, 2026-09-11
# investigation), never confirmed via a live `naukri-agent inspect` run
# and never checked against a second real job page. A genuinely distinct
# DOM section, sibling to the Education/"other details" blocks — NOT
# part of JOB_DETAIL_DESCRIPTION's dang-inner-html JD body, so it is
# never accidentally duplicated into `description`. Its own on-page
# legend reads "Skills highlighted with [icon] are preferred keyskills":
# chips carrying <i class="ni-icon-jd-save"> are Naukri's own explicit
# "preferred" tag; chips without it are the widget's baseline/default
# (required) entries — Naukri's own framing, read verbatim, not our
# inference. Run `inspect` against a live job page before trusting this
# in production, same as every other UNVERIFIED selector in this file.
KEY_SKILLS_CONTAINER = "div[class*='key-skill']"  # UNVERIFIED
KEY_SKILLS_CHIP = "a[class*='chip']"  # UNVERIFIED — scoped within KEY_SKILLS_CONTAINER
# ni-icon-jd-save is a semantic icon-font class (same human-readable
# naming family as ni-icon-facebook / ni-icon-twitter / ni-icon-linkedin
# seen elsewhere on the same real capture), not a build-hashed
# styles_xxx__HASH class — comparatively lower redeploy-drift risk than
# the two selectors immediately above, though still UNVERIFIED live.
KEY_SKILLS_PREFERRED_ICON = "i.ni-icon-jd-save"  # UNVERIFIED (evidenced, not hashed)

# --- Authentication state (any authenticated page) ---
# VERIFIED 2026-09-08 against real captures of the post-login,
# profile, and search-results pages (all three showed this element).
# This is the profile avatar in the persistent global navigation
# header, present on every authenticated page regardless of which
# specific page it is — a far more robust "am I logged in right now"
# signal than any single page's URL, which is why login.py's
# is_authenticated() check uses this rather than a URL fragment.
AUTHENTICATED_NAV_INDICATOR = "img.nI-gNb-icon-img[alt='naukri user profile image']"
