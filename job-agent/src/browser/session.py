"""
Browser session manager for Playwright with persistent context, stealth, and session state.

Fingerprint philosophy: *authenticity over spoofing*. Every value we do not need to
change is left at the browser's genuine value, because a genuine value can never
contradict another genuine value. We only rewrite the two things that actually leak
automation on a stock Playwright build:

  1. The "Chrome for Testing" / "HeadlessChrome" brand strings, which appear in both
     the User-Agent header and the Sec-CH-UA client hints.
  2. navigator.webdriver, plus a native-looking Function.prototype.toString so that
     any patched accessor cannot be unmasked by descriptor introspection.

Crucially, the UA string and the Sec-CH-UA client hints are derived from the *running*
browser at runtime, so they can never disagree about the Chrome version. Hardcoding a
version is worse than not spoofing at all: it guarantees a mismatch the moment the
bundled Chromium is upgraded, and a UA/client-hint disagreement is a deterministic,
single-request bot signal at the edge gateway.
"""

import asyncio
import os
import random
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from playwright.async_api import async_playwright, BrowserContext, Page, Playwright
from playwright_stealth import Stealth

from src.core import paths
from src.core.config import BrowserConfig
from src.core.logger import logger, console
from src.browser.utils import human_delay


# Brand strings that unmask an automated build, mapped to their retail equivalent.
_BRAND_REWRITES = {
    "Google Chrome for Testing": "Google Chrome",
    "Chrome for Testing": "Google Chrome",
    "HeadlessChrome": "Google Chrome",
}


def _sanitize_ua(ua: str) -> str:
    """Strips automation build markers out of a genuine User-Agent, preserving the version."""
    cleaned = ua.replace("HeadlessChrome", "Chrome")
    # "Google Chrome for Testing" never appears in the UA token itself, but guard anyway.
    cleaned = cleaned.replace("Chrome for Testing", "Chrome")
    return cleaned


def _sanitize_brands(brands: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """
    Rewrites automation brand names and guarantees a retail "Google Chrome" entry,
    while preserving the browser's own GREASE brand, ordering and version values.

    Retail Chrome always advertises three brands: Chromium, Google Chrome, and a
    GREASE brand. The bundled build advertises only Chromium + GREASE, which marks
    the visitor as a raw Chromium user -- a small population that correlates strongly
    with automation, and one LinkedIn has no reason to see from a job seeker.
    """
    out: List[Dict[str, str]] = []
    chromium_version = ""

    for entry in brands or []:
        brand = entry.get("brand", "")
        version = entry.get("version", "")
        for needle, replacement in _BRAND_REWRITES.items():
            if needle in brand:
                brand = replacement
                break
        if brand == "Chromium":
            chromium_version = version
        out.append({"brand": brand, "version": version})

    has_chrome = any(b["brand"] == "Google Chrome" for b in out)
    if not has_chrome and chromium_version:
        # Sit next to the Chromium entry, matching how retail Chrome orders them.
        insert_at = next(
            (i + 1 for i, b in enumerate(out) if b["brand"] == "Chromium"),
            len(out),
        )
        out.insert(insert_at, {"brand": "Google Chrome", "version": chromium_version})

    # Collapse the duplicate that appears if the build already had a rewritten entry.
    seen = set()
    deduped: List[Dict[str, str]] = []
    for b in out:
        key = (b["brand"], b["version"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(b)
    return deduped


def _format_sec_ch_ua(brands: List[Dict[str, str]]) -> str:
    """Serializes a brand list into the Sec-CH-UA structured header format."""
    return ", ".join(f'"{b["brand"]}";v="{b["version"]}"' for b in brands)


def build_stealth() -> Stealth:
    """
    Builds a Stealth instance with every evasion off that a *headed* browser does not need.

    This list is not a guess; it is the result of diffing this session against an
    unmodified headed Chromium launched from the same binary (see verify_stealth.py).
    The finding was that in headed mode almost every evasion is a net negative: the
    genuine browser already reports the correct value, so the evasion replaces a native
    accessor with a JavaScript one whose .name is wrong and whose toString() leaks its
    own source. Measured on the control:

      * navigator.languages is already ("en-US",) -- and it must stay that way, because
        the override appends "en" while the Accept-Language header (set from `locale`)
        still says just en-US. The pair then contradicts each other.
      * navigator.vendor is already "Google Inc.".
      * navigator.plugins already lists the five genuine PDF entries.
      * navigator.hardwareConcurrency already reports the real core count; a canned
        value would contradict the machine it is running on.
      * window.chrome already carries app, csi and loadTimes natively.
      * navigator.permissions is already coherent.

    The defaults are additionally tuned for a Windows target and contradict macOS:
    navigator_platform_override is "Win32" under a "Macintosh; Intel Mac OS X" UA, and
    webgl_vendor spoofs "Intel Inc." on an Apple Silicon host.

    What remains enabled is only what a headed browser does NOT already get right:
    media codecs, the hairline-feature probe, iframe contentWindow, and the Error
    stack shape. navigator.webdriver is handled in apply_stealth_layer instead, where
    the patched getter can be given a native name and toString.
    """
    return Stealth(
        # Would contradict the real host.
        navigator_platform=False,
        navigator_platform_override=None,
        webgl_vendor=False,
        # Owned by apply_fingerprint / apply_stealth_layer so the values stay in lockstep.
        navigator_user_agent=False,
        navigator_user_agent_data=False,
        sec_ch_ua=False,
        navigator_webdriver=False,
        # Already genuine in a headed browser; patching them only adds a JS accessor.
        navigator_languages=False,
        navigator_languages_override=None,
        navigator_vendor=False,
        navigator_plugins=False,
        navigator_hardware_concurrency=False,
        navigator_permissions=False,
        chrome_app=False,
        chrome_csi=False,
        chrome_load_times=False,
        chrome_runtime=False,
    )


async def probe_build_version(context: BrowserContext, page: Page) -> Optional[str]:
    """Reads the exact browser build (e.g. '151.0.7922.34') over CDP, without navigating."""
    if not context or not page:
        return None
    session = None
    try:
        session = await context.new_cdp_session(page)
        info = await session.send("Browser.getVersion")
        product = info.get("product", "")
        match = re.search(r"/([\d.]+)$", product)
        if match:
            return match.group(1)
    except Exception as e:
        logger.debug(f"CDP version probe unavailable: {e}")
    finally:
        if session is not None:
            try:
                await session.detach()
            except Exception:
                pass
    return None

async def apply_fingerprint(context: BrowserContext, page: Page) -> Optional[Dict[str, Any]]:
    """
    Reads the running browser's genuine identity and rewrites only the automation
    brand markers, pushing the result into the HTTP client hints.

    Returns the cleaned identity for apply_stealth_layer to install on the JS surface,
    or None when the browser is already retail-consistent and nothing needs patching.

    The JS half is deliberately NOT installed here. Init scripts run in the order they
    are registered, so a script added at this point would execute *before* the one that
    installs the native-looking Function.prototype.toString -- leaving every accessor it
    defines with a toString() that returns its own JavaScript source. Handing the values
    to apply_stealth_layer instead means all the patching happens inside a single script,
    where the native-masking helper is already in scope.
    """
    if not context or not page:
        return None

    try:
        identity = await page.evaluate(
            """async () => {
                const uad = navigator.userAgentData;
                let fullVersionList = [];
                if (uad) {
                    try {
                        const hev = await uad.getHighEntropyValues(['fullVersionList']);
                        fullVersionList = (hev.fullVersionList || []).map(b => ({brand: b.brand, version: b.version}));
                    } catch (e) {}
                }
                return {
                    ua: navigator.userAgent,
                    brands: uad ? uad.brands.map(b => ({brand: b.brand, version: b.version})) : [],
                    fullVersionList: fullVersionList,
                    mobile: uad ? uad.mobile : false,
                    platform: uad ? uad.platform : 'macOS',
                };
            }"""
        )
    except Exception as e:
        logger.debug(f"Could not read browser identity, skipping fingerprint alignment: {e}")
        return None

    real_ua: str = identity.get("ua", "")
    clean_ua = _sanitize_ua(real_ua)
    platform = identity.get("platform") or "macOS"
    mobile = bool(identity.get("mobile"))

    version_match = re.search(r"Chrome/(\d+)", clean_ua)
    detected_major = version_match.group(1) if version_match else None

    # navigator.userAgentData.brands is empty on about:blank, so the low-entropy
    # list is derived from fullVersionList (which is populated there) by truncating
    # each version to its major. Sending an empty Sec-CH-UA would be far louder than
    # sending none at all, so we only touch the header once we have a real list.
    observed_brands = [b for b in (identity.get("brands") or []) if b.get("version")]
    observed_full = [b for b in (identity.get("fullVersionList") or []) if b.get("version")]

    # navigator.userAgentData is gated behind a secure context, so on the about:blank
    # start page both lists come back empty. The real build number is still reachable
    # over CDP, which needs no navigation and no network -- important, because reading
    # identity from a live page would mean the very first request went out unaligned.
    build_version = await probe_build_version(context, page)

    if observed_full:
        clean_full = _sanitize_brands(observed_full)
    elif build_version:
        clean_full = [
            {"brand": "Chromium", "version": build_version},
            {"brand": "Google Chrome", "version": build_version},
            {"brand": "Not=A?Brand", "version": "99.0.0.0"},
        ]
    else:
        clean_full = []

    if observed_brands:
        clean_brands = _sanitize_brands(observed_brands)
    else:
        clean_brands = [
            {"brand": b["brand"], "version": b["version"].split(".")[0]}
            for b in clean_full
        ]

    if not detected_major and build_version:
        detected_major = build_version.split(".")[0]

    needs_rewrite = (
        clean_ua != real_ua
        or clean_brands != observed_brands
        or clean_full != observed_full
    )

    if not needs_rewrite:
        # This is the good case, and it is what a real Google Chrome install produces:
        # nothing is patched at all, so there is no patched accessor to unmask.
        logger.info(
            f"[green]✓ Browser identity is already retail-consistent (Chrome {detected_major}); "
            f"no fingerprint patching needed.[/green]"
        )
        return None

    # 1. HTTP layer: UA header and client hints, all at the real running version.
    headers = {"User-Agent": clean_ua}
    if clean_brands:
        headers["sec-ch-ua"] = _format_sec_ch_ua(clean_brands)
        headers["sec-ch-ua-mobile"] = "?1" if mobile else "?0"
        headers["sec-ch-ua-platform"] = f'"{platform}"'
        # The full-version-list hint must carry the same brand membership. If we add
        # "Google Chrome" to Sec-CH-UA but leave this one untouched, the two headers
        # disagree about which brands exist -- a contradiction worse than the
        # Chromium branding we set out to fix.
        if clean_full:
            headers["sec-ch-ua-full-version-list"] = _format_sec_ch_ua(clean_full)
    else:
        logger.debug("No brand list available; leaving Sec-CH-UA to the browser.")

    try:
        await context.set_extra_http_headers(headers)
    except Exception as e:
        logger.debug(f"Could not set client-hint headers: {e}")

    logger.info(
        f"[dim]✓ Fingerprint aligned to retail Chrome {detected_major} "
        f"(UA and Sec-CH-UA derived from the live browser).[/dim]"
    )

    # 2. The JS half is returned, not installed. apply_stealth_layer emits it inside the
    #    same script that installs the native-masking toString, so every accessor it
    #    defines can be given a native name and a native toString().
    return {
        "ua": clean_ua,
        "brands": clean_brands,
        "full": clean_full,
        "platform": platform,
        "mobile": mobile,
    }

def _build_identity_patch(identity: Optional[Dict[str, Any]]) -> str:
    """
    Emits the JS that rewrites navigator's identity surface, or nothing at all.

    Returning an empty string when `identity` is None is the point: a browser that is
    already retail-consistent (a real Google Chrome install) gets zero patched
    accessors, and zero patched accessors cannot be unmasked by introspection.
    """
    if not identity:
        return "// Browser is already retail-consistent; nothing patched."

    return """
            const CLEAN_UA = %s;
            const CLEAN_BRANDS = %s;
            const CLEAN_FULL = %s;
            const PLATFORM = %s;
            const MOBILE = %s;

            try {
                const navProto = Object.getPrototypeOf(navigator);
                defineNativeGetter(navProto, 'userAgent', function () { return CLEAN_UA; });
                defineNativeGetter(navProto, 'appVersion', function () {
                    return CLEAN_UA.replace(/^Mozilla\\//, '');
                });
            } catch (e) {}

            try {
                if (navigator.userAgentData && CLEAN_BRANDS.length) {
                    const uadProto = Object.getPrototypeOf(navigator.userAgentData);
                    defineNativeGetter(uadProto, 'brands', function () {
                        return CLEAN_BRANDS.map(b => ({ ...b }));
                    });
                    defineNativeGetter(uadProto, 'platform', function () { return PLATFORM; });
                    defineNativeGetter(uadProto, 'mobile', function () { return MOBILE; });

                    const originalHighEntropy = uadProto.getHighEntropyValues;
                    if (typeof originalHighEntropy === 'function') {
                        defineNativeMethod(uadProto, 'getHighEntropyValues', function (hints) {
                            return originalHighEntropy.call(this, hints).then(result => {
                                if (result && result.brands) {
                                    result.brands = CLEAN_BRANDS.map(b => ({ ...b }));
                                }
                                if (result && result.fullVersionList && CLEAN_FULL.length) {
                                    result.fullVersionList = CLEAN_FULL.map(b => ({ ...b }));
                                }
                                return result;
                            });
                        }, 1);
                    }
                }
            } catch (e) {}
    """ % (
        _js_str(identity.get("ua", "")),
        _js_json(identity.get("brands", [])),
        _js_json(identity.get("full", [])),
        _js_str(identity.get("platform", "macOS")),
        "true" if identity.get("mobile") else "false",
    )


async def apply_stealth_layer(
    context: BrowserContext,
    stealth: Stealth,
    page: Optional[Page] = None,
    identity: Optional[Dict[str, Any]] = None,
):
    """
    Installs the automation masking script at *context* level so every page --
    including popups, target=_blank tabs and pages opened later by ensure_active_page --
    inherits it. Page-level injection silently leaves new tabs unprotected.

    `identity` is the cleaned fingerprint from apply_fingerprint, or None when the
    browser is already retail-consistent and needs no identity patching at all.
    Everything is emitted as ONE script so the native-masking helper is in scope for
    every accessor defined below it.
    """
    if not context:
        return

    try:
        await stealth.apply_stealth_async(context)
    except Exception:
        # Older playwright-stealth builds only accept a Page.
        try:
            if page:
                await stealth.apply_stealth_async(page)
        except Exception as e:
            logger.debug(f"Stealth apply warning: {e}")

    identity_patch = _build_identity_patch(identity)

    await context.add_init_script(
        """
        (() => {
            // --- Native-looking toString ---------------------------------------
            // A patched accessor is detected by reading its descriptor and calling
            // Function.prototype.toString on the getter. Replacing toString with a
            // plain function just moves the tell: toString.toString() then leaks JS
            // source. A Proxy over the *native* toString keeps .name, .length and
            // its own toString() native, and lets us register patched functions.
            const patched = new WeakMap();
            const nativeToString = Function.prototype.toString;
            const nativeToStringStr = nativeToString.call(nativeToString);

            const toStringProxy = new Proxy(nativeToString, {
                apply(target, thisArg, args) {
                    if (thisArg === toStringProxy) return nativeToStringStr;
                    if (patched.has(thisArg)) return patched.get(thisArg);
                    return Reflect.apply(target, thisArg, args);
                }
            });

            try {
                Object.defineProperty(Function.prototype, 'toString', {
                    value: toStringProxy,
                    writable: true,
                    configurable: true,
                    enumerable: false
                });
            } catch (e) {}

            const markNative = (fn, label) => {
                try { patched.set(fn, label); } catch (e) {}
                return fn;
            };

            // A patched accessor has three tells, not one. Overriding toString alone
            // leaves the other two: a native getter's .name is "get <prop>" (not "" for
            // an arrow, and not "<prop>" for a named function expression) and its
            // .length is 0. Defining all three together is what makes the replacement
            // indistinguishable from the accessor it replaced.
            const defineNativeGetter = (obj, prop, impl) => {
                try { Object.defineProperty(impl, 'name', { value: 'get ' + prop, configurable: true }); } catch (e) {}
                try { Object.defineProperty(impl, 'length', { value: 0, configurable: true }); } catch (e) {}
                markNative(impl, 'function get ' + prop + '() { [native code] }');
                try {
                    Object.defineProperty(obj, prop, {
                        get: impl, set: undefined, enumerable: true, configurable: true
                    });
                } catch (e) {}
            };

            const defineNativeMethod = (obj, prop, impl, arity) => {
                try { Object.defineProperty(impl, 'name', { value: prop, configurable: true }); } catch (e) {}
                try { Object.defineProperty(impl, 'length', { value: arity | 0, configurable: true }); } catch (e) {}
                markNative(impl, 'function ' + prop + '() { [native code] }');
                try {
                    // WebIDL operations live on the prototype as writable, enumerable,
                    // configurable data properties. Assigning to the *instance* instead
                    // (navigator.permissions.query = fn) leaves an own property that
                    // Object.getOwnPropertyNames() reports and real Chrome never has.
                    Object.defineProperty(obj, prop, {
                        value: impl, writable: true, enumerable: true, configurable: true
                    });
                } catch (e) {}
            };

            // --- navigator.webdriver -------------------------------------------
            // Real Chrome exposes `false`, not `undefined`. Reporting undefined is
            // itself anomalous: the property exists on Navigator.prototype in every
            // modern Chrome, so a missing value is a signal of its own.
            try {
                defineNativeGetter(Object.getPrototypeOf(navigator), 'webdriver', function () { return false; });
            } catch (e) {}

            // --- Identity (only when the build is not already retail-consistent) --
            IDENTITY_PATCH

            // NOTE ON WHAT IS DELIBERATELY NOT TOUCHED
            //
            // window.chrome.{app,csi,loadTimes}: a headed Chromium already provides all
            // three natively. The previous version of this file added window.chrome.runtime
            // when it was missing -- but a headed browser with no extensions does not have
            // chrome.runtime, so that block was *adding* the very anomaly it was meant to
            // remove. Measured against an unmodified control: control keys were
            // [loadTimes, csi, app], this session's were [loadTimes, csi, app, runtime].
            //
            // navigator.permissions.query: the old notification-coherence patch assigned
            // to the instance, leaving an own "query" property that the control does not
            // have. It also read Notification.permission, which threw here because
            // --disable-notifications removes the Notification API entirely (that flag is
            // now gone, see start()).
            //
            // screen.*, devicePixelRatio, WebGL vendor/renderer, plugins, languages,
            // vendor and hardwareConcurrency: already genuine and self-consistent on this
            // build. Overriding them manufactures contradictions that do not otherwise
            // exist -- an "Apple Inc." WebGL vendor under a Chrome UA, or a languages
            // list that disagrees with the Accept-Language header.
        })();
        """.replace("IDENTITY_PATCH", identity_patch)
    )


def clear_stale_profile_lock(user_data_dir: Path) -> bool:
    """
    Drop a Chromium singleton lock whose owning process no longer exists.

    Chromium guards a profile with `SingletonLock`, a symlink whose target is
    `<hostname>-<pid>`. A clean exit removes it. A force quit -- Cmd-Q on a
    hung window, Activity Monitor, a crash, closing the laptop lid on a stuck
    run -- does not, and every later launch then fails against a process that
    is not there any more.

    The retry loop below used to sleep a second between attempts "waiting for
    the profile lock to release". A lock held by a dead pid never releases, so
    that was three seconds of waiting followed by a RuntimeError, every single
    time, until someone deleted the profile directory by hand. It presented as
    the app randomly refusing to start.

    The pid is checked before anything is removed, so a profile genuinely in
    use by another running copy is left alone: signal 0 tests for existence
    without delivering anything, and a PermissionError means the process is
    alive under another user, which still counts as alive.

    Returns True if a stale lock was cleared.
    """
    lock = user_data_dir / "SingletonLock"
    try:
        if not lock.is_symlink() and not lock.exists():
            return False
    except OSError:
        return False

    owner_alive = False
    try:
        target = os.readlink(lock)  # "hostname-1234"
        pid = int(target.rsplit("-", 1)[-1])
        try:
            os.kill(pid, 0)
            owner_alive = True
        except ProcessLookupError:
            owner_alive = False
        except PermissionError:
            # Running as another user. Alive, and not ours to clear.
            owner_alive = True
    except (OSError, ValueError, IndexError):
        # Not a symlink we understand, or a target from a different machine.
        # Nothing here can name a live local process, so treat it as stale.
        owner_alive = False

    if owner_alive:
        return False

    cleared = False
    # SingletonCookie and SingletonSocket are written alongside it and are just
    # as stale; leaving them behind reproduces the same failure.
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        path = user_data_dir / name
        try:
            if path.is_symlink() or path.exists():
                path.unlink()
                cleared = True
        except OSError as exc:
            logger.debug(f"Could not clear {name}: {exc}")

    if cleared:
        logger.info(
            "[dim]Cleared a browser profile lock left behind by a previous "
            "session that did not shut down cleanly.[/dim]"
        )
    return cleared


class BrowserSession:
    def __init__(self, config: BrowserConfig):
        self.config = config
        # load_app_config() normally hands this over already absolute, but a
        # default BrowserConfig() carries "./data/browser_profile" -- which
        # resolved into the bundle for any caller that built one directly.
        self.user_data_dir = paths.resolve_writable(
            config.user_data_dir, paths.browser_profile_dir("linkedin")
        )
        self.user_data_dir.mkdir(parents=True, exist_ok=True)

        self.playwright: Optional[Playwright] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self._fingerprint_applied = False
        self.stealth = build_stealth()

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def start(self) -> Page:
        """Launches the persistent browser context and aligns its fingerprint with a retail Chrome."""
        if not self.playwright:
            self.playwright = await async_playwright().start()

        args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-popup-blocking",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--start-maximized",
        ]

        logger.info(f"Launching browser (headless={self.config.headless}) with profile at {self.user_data_dir}...")

        launch_kwargs: Dict[str, Any] = {
            "user_data_dir": str(self.user_data_dir),
            "headless": self.config.headless,
            "slow_mo": self.config.slow_mo,
            "no_viewport": True,
            "args": args,
            "has_touch": False,
            "is_mobile": False,
        }

        # Locale and timezone are deliberately NOT set.
        #
        # They used to be pinned to en-US and Asia/Kolkata. That was harmless
        # while the only two users were in India, and became a defect the moment
        # this shipped to anyone else: a browser reporting Asia/Kolkata from an
        # IP that geolocates to Frankfurt is a contradiction between two facts
        # the site can read in the same request, and unlike a timing anomaly it
        # is a single deterministic check with no false-positive rate. Chromium
        # already takes both values from the host OS, which is by definition
        # consistent with where the machine actually is -- so the fix is to stop
        # overriding them, exactly as the module docstring says. Anything left
        # at its genuine value cannot contradict anything else.
        #
        # `SessionRhythm` reads local wall-clock time, so the active-hours
        # window follows the customer's own day without any extra plumbing.
        override_tz = (getattr(self.config, "timezone_id", "") or "").strip()
        override_locale = (getattr(self.config, "locale", "") or "").strip()
        if override_tz:
            launch_kwargs["timezone_id"] = override_tz
        if override_locale:
            launch_kwargs["locale"] = override_locale
        if override_tz or override_locale:
            logger.info(
                f"[dim]Browser locale/timezone overridden by config "
                f"({override_locale or 'host'} / {override_tz or 'host'}). "
                f"These must match where the machine appears to be.[/dim]"
            )

        # Before the first attempt, not after a failure: a lock owned by a dead
        # process is the single most common reason this launch fails, and
        # retrying into it just spends the retries.
        clear_stale_profile_lock(self.user_data_dir)

        # Prefer a real Google Chrome install: its TLS/JA3 fingerprint, codec set and
        # brand strings are genuine, so no rewriting is needed at all.
        self._using_real_chrome = False
        last_err = None
        for attempt in range(3):
            try:
                self.context = await self.playwright.chromium.launch_persistent_context(
                    channel="chrome",
                    **launch_kwargs
                )
                self._using_real_chrome = True
                logger.info("[green]✓ Launched with native Google Chrome channel.[/green]")
                break
            except Exception as chrome_err:
                last_err = chrome_err
                try:
                    self.context = await self.playwright.chromium.launch_persistent_context(**launch_kwargs)
                    logger.info("[dim]Launched with bundled Chromium (brand strings will be normalized).[/dim]")
                    break
                except Exception as bundled_err:
                    last_err = bundled_err
                    logger.debug(f"Launch attempt {attempt + 1} failed ({bundled_err}), waiting for profile lock release...")
                    # A half-started Chromium can leave its own lock behind, so
                    # re-check between attempts rather than only once up front.
                    clear_stale_profile_lock(self.user_data_dir)
                    await asyncio.sleep(1.0)
        else:
            raise RuntimeError(f"Failed to launch browser persistent context: {last_err}")

        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()

        identity = await apply_fingerprint(self.context, self.page)
        await apply_stealth_layer(self.context, self.stealth, self.page, identity)

        self.page.set_default_timeout(self.config.timeout_ms)
        return self.page

    async def close(self):
        """Clean shutdown of browser context and playwright."""
        if self.context:
            try:
                await self.context.close()
            except Exception as e:
                logger.debug(f"Error closing context: {e}")
            self.context = None
        self.page = None
        if self.playwright:
            try:
                await self.playwright.stop()
            except Exception as e:
                logger.debug(f"Error stopping playwright: {e}")
            self.playwright = None

    async def check_logged_in(self) -> bool:
        """Checks if the user has an active authenticated session on LinkedIn."""
        page = await self.ensure_active_page()
        if not page:
            return False

        try:
            await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
            await asyncio.sleep(2.5)

            current_url = page.url
            if "/feed" in current_url or "/mynetwork" in current_url:
                logger.info("[green]✓ Verified active LinkedIn login session.[/green]")
                return True

            nav_bar = page.locator(".global-nav__me, nav.global-nav")
            if await nav_bar.count() > 0:
                return True

            return False
        except Exception as e:
            logger.warning(f"Session check error: {e}")
            return False

    async def ensure_active_page(self) -> Page:
        """
        Guarantees an active, open, and responsive Playwright Page is available.
        Self-heals if the page or browser context was closed or crashed.
        """
        try:
            if self.context and self.page and not self.page.is_closed():
                await self.page.evaluate("() => true")
                return self.page
        except Exception:
            pass

        if self.context:
            try:
                for p in self.context.pages:
                    if not p.is_closed():
                        self.page = p
                        self.page.set_default_timeout(self.config.timeout_ms)
                        return self.page
                self.page = await self.context.new_page()
                self.page.set_default_timeout(self.config.timeout_ms)
                return self.page
            except Exception as e:
                logger.info(f"Browser window was closed or disconnected ({e}). Re-launching session...")

        # If context is closed or invalid, clean up and re-launch
        if self.context:
            try:
                await self.context.close()
            except Exception:
                pass
            self.context = None
        self.page = None

        return await self.start()

    async def interactive_login(self):
        """
        Opens LinkedIn login page and waits for user to manually complete login and 2FA.
        The persistent profile will save cookies automatically for future runs.
        """
        console.print("\n[bold yellow]══ LinkedIn Manual Login & Session Setup ══[/bold yellow]")
        console.print("Opening LinkedIn login page in the browser...")
        console.print("[dim]Please enter your credentials and solve any 2FA/Captchas in the opened browser window.[/dim]\n")

        page = await self.ensure_active_page()
        await page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")

        max_wait_seconds = 300
        interval = 2
        elapsed = 0

        with console.status("[bold cyan]Waiting for you to log in in the browser window...[/bold cyan]"):
            while elapsed < max_wait_seconds:
                await asyncio.sleep(interval)
                elapsed += interval

                try:
                    if self.page and not self.page.is_closed() and ("/feed" in self.page.url or "/mynetwork" in self.page.url):
                        console.print("\n[bold green]✓ Login detected successfully! Session saved for future runs.[/bold green]")
                        console.print("[bold yellow]⚠️  KEEP BROWSER OPEN: Do NOT close the browser window![/bold yellow]")
                        console.print("[dim cyan]The agent will automatically continue using this browser window to search and apply.[/dim cyan]\n")
                        await human_delay(2, 4)
                        # Re-verify page is active before returning
                        await self.ensure_active_page()
                        return True
                except Exception:
                    pass

        console.print("\n[bold red]✗ Login timed out (5 minutes). Please try running login again.[/bold red]\n")
        return False


def _js_str(value: str) -> str:
    """Serializes a Python string as a JS string literal."""
    import json
    return json.dumps(value)


def _js_json(value: Any) -> str:
    """Serializes a Python object as a JS literal."""
    import json
    return json.dumps(value)
