"""
Stealth regression harness: diffs the agent's hardened session against an unmodified
control browser launched from the same binary.

Why a *control* rather than a checklist of known-bad values: the only meaningful
question is "does this session differ from a real browser", and the honest reference
for that is a real browser. A hand-written list of expected values goes stale the
moment Chromium ships a new version, and it cannot catch the failure mode this harness
was written to catch -- the stealth layer *introducing* an anomaly while trying to
remove one.

That failure mode was real. Before this harness existed, the masking layer was leaving:
  * navigator.userAgent's getter reporting `() => CLEAN_UA` from toString()
  * userAgentData.brands / mobile / platform / getHighEntropyValues all leaking JS source
  * an own "query" property on navigator.permissions that no real Chrome has
  * window.chrome.runtime added to a browser that has no extensions
  * window.Notification removed entirely by --disable-notifications
Every one of those made the session *more* identifiable than plain Chromium.

Two categories of output:
  DIFFS          -- surfaces where the agent deviates from a real browser. Should be empty.
  INTENDED       -- the handful of values that are supposed to differ (webdriver, brand
                    strings). Printed so a regression there is visible too.

Run:  ./venv/bin/python verify_stealth.py
"""

import asyncio
import json
import sys
from typing import Any, Dict, List, Tuple

from playwright.async_api import async_playwright

from src.core.config import load_app_config
from src.browser.session import BrowserSession
from src.core.logger import console


# Surfaces that must be byte-identical to a real browser. Anything that shows up here
# is the masking layer making the session easier to fingerprint, not harder.
PROBE = r"""
() => {
  const o = {};
  const S = f => { try { return Function.prototype.toString.call(f); } catch (e) { return 'THREW'; } };
  const nat = f => /\[native code\]/.test(S(f));
  const dsc = (obj, p) => { try { return Object.getOwnPropertyDescriptor(obj, p); } catch (e) { return null; } };

  // --- Own-property surfaces -------------------------------------------------
  // Real Chrome keeps WebIDL operations on prototypes. An own property on the
  // instance is the signature of `navigator.permissions.query = fn`.
  o.own_navigator   = Object.getOwnPropertyNames(navigator);
  o.own_permissions = Object.getOwnPropertyNames(navigator.permissions);
  o.own_uaData      = Object.getOwnPropertyNames(navigator.userAgentData || {});
  o.own_screen      = Object.getOwnPropertyNames(screen);
  o.win_symbols     = Object.getOwnPropertySymbols(window).map(String);
  o.win_leaks       = ['__playwright','__pwInitScripts','_playwright','__driver','__markNative',
                       'cdc_adoQpoasnfa76pfcZLmcfl_Array','__nightmare','callPhantom','_phantom',
                       '__selenium_unwrapped','__webdriver_evaluate','__fxdriver_evaluate']
                      .filter(k => k in window);

  // --- Accessor authenticity -------------------------------------------------
  // A patched accessor has three tells: toString() source, .name, and .length.
  const acc = {};
  const check = (label, obj, prop) => {
    const d = dsc(obj, prop);
    if (!d) { acc[label] = 'ABSENT'; return; }
    acc[label] = d.get
      ? { kind: 'get', name: d.get.name, len: d.get.length, native: nat(d.get) }
      : { kind: 'value', name: typeof d.value === 'function' ? d.value.name : null,
          native: typeof d.value === 'function' ? nat(d.value) : 'nonfn',
          w: d.writable, e: d.enumerable, c: d.configurable };
  };
  ['userAgent','appVersion','webdriver','platform','languages','plugins','mimeTypes',
   'vendor','hardwareConcurrency','deviceMemory','permissions','maxTouchPoints']
    .forEach(p => check('navigator.' + p, Navigator.prototype, p));
  try {
    const UP = Object.getPrototypeOf(navigator.userAgentData);
    ['brands','mobile','platform','getHighEntropyValues','toJSON']
      .forEach(p => check('userAgentData.' + p, UP, p));
  } catch (e) { acc['userAgentData'] = 'ERR ' + e; }
  try {
    check('Permissions.query', Object.getPrototypeOf(navigator.permissions), 'query');
  } catch (e) { acc['Permissions.query'] = 'ERR ' + e; }
  check('Function.toString', Function.prototype, 'toString');
  o.accessors = acc;

  // --- Values that must not contradict the UA or each other -------------------
  o.notification_api = (typeof Notification !== 'undefined') ? Notification.permission : 'API_MISSING';
  o.chrome_keys      = window.chrome ? Object.keys(window.chrome).sort() : 'NO_CHROME';
  o.platform         = navigator.platform;
  o.vendor           = navigator.vendor;
  o.languages        = navigator.languages;
  o.hardwareConcurrency = navigator.hardwareConcurrency;
  o.deviceMemory     = navigator.deviceMemory;
  o.maxTouchPoints   = navigator.maxTouchPoints;
  o.plugins          = Array.from(navigator.plugins).map(p => p.name);
  o.mimeTypes        = Array.from(navigator.mimeTypes).map(m => m.type);
  o.timezone         = Intl.DateTimeFormat().resolvedOptions().timeZone;
  o.screen           = { w: screen.width, h: screen.height, aw: screen.availWidth,
                         ah: screen.availHeight, cd: screen.colorDepth, dpr: devicePixelRatio };
  try {
    const gl = document.createElement('canvas').getContext('webgl');
    const d = gl.getExtension('WEBGL_debug_renderer_info');
    o.webgl = [gl.getParameter(d.UNMASKED_VENDOR_WEBGL), gl.getParameter(d.UNMASKED_RENDERER_WEBGL)];
  } catch (e) { o.webgl = 'ERR'; }

  // --- CDP Runtime.enable probe ----------------------------------------------
  // Serializing an Error through the console only triggers a `stack` getter when a
  // debugger has the Runtime domain enabled. This is the check that historically
  // unmasked CDP-driven browsers regardless of how good their fingerprint was.
  try {
    let hit = false;
    const err = new Error('probe');
    Object.defineProperty(err, 'stack', { configurable: true, get() { hit = true; return ''; } });
    console.debug(err);
    o.cdp_runtime_detected = hit;
  } catch (e) { o.cdp_runtime_detected = 'ERR ' + e; }

  return o;
}
"""

# Values the agent is *meant* to change. Reported separately rather than as failures.
INTENDED = r"""
() => ({
  webdriver: navigator.webdriver,
  userAgent: navigator.userAgent,
  brands: (navigator.userAgentData || {}).brands,
})
"""

CONTROL_URL = "https://www.linkedin.com/robots.txt"


async def probe_control(user_data_dir: str) -> Tuple[Dict, Dict]:
    """An unmodified headed browser from the same binary: the reference to diff against."""
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            headless=False,
            no_viewport=True,
            locale="en-US",
            timezone_id="Asia/Kolkata",
            args=["--start-maximized", "--no-first-run", "--no-default-browser-check"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(CONTROL_URL, wait_until="domcontentloaded")
        result = await page.evaluate(PROBE)
        intended = await page.evaluate(INTENDED)
        await ctx.close()
        return result, intended


async def probe_agent() -> Tuple[Dict, Dict]:
    """The real hardened session, launched exactly as the agent launches it."""
    config = load_app_config()
    config.browser.headless = False
    async with BrowserSession(config.browser) as session:
        page = await session.ensure_active_page()
        await page.goto(CONTROL_URL, wait_until="domcontentloaded")
        return await page.evaluate(PROBE), await page.evaluate(INTENDED)


def walk(agent: Any, control: Any, path: str = "") -> List[Tuple[str, Any, Any]]:
    """Recursively diffs two probe results, returning (path, control, agent) triples."""
    if isinstance(agent, dict) and isinstance(control, dict):
        out: List[Tuple[str, Any, Any]] = []
        for key in sorted(set(agent) | set(control)):
            out += walk(
                agent.get(key, "<missing>"),
                control.get(key, "<missing>"),
                f"{path}.{key}" if path else key,
            )
        return out
    return [] if agent == control else [(path, control, agent)]


async def main() -> int:
    import tempfile

    console.print("\n[bold cyan]══ Stealth regression harness ══[/bold cyan]")
    console.print("[dim]Launching an unmodified control browser...[/dim]")
    with tempfile.TemporaryDirectory(prefix="stealth-control-") as tmp:
        control, control_intended = await probe_control(tmp)

    console.print("[dim]Launching the agent's hardened session...[/dim]")
    agent, agent_intended = await probe_agent()

    diffs = walk(agent, control)

    console.print("\n[bold]INTENDED differences[/bold] [dim](these should differ)[/dim]")
    for key in sorted(set(control_intended) | set(agent_intended)):
        c, a = control_intended.get(key), agent_intended.get(key)
        marker = "[green]changed[/green]" if c != a else "[yellow]unchanged[/yellow]"
        console.print(f"  {key}: {marker}")
        console.print(f"    [dim]control:[/dim] {json.dumps(c)}")
        console.print(f"    [dim]agent  :[/dim] {json.dumps(a)}")

    if agent.get("cdp_runtime_detected") is True:
        console.print(
            "\n[bold red]⚠ CDP Runtime.enable is detectable[/bold red] "
            "[dim](console error-stack probe fired)[/dim]"
        )

    console.print(f"\n[bold]UNINTENDED differences:[/bold] {len(diffs)}")
    if not diffs:
        console.print(
            "[bold green]✓ PASS — the session is indistinguishable from an unmodified "
            "browser on every surface probed.[/bold green]\n"
        )
        return 0

    console.print(
        "[bold red]✗ FAIL — the masking layer is deviating from a real browser below. "
        "Each of these makes the session easier to fingerprint, not harder.[/bold red]"
    )
    for path, c, a in diffs:
        console.print(f"\n  [yellow]{path}[/yellow]")
        console.print(f"    [dim]real browser:[/dim] {json.dumps(c)}")
        console.print(f"    [dim]this session:[/dim] {json.dumps(a)}")
    console.print()
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
