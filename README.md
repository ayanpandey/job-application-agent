# Job Application Agent

A desktop agent that finds jobs matching your profile, tailors your resume to each one, fills in the application, and drafts a note to the recruiter. It works on LinkedIn, Naukri and ten applicant-tracking systems, and it ships with the licence server and build pipeline needed to distribute it as a compiled app.

It runs on your own machine, in a real browser you can watch. Dry-run is the default: the agent fills every field and stops before the final submit until you turn that off.

> **Read before using.** Automating activity on LinkedIn, Naukri and most job sites is against their terms of service, and accounts that do it can be restricted or closed. This project is published for learning and research. If you run it against a real account, you do so at your own risk. See [Responsible use](#responsible-use).

## What it does

| Stage | What happens |
|---|---|
| **Discover** | Searches a keyword × location grid, reads each listing, and can derive extra job titles from your profile. |
| **Pre-filter** | A deterministic screen (title relevance, skills, exclusions) runs before any model call, so obvious mismatches cost nothing. |
| **Match** | A model scores each remaining job against your profile, 0 to 100, with reasons. Jobs under your threshold are dropped. |
| **Tailor** | Generates a resume for the job as LaTeX and PDF. A second, independent prompt audits the draft against your master resume and rejects it if it drifts more than 10%, or changes an employer, date or degree at all. |
| **Apply** | Fills LinkedIn Easy Apply and Naukri forms, and external forms on Greenhouse, Lever, Ashby, SmartRecruiters and other ATS vendors. Screening questions are answered from facts in your profile; when a question needs a fact that is not there, the agent leaves the job for you instead of guessing. |
| **Outreach** | Finds the recruiter or poster and drafts a connection note under LinkedIn's character limit. Sending is off by default. |
| **Track** | Everything is recorded in a local SQLite database: what was found, scored, applied to and with which resume. |

## Repository layout

```
job-agent/        The agent: CLI, desktop app, browser automation, tests
license-server/   Optional Cloud Run service for distributing the app to other people
scripts/          Release helper for uploading builds
docs/             Engineering notes from building and shipping it
.github/          Windows build and diagnostic workflows
```

There are two ways to run the agent, and they differ only in where model calls go.

```
Direct (your own key)                 Proxy (distributing to others)

  job-agent ──▶ Gemini / OpenAI /       job-agent ──licence key──▶ license-server ──▶ Vertex AI
                Groq / Ollama                                     (holds credentials,
                                                                   prompts and quotas)
```

**Direct** is what you want for yourself: clone, add an API key, run. **Proxy** is for handing a compiled app to people who should not hold your cloud credentials; the server authenticates a licence key, enforces a daily quota, and makes the model call itself. [license-server/README.md](license-server/README.md) covers it.

## Quick start

Needs Python 3.10 or newer.

```bash
git clone https://github.com/ayanpandey/job-application-agent.git
cd job-application-agent/job-agent

python3 -m venv venv
./venv/bin/python -m pip install -r requirements.txt
./venv/bin/python -m playwright install chromium

# Your profile, your model key, and the prompt templates
cp config/profile.example.yaml config/profile.yaml     # then edit it
cp .env.example .env                                   # then add one API key
cp ../license-server/prompts.yaml config/prompts.yaml

mkdir -p resumes && cp /path/to/your/resume.pdf resumes/resume.pdf
```

Set your search in `config/config.yaml` under `search.keywords` and `search.locations`, then:

```bash
./venv/bin/python run.py login              # log in once, in the browser that opens
./venv/bin/python run.py run-all --dry-run  # search, match, tailor, fill; never submits
./venv/bin/python run.py stats              # what it found and did
```

`profile.yaml`, `.env`, `config/prompts.yaml`, `resumes/` and `data/` are all gitignored. They hold a real person's details and stay on your machine.

On Windows, `start_windows.bat` creates the environment and opens the menu; `verify_windows.bat` runs the diagnostic. [job-agent/README.md](job-agent/README.md) has the full command list, configuration and the desktop app.

## How it is built

- **Python, Playwright, Pydantic, Typer, Rich.** The desktop shell is pywebview over the OS webview, so the installer does not bundle a second Chromium.
- **Human-behaviour layer** (`job-agent/src/browser/`). Pointer movement, scrolling and typing are modelled rather than scripted, and a session-level governor shapes volume, hours and rest days. `verify_stealth.py` compares the automated browser against an unmodified one from the same binary, which is how regressions in that layer get caught.
- **A form filler that reads the page** (`src/modules/form_filler.py`, `form_scout.py`). It works from labels, roles and structure rather than per-site selectors, and when a selector breaks it can ask the model to re-resolve the element.
- **Prompts as data** (`license-server/prompts.yaml`). Nine tasks, each a system prompt and a template. No prompt names an employer, a field or a candidate.
- **Tests that use a real browser.** 133 tests on the standard library's `unittest`, driving the form filler against HTML fixtures of real ATS layouts.

```bash
cd job-agent && ./venv/bin/python -m unittest discover -s tests
```

- **Compiled builds.** `build.py` compiles with Nuitka for macOS and Windows and refuses to build if a credential file is anywhere in the tree. The workflows in `.github/` build and smoke-test the Windows app.

[docs/ENGINEERING_NOTES.md](docs/ENGINEERING_NOTES.md) collects the problems that took longest to find, mostly about freezing a Python app for Windows.

## Responsible use

- Automated applications and connection requests break the terms of service of LinkedIn, Naukri and most job boards. Expect that an account used this way can be challenged, restricted or banned. The agent does not solve captchas or work around login walls: an application form behind one is abandoned and left for you.
- Leave `dry_run: true` until you have read what it fills in. The form solver is deliberately conservative, but you are the one applying.
- Keep the daily limits low. They exist to protect your account and to keep the applications you send worth sending.
- Only apply to jobs you would take. An employer reads every application this sends.
- The tailoring auditor exists so the resume stays true. Do not loosen it to invent experience.

## Licence

[MIT](LICENSE). The bundled IBM Plex fonts in `job-agent/app/ui/fonts/` are © IBM Corp. and licensed under the SIL Open Font License 1.1.
