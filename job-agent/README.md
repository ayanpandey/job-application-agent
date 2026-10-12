# Job Agent

The agent itself: a command-line tool, an interactive launcher and a desktop app over the same engine. For what the project is and the quick start, see the [root README](../README.md).

## Setup

Needs Python 3.10 or newer.

```bash
python3 -m venv venv
./venv/bin/python -m pip install -r requirements.txt
./venv/bin/python -m playwright install chromium
```

Use `./venv/bin/python -m pip`, not `./venv/bin/pip`: if a virtualenv has ever been copied or moved, its `pip` script can point at a different interpreter and install into the wrong environment.

Then create the four things the agent reads that are not in the repository:

| File | How to create it | What it is |
|---|---|---|
| `config/profile.yaml` | `cp config/profile.example.yaml config/profile.yaml`, then edit | Your details, experience, skills and the facts the form solver may state |
| `.env` | `cp .env.example .env`, then add one API key | Model provider and key |
| `config/prompts.yaml` | `cp ../license-server/prompts.yaml config/prompts.yaml` | Prompt templates, read locally when you call a provider directly |
| `resumes/resume.pdf` | Copy your resume there | Your master resume; tailored copies go to `resumes/generated/` |

All four are gitignored. Once `profile.yaml` has your contact details, `./venv/bin/python run.py import-cv` rebuilds its experience, skills and education from your PDF so you do not have to type them.

### Model providers

Set `LLM_PROVIDER` in `.env`. It overrides `llm.provider` in `config/config.yaml`, whose shipped default is `proxy`.

| `LLM_PROVIDER` | Needs | Notes |
|---|---|---|
| `gemini` | `GEMINI_API_KEY`, or `GOOGLE_APPLICATION_CREDENTIALS` for Vertex AI | Keep a service-account file outside the repository |
| `openai` | `OPENAI_API_KEY` | |
| `groq` | `GROQ_API_KEY` | OpenAI-compatible endpoint |
| `ollama` | `OLLAMA_BASE_URL` | Local models; JSON output quality varies by model |
| `proxy` | A licence key and a deployed [licence server](../license-server/README.md) | What compiled builds use |

With no key configured the agent still runs, on deterministic rules only: no match scoring, tailoring or free-text answers.

## Configuration

`config/config.yaml` holds everything that is not about you personally. The parts most worth knowing:

- `general.dry_run` (default `true`): fill every field, stop before the final submit.
- `general.human_in_the_loop`: ask before each submit. `--interactive` turns it on for one run.
- `search.keywords`, `search.locations`: the grid to search. Empty by default.
- `search.keyword_expansion`: derive up to `max_expanded_keywords` extra titles from your profile.
- `search.min_match_score`: jobs scoring below this are not applied to.
- `limits.max_applications_per_day` (25) and `limits.max_outreach_messages_per_day` (15).
- `outreach.auto_send_connection` (default `false`): drafts are saved for review, not sent.
- `browser.headless` (default `false`): leave it off so you can watch.

## Commands

```bash
./venv/bin/python run.py --help
```

| Command | Does |
|---|---|
| `login`, `naukri-login` | Open a browser to log in once. The session is saved under `data/`. |
| `import-cv` | Rebuild the profile's experience, skills and education from your PDF resume. |
| `search` | Search LinkedIn and score what it finds. `--keyword`, `--location` override the config. |
| `apply` | Apply to matched jobs. `--dry-run` to stop before submit, `-i` to confirm each one. |
| `outreach` | Draft recruiter notes. `--send` to send them. |
| `run-all` | Search, match, tailor, apply and draft outreach in one session. `--dry-run` or `--live`. |
| `naukri-search`, `naukri-run-all`, `naukri-boost` | The Naukri equivalents, and the daily profile refresh. |
| `tailor-resume` | Generate a tailored resume for one job description. |
| `view-resumes`, `inspect-resume`, `open-resumes` | List generated resumes, diff one against the master, open the folder. |
| `stats` | Today's activity, limits and queue. |

`launcher.py` is the same thing as a menu. `start_mac_linux.sh`, `start_windows.bat` and `start_windows.ps1` create the environment if needed and open it.

## Desktop app

```bash
./venv/bin/python app/gui.py
```

A pywebview window over the same engine, with an onboarding wizard that writes `profile.yaml` and your own `config.yaml`. From a source checkout these live in the repository; in a compiled build they live in the per-user application-data directory (`src/core/paths.py`).

## Diagnostics and tests

```bash
./venv/bin/python verify_system.py     # seven checks: config, profile, database, model, resume generator, browser
./venv/bin/python verify_stealth.py    # diff the automated browser against an unmodified control
./venv/bin/python -m unittest discover -s tests
```

The tests use the standard library's `unittest` and need no network. Those that drive a form need Playwright's Chromium and skip themselves if it is missing.

## Building a distributable app

```bash
JOB_AGENT_API_URL=https://your-licence-server.example.com ./venv/bin/python build.py
```

`build.py` compiles with Nuitka. A compiled build always uses the `proxy` provider, so it needs the URL of a licence server; it holds no credentials and no prompts. The build refuses to start if a service-account file, key file or `.env` is in the tree, and checks the output again afterwards. `.github/workflows/build-windows.yml` does the Windows build and `diagnose-windows.yml` runs the result the way a customer's machine would.

Before publishing under your own name, change `BUNDLE_ID` in `build.py` to a reverse-DNS identifier on a domain you own.

## Layout

```
app/                Desktop shell: gui.py, onboarding.py, runtime.py, ui/
config/             config.yaml (shipped defaults), profile.example.yaml
src/
  browser/          Session, human-behaviour model, session rhythm, attention, DOM resolver
  cli/              Typer commands
  core/             Config, models, SQLite, paths, licensing, telemetry
  llm/              Client, matcher, pre-filter, form solver, keyword expander, outreach
  modules/          Discovery, Easy Apply, external ATS apply, form filler and scout,
                    resume generator, outreach, status tracker, naukri/
tests/              unittest suites and HTML fixtures of ATS forms
```

## What leaves your machine

Run directly with your own key, the only network traffic is the browser and your model provider. `src/core/telemetry.py` reports activity to a licence server and `src/core/cloud_storage.py` backs resumes up through it. Both need a server URL (`JOB_AGENT_API_URL`, or one baked into a compiled build) and a stored licence key, and send nothing when either is missing, which is the case in a source checkout. `JOB_AGENT_TELEMETRY=off` turns reporting off in a licensed build.
