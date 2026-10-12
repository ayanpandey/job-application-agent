# Engineering notes

The problems that took longest to find while building and shipping this, written down so they are not found twice. Most are about turning a Python app into a binary that works on someone else's Windows machine. Each one names the code that now guards against it.

## Contents

1. [Google Frontend swallows `/healthz` on run.app domains](#1-google-frontend-swallows-healthz-on-runapp-domains)
2. [A build can "succeed" and be unshippable](#2-a-build-can-succeed-and-be-unshippable)
3. [`date.today()` is the local date, not UTC](#3-datetoday-is-the-local-date-not-utc)
4. [One unused provider was 56% of the build](#4-one-unused-provider-was-56-of-the-build)
5. [MSVC cannot compile this project](#5-msvc-cannot-compile-this-project)
6. [The Mark of the Web silently kills the Windows app](#6-the-mark-of-the-web-silently-kills-the-windows-app)
7. [A GUI-subsystem binary discards every traceback](#7-a-gui-subsystem-binary-discards-every-traceback)
8. [Starting a run was broken on every frozen build](#8-starting-a-run-was-broken-on-every-frozen-build)
9. [Text encoding has four separate ends](#9-text-encoding-has-four-separate-ends)
10. [A modal dialog in a headless process is a hang](#10-a-modal-dialog-in-a-headless-process-is-a-hang)
11. [One module took 38 minutes to compile](#11-one-module-took-38-minutes-to-compile)
12. [The client and the proxy ship on separate clocks](#12-the-client-and-the-proxy-ship-on-separate-clocks)
13. [A flag with no expiry](#13-a-flag-with-no-expiry)

---

## 1. Google Frontend swallows `/healthz` on run.app domains

On a Cloud Run `run.app` domain, Google's frontend answers `/healthz` itself with an HTML 404, and the request never reaches the container. Every other path routes normally. The health endpoint used to be `/healthz`, so every check returned 404 while the service worked perfectly. This was misdiagnosed as "the service URL is not routing" across six redeploys and a pointless project migration.

The endpoint is `/health` (`license-server/main.py`) and must not move back. If a Cloud Run service looks unreachable, probe `/` first and read the `content-type`:

```
GET /            → 404 application/json   ← FastAPI answering: the service is fine
GET /healthz     → 404 text/html          ← Google answering
GET /v1/activate → 405 application/json   ← FastAPI (correct for GET on a POST route)
```

## 2. A build can "succeed" and be unshippable

Two failures hid behind output that looked finished.

`--include-package-data=playwright` and Nuitka's Playwright plugin both contributed `playwright/driver/node`, and Nuitka refuses a duplicate path:

```
FATAL: data file to be placed in distribution as 'playwright/driver/node'
conflicts with exe 'playwright/driver/node'
```

It aborts part-way. The bundle left behind was 299 MB, had a correct `Info.plist` and every module, and passed every credential check, but the driver had lost `package/`. The app installed, activated, started, and could not open a browser.

It went unnoticed because the build wrapper piped output through `tail`, which discarded the FATAL line, and because the output check only asked whether something got in that should not have. Never run the build through a pipe that can drop its tail, and always check the exit code.

Separately, Nuitka leaves a linker-signed ad-hoc signature covering the main executable only. `codesign --verify` calls that invalid, and macOS reports an invalid bundle as "the application is damaged and should be moved to the Bin". Removing the quarantine attribute cannot fix that, because the problem is the signature. `build.py` force-signs the whole bundle ad-hoc and verifies it.

## 3. `date.today()` is the local date, not UTC

Quota lives at `usage/<id>/<YYYY-MM-DD>.json`, and the limit message promises a reset at midnight UTC. Cloud Run runs in UTC and wrote the right file. The admin tools ran on a laptop five and a half hours ahead and, between local midnight and 05:30, asked for tomorrow's file, found nothing, and reported every customer as idle.

`license-server/keystore.py` has `utc_today()`. Anything that names a day in a storage path uses it.

## 4. One unused provider was 56% of the build

Counting generated C by top-level package, `openai` contributed 1,514 of 2,705 modules, for a provider a compiled build can never call. `pygments` (325) and `markdown_it` (62) arrived through `rich`. All three are excluded in `build.py`, which took the build to about 705 modules. Both `openai` imports are inside a function, and `rich` was verified to import without the other two by blocking them on `sys.meta_path`.

It was not only slow. MSVC died inside `openai.types.responses` on the first Windows attempt, after 84 minutes.

## 5. MSVC cannot compile this project

`playwright.sync_api._generated` becomes a single 218,746-line C file, and `cl.exe` at `/Ox` dies on it after 29 minutes, having compiled everything else:

```
module.playwright.sync_api._generated.c(218746) : fatal error C1002:
compiler is out of heap space in pass 2
```

Nuitka applies `/Ox` unconditionally in MSVC mode, and `--low-memory` only affects gcc, so MSVC cannot be asked for less. The module cannot be dropped either: `playwright_stealth` does `from playwright import async_api, sync_api` at module level and `src/browser/session.py` imports it, so excluding `sync_api` breaks startup even though none of this project's code uses the sync API. `build.py` therefore passes `--mingw64` on Windows. Do not remove it.

Two more Windows CI traps, both fixed in `.github/workflows/build-windows.yml`:

- `actions/cache` declares `post-if: success()`, so it saved nothing on the two runs that died at 84 and 37 minutes. The workflow uses `cache/restore` and `cache/save` with `if: always()`.
- The package step took the first directory in `dist/`. That folder holds both `gui.build` (a gigabyte of C intermediates) and `gui.dist`, returned alphabetically, so a successful build would have zipped the scratch directory and shipped it. It now names `*.dist` and requires `JobAgent.exe` inside.

## 6. The Mark of the Web silently kills the Windows app

This is what "the Windows app does nothing when you double-click it" turned out to be, confirmed on a real machine.

A file downloaded from the internet gets a `Zone.Identifier` alternate data stream, and Explorer's "Extract All" copies it onto every file it unpacks. The .NET Framework then refuses to load a managed assembly carrying that tag unless the host opts into `loadFromRemoteSources`, which this app does not control, because the host is `clr_loader`'s native `ClrLoader.dll`.

So `pythonnet/runtime/Python.Runtime.dll` never loads, `import clr` fails, pywebview has no Windows backend, and the process dies before a window exists:

```
RuntimeError: Failed to resolve Python.Runtime.Loader.Initialize
from ...\pythonnet\runtime\Python.Runtime.dll
```

`ClrLoader.dll` swallows the real .NET exception and returns NULL, which is why the message names no cause. The cure on the user's side is `Get-ChildItem -Recurse | Unblock-File`.

Three things made this the most dangerous bug in the project:

- Nothing about it reproduces on a build machine. CI pulls its artifact through the Actions API, which applies no zone tag. The binary is correct; only the user's filesystem differs. `diagnose-windows.yml` now writes the `Zone.Identifier` streams itself, so a runner behaves like a customer's machine.
- It presented as complete silence (see the next note).
- The competing explanation, a .NET older than pythonnet 3.x's 4.7.2 floor, was ruled out on the same machine.

`app/gui.py` deletes the stream from its own managed assemblies at startup, before pywebview loads (`_unblock_bundled_assemblies`). It is best-effort: an install under Program Files is not writable by a standard user, and there the crash handler reports the real cause with the `Unblock-File` instruction. A code-signing certificate is the actual fix.

## 7. A GUI-subsystem binary discards every traceback

`build.py` used `--windows-console-mode=disable`, which links the exe for the windowing subsystem. No console is created and none is attached, so on a double-click an uncaught exception writes its traceback nowhere. The previous bug was invisible for this reason alone: the app started, died in milliseconds, and showed nothing.

`disable` does not deny the process stdio. Redirected handles are still inherited. What it denies is a console of its own, so `.\JobAgent.exe` typed into PowerShell prints nothing, which looks exactly like a program that failed without an error.

This is how every startup failure in a frozen GUI app presents. Two changes, both needed:

- `--windows-console-mode=attach`. Still silent on a double-click, but it attaches to the parent console when there is one, so running the exe from a terminal prints the traceback where you ran it.
- `app/gui.py` installs `sys.excepthook` and `threading.excepthook` before its own imports, so even a module-level failure lands in `%APPDATA%\JobAgent\logs\startup-crash.log` and a native dialog.

Do not go back to `disable`, and do not let anything run before the excepthook is installed.

To read a frozen Windows app's stderr without a rebuild:

```powershell
Start-Process .\JobAgent.exe -PassThru -RedirectStandardOutput out.txt -RedirectStandardError err.txt
Start-Sleep 20; Get-Content err.txt
```

## 8. Starting a run was broken on every frozen build

Pressing Start reported `[WinError 6] The handle is invalid`. Fixing that revealed a second, unrelated fault underneath it. Both were in the spawn in `Api.start_run`, and neither is reachable from a developer's machine.

**`stdin` must be given explicitly.** Redirecting any stream makes Windows use `STARTF_USESTDHANDLES`, obliging `subprocess` to supply all three. For one left as `None` it calls `GetStdHandle`. This app has no console. CPython guards only against `GetStdHandle` returning NULL, inventing a dummy pipe in that case, but a GUI-subsystem process returns a non-NULL handle that is not valid, which passes that check and dies a line later in `DuplicateHandle`. `stdin=DEVNULL` gives it a real handle on `NUL`.

**`sys.executable` is not this application.** Nuitka standalone sets it to a `python` or `python.exe` beside the real binary. On Windows that file is never produced, so the spawn raised `[WinError 2]`. On macOS it resolves to the interpreter Nuitka bundles, which starts and then fails on an `--agent-run` argument it has never heard of. `_frozen_executable()` resolves `bundled_dir()/JobAgent[.exe]` and falls back to `argv[0]`.

`paths.bundled_dir()` takes the directory of `sys.executable`, which is right even when the filename is wrong. That is why the UI, config and browser driver all loaded correctly while spawning failed. Do not simplify `_frozen_executable()` back to `sys.executable` on the strength of that.

The ordering is the lesson. `Popen` builds its handles before it calls `CreateProcess`, so the handle bug was raised first and completely hid the wrong path. The `--self-test` mode found the second one on its first run against a real Windows build.

## 9. Text encoding has four separate ends

A run died on its first line, `══ Starting LinkedIn ══`, with `UnicodeEncodeError: 'charmap' codec can't encode characters`. Twenty-two source files emit non-ASCII, so the fix was never to edit strings. There are four places encoding is decided, and every one defaulted to the Windows locale codec:

1. **Rich picks its Win32 console path by file descriptor, not by terminal.** A child spawned with `stdout=PIPE` still has fd 1, so Rich drove its legacy Windows path against a pipe and encoded to the console codepage. `src/core/logger.py` sets `legacy_windows=False` when `JOB_AGENT_PLAIN_LOGS` is set.
2. **The child's own stdio.** `PYTHONIOENCODING=utf-8:replace`, set in the environment and not by reconfiguring in code, because the interpreter fixes stdio encoding before any application code runs.
3. **The parent decoding the pipe.** `Popen(text=True)` with no `encoding=` decodes with the locale codec, so the parent would have raised `UnicodeDecodeError` reading its own child even after the child was fixed.
4. **The process's own stdout.** `_force_utf8_stdio()` at import. Neither of the above covers a process printing on its own behalf, which is how the self-test died echoing its child, and how a crash reporter would die writing a traceback that names a non-ASCII path.

`errors="replace"` throughout, deliberately: a mangled glyph beats a dead run.

## 10. A modal dialog in a headless process is a hang

The crash handler calls `MessageBoxW`, which blocks until someone clicks OK. In CI nobody does, so a crashed self-test survived its own fatal error for 300 seconds and was killed by its harness, reported as "hung", which is exactly the wrong diagnosis. The dialog is suppressed for every headless invocation through `_HEADLESS_FLAGS` in `app/gui.py`. A normal launch still gets it; that is what it is for.

A related harness lesson: the timeout branch bailed before printing what it had captured, so the failure that most needed explaining produced no evidence. Print first, judge second.

## 11. One module took 38 minutes to compile

`google.genai.types` alone took 38 minutes of single-threaded clang at `-O2`, after every other module had finished. It is excluded with `--nofollow-import-to=google.genai` and `--nofollow-import-to=google.cloud`, because a proxy-mode build never imports them: every such import is inside a function, in the `gemini` branch only. Do not remove those flags. ccache is active, so rebuilds are much faster than the first.

## 12. The client and the proxy ship on separate clocks

A release added a `keyword_expansion` task to the licence server and its prompts, and shipped a client that calls it by default. The server was not redeployed, so for four days the new builds called a task the live server had never heard of.

It degraded instead of breaking. The keyword expander catches the refusal, logs that the AI service has no such task yet, and falls back to the seed keywords, which is exactly why nobody noticed. A feature was shipped, enabled, and inert.

A build is not released until the server it talks to is deployed. The two live in one repository and feel like one thing; they are two deployments with no shared version. Before publishing a build, diff what the client calls against what the server serves:

```bash
curl -s "$PROXY/health?probe=1" | python3 -c "import sys,json; print(json.load(sys.stdin)['tasks'])"
grep -rhoE 'run_task_json\("[a-z_]+"' src/ | sort -u
```

## 13. A flag with no expiry

A status board showed `running` straight from the last heartbeat. The server sets the flag on `run_started` and clears it on `run_finished`, and a crash, a force-quit or a flat battery never sends `run_finished`. The flag stays true and every later heartbeat carries it forward. A machine last heard from three weeks earlier displayed a green dot and the word Running.

`online` was the only field with an expiry: a heartbeat inside ten minutes. Requiring both makes a run credible exactly while the heartbeat behind it is.

The general form: every boolean written by one event and cleared by another needs the clearing event to be guaranteed, and across a process boundary it never is. Pair it with something that expires.
