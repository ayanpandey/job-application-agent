"""
Finding the application form on a page nobody has ever looked at.

The external-apply engine used to answer "can we fill this?" with a whitelist of
four ATS vendors matched on the URL. That is correct but tiny. It misses two
whole categories of posting that are not exotic at all:

  - **Embedded boards.** `boards.greenhouse.io/acme` is recognised; the very same
    form served inside `acme.com/careers` as `<iframe id="grnhse_iframe">` is
    not, because the top-level URL is the company's. Greenhouse, Lever and
    SmartRecruiters all ship an embed script, and plenty of companies prefer it
    so the careers page keeps their own branding. The form is identical.
  - **Everything else.** Workable, Recruitee, BambooHR, JazzHR, Personio, Zoho
    Recruit, Freshteam, and the very large tail of hand-rolled `<form>`s on a
    company's own site. None of them need special handling: they are a name, an
    email, a resume upload and a few questions, which is precisely what
    `FormFiller` already reads.

So detection is split in two. A **vendor profile** still exists, because a known
vendor has selectors worth being precise about and quirks worth remembering. But
failing to match one is no longer fatal: the fallback is a *structural* search
that asks whether a container looks like a job application, in every frame on
the page.

The deny-list stays exactly as it was, and is checked first. Workday, Taleo and
iCIMS are not a selector problem -- they need a registered account per employer
tenant, and a run that starts one leaves a half-built profile behind that is
worse for the candidate than never having tried.

Two things this module refuses to do, both deliberately:

  - It does not mutate the DOM to find its target. Scoring happens by reading
    candidate containers, not by tagging them with a data attribute. An
    attribute that only automated visitors write is a free signal, and it would
    be a self-inflicted one.
  - It does not treat a login form or a captcha as something to work around. It
    reports them, and the caller stops. See `detect_bot_wall`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from playwright.async_api import Frame, Locator, Page

from src.core.logger import logger


# ---------------------------------------------------------------------------
# Vendor profiles
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AtsProfile:
    """
    One applicant tracking system we know by name.

    `patterns` match the URL of the page *or of any frame on it*, which is what
    makes an embedded board resolve to its real vendor rather than to the
    generic path. `dom` matches a container the vendor's own markup produces,
    for embeds that proxy the form through a same-origin path and so carry no
    recognisable URL at all.
    """

    name: str
    patterns: Tuple[str, ...]
    form: str
    submit: str
    dom: Tuple[str, ...] = ()


# Ordered: the first pattern that matches wins.
SUPPORTED: Tuple[AtsProfile, ...] = (
    AtsProfile(
        "Greenhouse",
        ("greenhouse.io", "gh_jid", "boards.greenhouse"),
        "form#application_form, form[action*='greenhouse'], main form, form",
        "input[type='submit'], button[type='submit'], "
        "button:has-text('Submit application'), button:has-text('Submit')",
        dom=("#grnhse_app", "#application_form", "[data-mapped='true'][id*='greenhouse']"),
    ),
    AtsProfile(
        "Lever",
        ("lever.co", "jobs.lever"),
        "form.application-form, form[action*='lever'], main form, form",
        "button[type='submit'], button:has-text('Submit application'), button:has-text('Submit')",
        dom=(".application-form", "[data-qa='application-form']"),
    ),
    AtsProfile(
        "Ashby",
        ("ashbyhq.com", "jobs.ashby"),
        "form, div[class*='ApplicationForm']",
        "button[type='submit'], button:has-text('Submit Application'), button:has-text('Submit')",
        dom=("div[class*='ApplicationForm']", "#ashby_embed"),
    ),
    AtsProfile(
        "SmartRecruiters",
        ("smartrecruiters.com", "jobs.smartrecruiters"),
        "form, section[class*='application']",
        "button[type='submit'], button:has-text('Apply'), button:has-text('Submit')",
        dom=("#smartrecruiters-application", "[class*='sr-application']"),
    ),
    AtsProfile(
        "Workable",
        ("workable.com", "apply.workable"),
        "form[data-ui='application-form'], form#application-form, main form, form",
        "button[type='submit'], button[data-ui='submit-application'], button:has-text('Submit')",
        dom=("[data-ui='application-form']", "#workable-application"),
    ),
    AtsProfile(
        "Recruitee",
        ("recruitee.com",),
        "form.c-application-form, main form, form",
        "button[type='submit'], button:has-text('Apply'), button:has-text('Submit')",
        dom=(".c-application-form", "#recruitee-careers"),
    ),
    AtsProfile(
        "BambooHR",
        ("bamboohr.com/careers", "bamboohr.co.uk/careers"),
        "form, div[class*='application']",
        "button[type='submit'], button:has-text('Submit Application'), button:has-text('Submit')",
    ),
    AtsProfile(
        "JazzHR",
        ("applytojob.com", "jazz.co"),
        "form#job_application, main form, form",
        "input[type='submit'], button[type='submit'], button:has-text('Submit')",
    ),
    AtsProfile(
        "Personio",
        ("jobs.personio.com", "personio.de"),
        "form, div[class*='application']",
        "button[type='submit'], button:has-text('Send application'), button:has-text('Submit')",
    ),
    AtsProfile(
        "Zoho Recruit",
        ("zohorecruit.com", "recruit.zoho"),
        "form, div[class*='application']",
        "button[type='submit'], input[type='submit'], button:has-text('Submit')",
    ),
)


# Not a matter of selectors: each of these needs a registered account per
# employer tenant, so an automated attempt leaves an abandoned half-built
# profile behind rather than an application.
KNOWN_UNSUPPORTED: Tuple[Tuple[str, Tuple[str, ...], str], ...] = (
    ("Workday", ("myworkdayjobs.com", "workday.com", "wd1.myworkday", "wd5.myworkday"),
     "Workday needs a registered account per employer"),
    ("Taleo", ("taleo.net", "taleo.com"), "Taleo needs a registered account"),
    ("iCIMS", ("icims.com",), "iCIMS needs a registered account"),
    # Matched on the bare product name, not a TLD: SuccessFactors is served
    # from .com, .eu and .cn depending on the customer's data region, and a
    # pattern pinned to one of them silently lets the others through.
    ("SuccessFactors", ("successfactors", "sapsf.com", "jobs.sap.com"),
     "SAP SuccessFactors needs a registered account"),
    # Lowercase, because these are compared against a lowercased URL -- the
    # vendor spells the path "hcmUI" and a mixed-case pattern can never match.
    ("Oracle Recruiting", ("oraclecloud.com/hcmui", "/candidateexperience/"),
     "Oracle Recruiting Cloud needs a registered account"),
)


# The generic submit control, for a portal with no profile of its own. Ordered
# most specific first: a `type=submit` inside the form we already scoped to is a
# far safer thing to click than anything matched on visible text.
GENERIC_SUBMIT = (
    "button[type='submit'], input[type='submit'], "
    "button:has-text('Submit application'), button:has-text('Submit Application'), "
    "button:has-text('Send application'), button:has-text('Submit'), "
    "button:has-text('Apply now'), button:has-text('Apply')"
)

# Controls that move a multi-page form forward without submitting it. Kept
# distinct from the submit list because clicking the wrong one of these two is
# the difference between a saved draft and a sent application.
GENERIC_NEXT = (
    "button:has-text('Next'), button:has-text('Continue'), "
    "button[aria-label*='Continue'], button[aria-label*='Next'], "
    "button:has-text('Save and continue'), button:has-text('Next step')"
)


def detect_ats(url: str) -> Tuple[Optional[AtsProfile], str]:
    """
    Which ATS a URL belongs to.

    Returns (profile, reason). A None profile with a reason is a deliberate
    skip; a None profile with an empty reason means "not a vendor we know",
    which is now an invitation to try the generic path rather than a refusal.
    """
    low = (url or "").lower()
    if not low:
        return None, "no application URL"

    for name, patterns, why in KNOWN_UNSUPPORTED:
        if any(p in low for p in patterns):
            return None, f"{name}: {why}"

    for ats in SUPPORTED:
        if any(p in low for p in ats.patterns):
            return ats, ""

    return None, ""


def blocked_vendor(urls: Sequence[str]) -> str:
    """
    The deny-list reason for any of `urls`, or "".

    Checked across every frame URL, not just the top-level one. A company
    careers page that embeds Workday in an iframe is still Workday, and the
    top-level URL says nothing about it.
    """
    for url in urls:
        low = (url or "").lower()
        for name, patterns, why in KNOWN_UNSUPPORTED:
            if any(p in low for p in patterns):
                return f"{name}: {why}"
    return ""


# ---------------------------------------------------------------------------
# Structural discovery
# ---------------------------------------------------------------------------


@dataclass
class FormTarget:
    """An application form we are prepared to fill, wherever it turned out to live."""

    frame: Frame
    root: Locator
    vendor: str
    submit: str
    next_control: str
    score: float
    controls: int
    has_file_input: bool

    @property
    def known_vendor(self) -> bool:
        return self.vendor != "Unknown"

    def describe(self) -> str:
        where = "the page" if self.frame.parent_frame is None else "an embedded frame"
        return f"{self.vendor} form in {where} ({self.controls} fields, score {self.score:.0f})"


# Containers worth scoring. `form` first because a real form element is the
# strongest possible evidence; the rest cover ATSs that render a div and post
# over fetch, which is most of the modern ones.
_CANDIDATE_SELECTORS = (
    "form",
    "[role='form']",
    "div[class*='application' i]",
    "div[id*='application' i]",
    "section[class*='application' i]",
    "div[class*='apply' i]",
    "main",
)

# Scored inside the page so a candidate costs one round trip rather than twelve.
_SCORE_JS = r"""
el => {
    const vis = n => {
        if (!n) return false;
        const r = n.getBoundingClientRect();
        if (r.width < 1 && r.height < 1) return false;
        const s = getComputedStyle(n);
        return s.visibility !== 'hidden' && s.display !== 'none';
    };
    const all = sel => Array.from(el.querySelectorAll(sel));

    const text = all("input[type='text'],input[type='email'],input[type='tel'],"
                   + "input[type='url'],input[type='number'],input:not([type]),textarea")
                 .filter(vis);
    const selects  = all('select').filter(vis);
    const radios   = all("input[type='radio']").filter(vis);
    const boxes    = all("input[type='checkbox']").filter(vis);
    // A file input is very often deliberately hidden behind a styled drop zone,
    // so visibility is not required of it -- only that it exists.
    const files    = all("input[type='file']");
    const combos   = all("[role='combobox'],[role='listbox'],[class*='select__control']").filter(vis);
    const editable = all('[contenteditable="true"]').filter(vis);
    const passwords = all("input[type='password']").filter(vis);

    const controls = text.length + selects.length + boxes.length + combos.length
                   + editable.length + new Set(radios.map(r => r.name)).size;

    // Identity fields are what distinguishes an application from a newsletter
    // box: nobody asks for a first name, a phone number and a resume to send a
    // marketing email.
    const hay = (
        all('input,textarea,select,label')
            .map(n => [n.name, n.id, n.getAttribute('autocomplete'), n.innerText]
                        .filter(Boolean).join(' '))
            .join(' ')
    ).toLowerCase().slice(0, 20000);

    const idHits = ['first name','last name','full name','email','phone','resume','cv',
                    'cover letter','linkedin','given-name','family-name','first_name',
                    'last_name']
                   .filter(k => hay.includes(k)).length;

    const submits = all("button[type='submit'],input[type='submit']").filter(vis).length
        + all('button,a').filter(n => vis(n) &&
              /\b(submit|apply|send application)\b/i.test(n.innerText || '')).length;

    const body = (el.innerText || '').toLowerCase().slice(0, 6000);
    const spam = ['subscribe','newsletter','sign up for our','marketing emails']
                 .filter(k => body.includes(k)).length;

    return {
        controls, idHits, submits, spam,
        files: files.length,
        passwords: passwords.length,
        area: el.getBoundingClientRect().width * el.getBoundingClientRect().height,
        depth: (() => { let d = 0, n = el; while ((n = n.parentElement)) d++; return d; })(),
        tag: el.tagName.toLowerCase(),
    };
}
"""


def _score(stats: dict) -> float:
    """
    How much a container looks like a job application.

    Weighted from what actually separates the two things it gets confused with.
    A file input is the single strongest signal, because a newsletter box and a
    site search never have one and an application almost always does. Identity
    fields are next. A visible password field is close to disqualifying: that is
    a sign-in form, and the login-wall path should handle it instead of this one.
    """
    if not stats:
        return 0.0

    score = 0.0
    score += min(stats.get("controls", 0), 25) * 4.0
    score += min(stats.get("idHits", 0), 8) * 9.0
    score += 30.0 if stats.get("files", 0) else 0.0
    score += 12.0 if stats.get("submits", 0) else 0.0
    score -= stats.get("spam", 0) * 15.0
    score -= stats.get("passwords", 0) * 40.0

    # A <form> is meaningfully better evidence than a <main> that happens to
    # wrap one, and preferring the tighter container keeps a site's navigation
    # and cookie banner out of the field sweep.
    if stats.get("tag") == "form":
        score += 18.0
    elif stats.get("tag") == "main":
        score -= 10.0

    return score


# Below this a container is not an application form, and filling it would mean
# typing a candidate's phone number into whatever it actually is. Calibrated so
# that the smallest genuine form -- name, email, resume, submit -- clears it
# with room to spare, while a newsletter signup or a site search cannot.
MIN_FORM_SCORE = 70.0


def _plausible_application(stats: dict) -> bool:
    """
    A floor the score alone does not enforce.

    Scoring is a weighted sum, so a high enough total can be reached without the
    evidence that actually matters. The case this exists for is a contact form
    that accepts an attachment: name, email, message and a file input scores
    about 94, comfortably over the threshold, and it is not a job application.

    Two identity hits is the whole rule, and it separates those two cleanly
    because of what counts as a hit. A contact form reaches exactly one --
    "email"; "name" on its own is deliberately not in the list, only "first
    name" / "last name" / "full name" are. A job application reaches at least
    three, because "resume" and "cv" are hits too and it is the rare application
    that does not mention one. Anything asking for a password is a sign-in, and
    the login-wall path owns that case.
    """
    if stats.get("passwords", 0):
        return False
    return stats.get("idHits", 0) >= 2


async def _score_candidates(frame: Frame) -> List[Tuple[float, Locator, dict]]:
    """Every plausible application container in one frame, best first."""
    found: List[Tuple[float, Locator, dict]] = []
    seen_signatures: set = set()

    for selector in _CANDIDATE_SELECTORS:
        try:
            handles = await frame.locator(selector).all()
        except Exception:
            continue

        # A page with fifty divs matching "apply" is a listing page, not an
        # application; scoring all of them would cost fifty round trips to
        # discover that. The genuine article is always among the first few.
        for handle in handles[:8]:
            try:
                stats = await handle.evaluate(_SCORE_JS)
            except Exception:
                continue
            if not stats:
                continue

            # Nested candidates (a form inside a main) produce identical stats;
            # keep the first, which is the tighter container by selector order.
            signature = (
                stats.get("controls"), stats.get("idHits"),
                stats.get("files"), round(stats.get("area", 0) or 0),
            )
            if signature in seen_signatures:
                continue
            seen_signatures.add(signature)

            found.append((_score(stats), handle, stats))

    found.sort(key=lambda row: row[0], reverse=True)
    return found


async def find_application_form(
    page: Page, prefer: Optional[AtsProfile] = None
) -> Optional[FormTarget]:
    """
    The application form on this page, in whatever frame it lives in.

    Frames are searched in document order with the main frame first, and the
    highest-scoring container across all of them wins. `prefer` is the vendor
    profile matched from the URL, if there was one; its selectors are tried
    before the structural search, because a vendor we know is one whose form
    element we can name exactly rather than infer.
    """
    frames = _live_frames(page)

    # 1. A named vendor, by its own selectors, in any frame.
    if prefer is not None:
        for frame in frames:
            target = await _match_profile(frame, prefer)
            if target is not None:
                return target

    # 2. A vendor recognised by its markup rather than its URL -- an embed that
    #    proxies the form through the company's own domain.
    for frame in frames:
        for profile in SUPPORTED:
            if not profile.dom:
                continue
            for selector in profile.dom:
                try:
                    handle = frame.locator(selector).first
                    if await handle.count() == 0:
                        continue
                except Exception:
                    continue
                target = await _match_profile(frame, profile)
                if target is not None:
                    logger.info(f"Recognised an embedded {profile.name} form by its markup.")
                    return target

    # 3. Structural search. This is the path that makes a site nobody has
    #    configured work at all.
    best: Optional[FormTarget] = None
    for frame in frames:
        for score, handle, stats in await _score_candidates(frame):
            if score < MIN_FORM_SCORE:
                break  # sorted, so nothing after this clears the bar either
            # Keep looking rather than giving up on the frame. The
            # highest-scoring container is not always the application: a page
            # whose contact form outscores a short application form should end
            # up filling the application, not neither.
            if not _plausible_application(stats):
                continue
            candidate = FormTarget(
                frame=frame, root=handle, vendor="Unknown",
                submit=GENERIC_SUBMIT, next_control=GENERIC_NEXT,
                score=score, controls=int(stats.get("controls", 0)),
                has_file_input=bool(stats.get("files", 0)),
            )
            if best is None or candidate.score > best.score:
                best = candidate
            break  # the best plausible candidate in this frame; list is sorted

    return best


async def _match_profile(frame: Frame, profile: AtsProfile) -> Optional[FormTarget]:
    """A vendor's own form selector, scored so a stale selector cannot match junk."""
    try:
        handle = frame.locator(profile.form).first
        if await handle.count() == 0:
            return None
        stats = await handle.evaluate(_SCORE_JS)
    except Exception:
        return None

    score = _score(stats or {})
    if score < MIN_FORM_SCORE or not _plausible_application(stats or {}):
        # The vendor's selector matched something that is not an application
        # form -- most often because the page has not finished rendering, or
        # because the selector's `form` fallback caught a search box. Falling
        # through to the structural search is better than filling it.
        return None

    return FormTarget(
        frame=frame, root=handle, vendor=profile.name,
        submit=profile.submit, next_control=GENERIC_NEXT,
        score=score, controls=int((stats or {}).get("controls", 0)),
        has_file_input=bool((stats or {}).get("files", 0)),
    )


def _live_frames(page: Page) -> List[Frame]:
    """Main frame first, then children, skipping any that have detached."""
    out: List[Frame] = []
    try:
        for frame in page.frames:
            try:
                if frame.is_detached():
                    continue
            except Exception:
                continue
            out.append(frame)
    except Exception:
        try:
            out = [page.main_frame]
        except Exception:
            return []

    out.sort(key=lambda f: 0 if f.parent_frame is None else 1)
    return out


def frame_urls(page: Page) -> List[str]:
    """Every frame URL on the page, for the deny-list check."""
    urls: List[str] = []
    for frame in _live_frames(page):
        try:
            if frame.url:
                urls.append(frame.url)
        except Exception:
            continue
    return urls


# ---------------------------------------------------------------------------
# Walls
# ---------------------------------------------------------------------------


# Each entry is (name, selectors). Matched across every frame, because a captcha
# is nearly always served in one of its own.
#
# The selectors are deliberately narrower than "anything captcha-shaped", and
# this is the difference between a working agent and one that refuses to apply
# anywhere. Greenhouse and Lever both ship **invisible reCAPTCHA v3** on
# ordinary application forms: the page carries a `data-sitekey`, a
# `recaptcha/api2/anchor` iframe and a visible `.grecaptcha-badge` in the
# corner, and *nothing is ever asked of the user*. An earlier version of this
# list matched `[data-sitekey]` and any `iframe[src*='recaptcha']`, which meant
# the two most common ATSs in the world would both have been reported as
# challenges and every application to them abandoned.
#
# So what counts is a widget the user must actually interact with:
#   - `api2/bframe` -- the image-grid challenge, only present while challenging
#   - an explicitly rendered `.g-recaptcha` / `.h-captcha` checkbox widget
# and never the badge, which is excluded by name below.
_CAPTCHA_SIGNS: Tuple[Tuple[str, str], ...] = (
    ("reCAPTCHA", "iframe[src*='recaptcha/api2/bframe'], "
                  "iframe[title*='recaptcha challenge' i], .g-recaptcha, #g-recaptcha"),
    ("hCaptcha", "iframe[src*='hcaptcha'], .h-captcha, #h-captcha"),
    ("Cloudflare Turnstile", "iframe[src*='challenges.cloudflare.com'], .cf-turnstile"),
    ("Cloudflare challenge", "#challenge-running, #cf-challenge-running, #challenge-form"),
    ("Arkose/FunCaptcha", "iframe[src*='arkoselabs'], #funcaptcha, #FunCaptcha"),
    ("DataDome", "iframe[src*='captcha-delivery.com'], #datadome-captcha"),
    ("PerimeterX", "#px-captcha, iframe[src*='perimeterx']"),
)

# A challenge widget is a thing you can see and click. Anything smaller than
# this is a tracking pixel, a hidden container or a 0x0 iframe -- all of which
# an invisible captcha leaves lying around on a perfectly ordinary form.
_MIN_CHALLENGE_WIDTH = 70
_MIN_CHALLENGE_HEIGHT = 40

_WALL_TEXT = re.compile(
    r"verify (?:that )?you(?:'| a)?re (?:a )?human"
    r"|are you a robot"
    r"|unusual traffic"
    r"|enable javascript and cookies to continue"
    r"|checking your browser",
    re.I,
)

_LOGIN_TEXT = re.compile(
    r"sign in to apply|log ?in to apply|create an account to apply"
    r"|you must be signed in|please sign in|register to apply",
    re.I,
)


async def detect_bot_wall(page: Page) -> str:
    """
    Whatever is standing between us and the form, or "".

    Returns a human-readable reason so the caller can log why it stopped.
    Nothing here attempts to get past any of it: a captcha means the site has
    asked for a human, and the honest response is to hand it to one. Solving it
    would also be the wrong engineering call even setting that aside -- a
    challenge that fires once will fire again, and an account that answers them
    automatically is a stronger signal than the one that triggered the challenge.
    """
    # Capped, and both passes share one frame walk. A page carrying a dozen ad
    # and analytics iframes would otherwise spend a `body.inner_text` timeout on
    # each of them before deciding the form was fine all along.
    frames = _live_frames(page)[:12]

    for frame in frames:
        for name, selector in _CAPTCHA_SIGNS:
            # A sitekey in the DOM is not the same as a challenge on screen, and
            # neither is the badge an invisible reCAPTCHA leaves in the corner.
            # _any_visible is what draws that line; counting matches first would
            # only cost a round trip without changing the answer.
            try:
                if await _any_visible(frame, selector):
                    return f"{name} challenge on the page"
            except Exception:
                continue

    for frame in frames:
        try:
            body = await frame.locator("body").first.inner_text(timeout=2000)
        except Exception:
            continue
        if _WALL_TEXT.search(body or ""):
            return "an anti-bot interstitial is showing"

    return ""


async def detect_login_wall(page: Page, target: Optional[FormTarget]) -> str:
    """
    Whether applying here requires an account first.

    Two independent signals, because either alone has a false positive: a
    visible password field (a form that asks for one is a sign-up, not an
    application) and a page that says so in words. A page carrying both an
    application form and a sign-in link is fine -- that is every ATS -- so a
    password field only counts when it sits inside the form we intend to fill.
    """
    if target is not None:
        try:
            if await target.root.locator("input[type='password']:visible").count() > 0:
                return "the form asks for a password (account creation, not an application)"
        except Exception:
            pass

    if target is None:
        for frame in _live_frames(page):
            try:
                body = await frame.locator("body").first.inner_text(timeout=3000)
            except Exception:
                continue
            if _LOGIN_TEXT.search(body or ""):
                return "the portal requires a sign-in before applying"

    return ""


async def _any_visible(frame: Frame, selector: str) -> bool:
    """
    Whether any match is a challenge a person would have to answer.

    `is_visible()` alone is not enough and was the bug: reCAPTCHA's invisible
    badge is genuinely visible -- it is the little logo in the corner of the
    page -- so a plain visibility test reports a challenge on every Greenhouse
    and Lever form in existence. A real challenge has a real size and does not
    live inside the badge.
    """
    try:
        return bool(await frame.locator(selector).evaluate_all(
            """(nodes, limits) => nodes.slice(0, 8).some(n => {
                if (n.closest('.grecaptcha-badge')) return false;
                const s = getComputedStyle(n);
                if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0')
                    return false;
                const r = n.getBoundingClientRect();
                return r.width >= limits.w && r.height >= limits.h;
            })""",
            {"w": _MIN_CHALLENGE_WIDTH, "h": _MIN_CHALLENGE_HEIGHT},
        ))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Consent banners
# ---------------------------------------------------------------------------

# A cookie banner is not a detection problem, it is a physical one: it overlays
# the submit control, so an organic click lands on the banner instead and the
# application silently never sends. Accepting is also what essentially every
# visitor does, so it costs nothing in realism.
_CONSENT_BUTTONS = (
    "#onetrust-accept-btn-handler",
    "button#hs-eu-confirmation-button",
    "button[aria-label*='Accept all' i]",
    ".cc-allow, .cookie-accept, #accept-cookies",
    "button:has-text('Accept all')",
    "button:has-text('Accept All Cookies')",
    "button:has-text('I accept')",
    "button:has-text('Got it')",
)


async def dismiss_consent_banner(page: Page, click) -> bool:
    """
    Clears a cookie banner if one is covering the page.

    `click` is injected rather than imported so this module stays free of the
    behaviour layer and remains testable without a browser.
    """
    for selector in _CONSENT_BUTTONS:
        for frame in _live_frames(page):
            try:
                handle = frame.locator(selector).first
                if await handle.count() == 0 or not await handle.is_visible():
                    continue
            except Exception:
                continue
            try:
                await click(page, handle)
                logger.debug(f"Dismissed a consent banner via {selector}.")
                return True
            except Exception:
                continue
    return False
