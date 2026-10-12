#!/usr/bin/env python3
"""
Builds the distributable desktop app with Nuitka.

Two deliberate differences from the PyInstaller script this replaces.

Nuitka compiles to C and then to a native binary, so there is no bytecode to
extract. `pyinstxtractor` followed by a decompiler turns a PyInstaller bundle
back into readable source in a couple of minutes; that is not protection for
something being sold. This is not unbreakable either -- nothing shipped to a
customer's machine is -- but it moves the job from "a script anyone can run" to
"read the disassembly of generated C".

What goes in is an allowlist, never a directory sweep. The previous script did
`--add-data=config`, which swept up a live Google service-account key and the
whole of `src/` as plain .py files. So: files are named one at a time, and
`preflight()` refuses to build at all if a secret is sitting where a careless
future edit could pick it up.

    python build.py                      # build for the current platform
    JOB_AGENT_API_URL=https://… python build.py

Windows binaries must be built on Windows -- Nuitka is a compiler, not a
cross-compiler. See .github/workflows/build-windows.yml.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DIST = BASE_DIR / "dist"

APP_NAME = "AI Job Agent"
BINARY_NAME = "JobAgent"
# What the customer sees in Finder and drags into Applications. It matches the
# live setup guide's wording ("drag Job Agent into your Applications folder")
# rather than APP_NAME, because the guide is what they are reading while they
# look at the folder.
BUNDLE_DIR_NAME = "Job Agent"
# Reverse-DNS, on a domain the operator actually owns. Nuitka derives the
# identifier from --macos-app-name, which produced "AI Job Agent" -- spaces and
# all. That is not a valid bundle identifier: macOS scopes Keychain items and
# LaunchServices registration by it, it is the UserDefaults domain, and
# notarisation rejects it outright, so it would have to change the day a
# Developer ID is bought -- moving every customer's stored licence key.
BUNDLE_ID = "com.example.jobagent"
VERSION = os.getenv("JOB_AGENT_VERSION", "1.0.0")
API_URL = os.getenv("JOB_AGENT_API_URL", "")

# Files that must never enter a build, checked by path. Anything matching is a
# hard stop: shipping any one of these is the failure this script exists to
# prevent, and a warning would eventually be ignored.
FORBIDDEN = [
    "config/service_account.json",
    "config/profile.yaml",      # a real person's data
    "config/prompts.yaml",      # the IP; it lives on the proxy now
    ".env",
    "config/.licence",
    "config/.device",
]
FORBIDDEN_SUFFIXES = (".pem", ".key", ".p12", ".pfx")

# Playwright names its bundled Node runtime per platform, and both the packaging
# exclusion below and the completeness check have to agree with the platform
# being built -- an exclusion that names the wrong one silently does nothing.
DRIVER_NODE = "node.exe" if sys.platform == "win32" else "node"

# Everything the app is allowed to carry, as (source, destination-in-bundle).
DATA_FILES = [
    ("config/config.yaml", "config/config.yaml"),
]
DATA_DIRS = [
    ("app/ui", "app/ui"),
]


def say(message: str) -> None:
    print(f"\n\033[1;36m==> {message}\033[0m")


def fail(message: str) -> "NoReturn":  # type: ignore[valid-type]
    print(f"\n\033[1;31mBUILD REFUSED: {message}\033[0m\n", file=sys.stderr)
    sys.exit(1)


def preflight() -> None:
    """Refuse to build if anything secret could be swept in."""
    say("Preflight")

    found = []
    for rel in FORBIDDEN:
        if (BASE_DIR / rel).exists():
            found.append(rel)
    # Skip directories that are not source. `dist` matters most: a completed
    # build copies certifi's cacert.pem into the bundle, preflight scanned it,
    # and refused -- so the first build that fully succeeded made every later
    # build impossible until dist/ was deleted by hand. rmtree(DIST) happens
    # *after* this check, so it could never clear it either. certifi is exempt
    # by name for the same reason verify_output() exempts it: a CA bundle is not
    # a credential.
    skip_dirs = {"venv", "node_modules", "dist", "build", ".git", "__pycache__"}
    for path in BASE_DIR.rglob("*"):
        if not path.is_file() or path.suffix not in FORBIDDEN_SUFFIXES:
            continue
        if skip_dirs & set(path.parts) or "certifi" in path.parts:
            continue
        found.append(str(path.relative_to(BASE_DIR)))

    if found:
        print("    These files exist in the build tree and must not ship:")
        for rel in sorted(set(found)):
            print(f"      - {rel}")
        print()
        print("    They are NOT in the allowlist, so they would not be bundled -- but")
        print("    keeping credentials in the build tree is how they end up shipped.")
        print("    Move them out (or delete them) and run again.")
        print()
        print("    To build anyway, once you are certain:  ALLOW_SECRETS=1 python build.py")
        if os.getenv("ALLOW_SECRETS") != "1":
            fail(f"{len(set(found))} sensitive file(s) present in the build tree.")
        print("    \033[1;33mALLOW_SECRETS=1 set — continuing.\033[0m")

    for src, _ in DATA_FILES:
        if not (BASE_DIR / src).exists():
            fail(f"Allowlisted data file is missing: {src}")
    for src, _ in DATA_DIRS:
        if not (BASE_DIR / src).is_dir():
            fail(f"Allowlisted data directory is missing: {src}")

    if not API_URL:
        print("    \033[1;33mJOB_AGENT_API_URL is not set.\033[0m The build will use the")
        print("    placeholder in licensing.py and will not reach a real server.")
    else:
        print(f"    API endpoint: {API_URL}")

    try:
        import nuitka  # noqa: F401
    except ImportError:
        fail("Nuitka is not installed. Run:  pip install nuitka")

    print("    OK")


def write_build_info() -> None:
    """
    Bakes the endpoint and version into a generated module.

    Generated rather than edited in place so a build never leaves the working
    tree dirty, and so the value that shipped is recoverable from the build log
    rather than from git history.
    """
    say("Writing build info")
    target = BASE_DIR / "src" / "core" / "build_info.py"
    target.write_text(
        '"""Generated by build.py. Do not edit; changes are overwritten."""\n\n'
        f'API_URL = {API_URL!r}\n'
        f'VERSION = {VERSION!r}\n',
        encoding="utf-8",
    )
    print(f"    {target.relative_to(BASE_DIR)}  (API_URL={API_URL or 'unset'}, VERSION={VERSION})")


def nuitka_command() -> list[str]:
    sep = "=" if os.name != "nt" else "="
    cmd = [
        sys.executable, "-m", "nuitka",
        "--standalone",
        "--assume-yes-for-downloads",
        f"--output-dir={DIST}",
        "--company-name=Job Agent",
        f"--product-name={APP_NAME}",
        f"--product-version={VERSION}",
        # Follow only our own packages. Third-party imports are resolved by
        # Nuitka's own analysis; forcing them wholesale is what makes builds
        # enormous and slow.
        "--include-package=src",
        "--include-package=app",
        # Playwright ships a Node driver as package data. Without this the
        # frozen app can import playwright but cannot launch or install a
        # browser, which fails only at runtime on the customer's machine.
        "--include-package-data=playwright",
        # ...but Nuitka's playwright plugin ALSO contributes driver/node, as an
        # executable rather than a data file, and Nuitka refuses to package the
        # same path twice:
        #
        #   FATAL: data file to be placed in distribution as
        #   'playwright/driver/node' conflicts with exe 'playwright/driver/node'
        #
        # That FATAL aborted packaging part-way through, leaving a bundle that
        # looked complete -- 299MB, correct Info.plist, every .so present --
        # but whose playwright/driver held only `node` and had lost
        # `package/`, which is the JavaScript driver that `node` exists to run.
        # The app would import playwright, start, and fail to open a browser on
        # the customer's machine: the one thing it is for.
        #
        # Yielding this single path to the plugin keeps the executable bit and
        # the macOS signing the plugin does, and lets the package data supply
        # the rest of the driver.
        f"--noinclude-data-files=playwright/driver/{DRIVER_NODE}",
        "--enable-plugin=anti-bloat",
        # The Google SDKs are dev-only and must not be compiled in.
        #
        # A shipped build runs the "proxy" provider: it reaches the model over
        # HTTP with httpx and holds no Google credentials, so `google.genai`
        # and `google.cloud.storage` are only ever imported by the direct
        # provider and by CloudStorageManager -- both of which exist for local
        # development. Every one of those imports is inside a function, so
        # nothing breaks by their absence in a customer build.
        #
        # This is not a micro-optimisation. `google.genai.types` is a single
        # enormous generated module (thousands of Pydantic classes); Nuitka
        # emits it as one C translation unit and clang spent **38 minutes** on
        # that one file at full CPU, having finished all 3,110 others. It also
        # meant the operator's model SDK was being shipped to customers who can
        # never call it.
        "--lto=no",
        "--nofollow-import-to=google.genai",
        "--nofollow-import-to=google.cloud",
        # openai is the single biggest thing in this build and nothing in a
        # shipped copy can reach it. Counting the generated C by top-level
        # package: openai contributed 1,514 of 2,705 modules -- 56% of the
        # entire compile -- for a provider that only the direct, developer-only
        # path uses. Both of its imports are inside a function in
        # src/llm/client.py, exactly like google.genai.
        "--nofollow-import-to=openai",
        "--nofollow-import-to=anthropic",
        "--nofollow-import-to=pytest",
        "--nofollow-import-to=unittest",
        # pygments (325 modules) and markdown_it (62) arrive through `rich`,
        # which the app uses for console output only -- rich.syntax and
        # rich.markdown are never imported. Verified by blocking both on
        # sys.meta_path and importing every rich module this codebase actually
        # uses (console, logging, theme, table, panel, columns): all import.
        "--nofollow-import-to=pygments",
        "--nofollow-import-to=markdown_it",
    ]

    for src, dest in DATA_FILES:
        cmd.append(f"--include-data-files={BASE_DIR / src}{sep}{dest}")
    for src, dest in DATA_DIRS:
        cmd.append(f"--include-data-dir={BASE_DIR / src}{sep}{dest}")

    if sys.platform == "darwin":
        cmd += [
            "--macos-create-app-bundle",
            f"--macos-app-name={APP_NAME}",
            f"--macos-app-version={VERSION}",
            # The app drives a browser and reaches the network; declaring the
            # bundle as a normal GUI app keeps it out of the Dock-less limbo
            # that background agents land in.
            "--macos-app-mode=gui",
            # Without this the bundle is named after the entry script: the
            # customer unzipped their purchase and got `gui.app`, while the
            # setup guide told them to drag "Job Agent" into Applications.
            # Windows and Linux already set this; macOS was simply missed.
            f"--output-filename={BINARY_NAME}",
        ]
    elif sys.platform == "win32":
        cmd += [
            # `attach`, not `disable`. Both leave the binary on the windowing
            # subsystem, so neither pops a console on the double-click a
            # customer performs. The difference is what happens when someone is
            # trying to debug it:
            #
            #   disable  no console is created and none is attached. Redirected
            #            handles are still inherited, so
            #            `Start-Process -RedirectStandardError err.txt` does
            #            capture a traceback -- but plain `.\JobAgent.exe` in a
            #            terminal shows nothing, which reads as "it printed no
            #            error" when in fact nobody was listening.
            #   attach   additionally attaches to the parent console when there
            #            is one, so running it from PowerShell prints the
            #            traceback where the person ran it.
            #
            # The redirect trick is what finally read the Mark of the Web crash
            # off a customer machine; `attach` is so that the next person does
            # not have to know the trick. See .github/workflows/diagnose-windows.yml.
            "--windows-console-mode=attach",
            f"--output-filename={BINARY_NAME}.exe",
            # MSVC cannot compile this project. `playwright.sync_api._generated`
            # is a single 218,746-line C file, and cl.exe at /Ox dies on it:
            #
            #   module.playwright.sync_api._generated.c(218746) : fatal error
            #   C1002: compiler is out of heap space in pass 2
            #
            # after 29 minutes, having compiled everything else. Nuitka applies
            # /Ox unconditionally in MSVC mode -- its --low-memory option only
            # affects gcc (it just drops -pipe), so there is no way to ask MSVC
            # for a cheaper optimisation level.
            #
            # The module cannot be dropped either: playwright_stealth does
            # `from playwright import async_api, sync_api` at module level, and
            # src/browser/session.py imports playwright_stealth, so excluding
            # sync_api breaks the app at startup even though none of our own
            # code uses it.
            #
            # gcc has no equivalent heap ceiling on a single translation unit,
            # and Nuitka fetches a known-good MinGW-w64 itself under
            # --assume-yes-for-downloads. The binary wheels this app depends on
            # (lxml, pydantic_core, cryptography) ship as .pyd and are copied
            # rather than recompiled, so the C toolchain does not affect them.
            "--mingw64",
        ]
    else:
        cmd += [f"--output-filename={BINARY_NAME}"]

    cmd.append(str(BASE_DIR / "app" / "gui.py"))
    return cmd


def sign_macos_bundle() -> None:
    """
    Re-sign the .app ad-hoc so the signature actually covers the bundle.

    Nuitka leaves a "linker-signed" ad-hoc signature on the main executable
    only. An .app whose signature claims resources but seals none is not merely
    unsigned -- it is *invalid*, and macOS reports that as "the application is
    damaged and should be moved to the Bin". That message is indistinguishable
    to a customer from a corrupted download, and clearing the quarantine
    attribute does not fix it, because the problem is the signature rather than
    the quarantine.

    A forced ad-hoc signature over the whole bundle seals every nested .so and
    dylib and produces a signature that validates. The app is still not
    notarised and Gatekeeper still refuses it on first launch -- that needs a
    paid Developer ID -- but it refuses it as "unidentified developer", which is
    the case the setup guide walks the customer through and which "Open Anyway"
    resolves.
    """
    if sys.platform != "darwin":
        return

    bundles = [p for p in DIST.iterdir() if p.suffix == ".app"]
    if not bundles:
        fail("No .app bundle was produced.")
    bundle = bundles[0]

    # Nuitka names the bundle directory after the entry script, so
    # --output-filename renamed the executable inside but left the bundle as
    # `gui.app`: the customer unzipped their purchase and saw "gui", while the
    # setup guide told them to look for "Job Agent". Finder shows the directory
    # name, not CFBundleName, so this is the name they actually read.
    #
    # Renamed before signing. The signature seals the bundle's contents, and
    # doing it afterwards would be one more way to hand someone a build macOS
    # calls damaged.
    wanted = DIST / f"{BUNDLE_DIR_NAME}.app"
    if bundle != wanted:
        if wanted.exists():
            shutil.rmtree(wanted)
        bundle.rename(wanted)
        print(f"    {bundle.name} -> {wanted.name}")
        bundle = wanted

    # Before signing, not after: the signature seals Info.plist, so a later edit
    # invalidates it and lands the customer back on "the application is damaged".
    say("Correcting the bundle identifier")
    import plistlib

    plist_path = bundle / "Contents" / "Info.plist"
    with open(plist_path, "rb") as fh:
        plist = plistlib.load(fh)
    was = plist.get("CFBundleIdentifier", "")
    plist["CFBundleIdentifier"] = BUNDLE_ID
    with open(plist_path, "wb") as fh:
        plistlib.dump(plist, fh)
    print(f"    {was!r} -> {BUNDLE_ID!r}")

    say("Signing the app bundle (ad-hoc)")
    signed = subprocess.run(
        ["codesign", "--force", "--deep", "--sign", "-", "--timestamp=none", str(bundle)],
        capture_output=True, text=True,
    )
    if signed.returncode != 0:
        print(signed.stderr.strip())
        fail("codesign failed; the bundle would be reported as damaged on a customer machine.")

    checked = subprocess.run(
        ["codesign", "--verify", "--strict", str(bundle)],
        capture_output=True, text=True,
    )
    if checked.returncode != 0:
        print(checked.stderr.strip())
        fail("The signature does not validate. Do not distribute this build.")

    print(f"    {bundle.name}: signature valid")
    print("    Not notarised -- first launch needs Privacy & Security -> Open Anyway.")


def verify_output() -> None:
    """Check the built bundle for anything that should not have made it in."""
    say("Verifying the built bundle")
    if not DIST.exists():
        fail("No dist/ directory was produced.")

    leaked = []
    for path in DIST.rglob("*"):
        if not path.is_file():
            continue
        name = path.name
        if name in ("service_account.json", "profile.yaml", "prompts.yaml", ".env"):
            leaked.append(str(path.relative_to(DIST)))
        elif path.suffix in FORBIDDEN_SUFFIXES and "certifi" not in str(path):
            leaked.append(str(path.relative_to(DIST)))

    if leaked:
        for rel in leaked:
            print(f"      LEAKED: {rel}")
        fail("The build contains files that must not ship. Do not distribute it.")

    # Plain .py of our own code would mean the compile silently fell back.
    ours = [
        p for p in DIST.rglob("*.py")
        if "src" in p.parts or "app" in p.parts
    ]
    if ours:
        print(f"    \033[1;33mWarning:\033[0m {len(ours)} of our .py files are present as source:")
        for p in ours[:5]:
            print(f"      {p.relative_to(DIST)}")

    print("    No credentials, profile data or prompt templates in the bundle.")

    # Completeness, not just safety. Everything above asks "did something get in
    # that should not have"; nothing asked "did everything get in that must".
    # A packaging step that dies half-way leaves a bundle that passes every
    # check here and cannot open a browser, and that failure surfaces only on
    # the customer's machine, after they have paid.
    # macOS puts the payload inside the bundle; every other platform gets a
    # plain `<name>.dist` directory beside it. Looking in DIST itself finds
    # neither, and a check that looks in the wrong place passes vacuously.
    bundles = [p for p in DIST.iterdir() if p.suffix == ".app"]
    plains = [p for p in DIST.iterdir() if p.suffix == ".dist" and p.is_dir()]
    if bundles:
        root = bundles[0] / "Contents" / "MacOS"
    elif plains:
        root = plains[0]
    else:
        fail("No .app bundle or .dist directory was produced.")
    required = [
        (f"playwright/driver/{DRIVER_NODE}", "the Node runtime the browser driver runs on"),
        ("playwright/driver/package/cli.js", "the browser driver itself"),
        ("config/config.yaml", "the first-run configuration seed"),
        ("app/ui/index.html", "the onboarding and dashboard UI"),
    ]
    missing = [(rel, why) for rel, why in required if not (root / rel).exists()]
    if missing:
        print("    The bundle is incomplete. Missing:")
        for rel, why in missing:
            print(f"      - {rel}  ({why})")
        fail("Packaging did not finish. This build would fail on a customer machine.")
    print("    Browser driver, config seed and UI are all present.")


def main() -> int:
    preflight()
    write_build_info()

    if DIST.exists():
        say("Clearing previous build")
        shutil.rmtree(DIST)

    cmd = nuitka_command()
    say("Compiling with Nuitka (this takes several minutes)")
    print("    " + " \\\n      ".join(cmd))
    result = subprocess.run(cmd, cwd=str(BASE_DIR))
    if result.returncode != 0:
        fail(f"Nuitka exited with code {result.returncode}.")

    sign_macos_bundle()
    verify_output()

    say("Done")
    for child in sorted(DIST.iterdir()):
        print(f"    {child}")
    print()
    print("    Distribute the .app (macOS) or the whole output folder (Windows).")
    print("    Ship a licence key separately — never inside the download.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
