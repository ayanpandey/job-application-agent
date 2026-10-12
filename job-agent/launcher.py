#!/usr/bin/env python3
"""
Simple 1-Click Interactive Launcher for the Job AI Agent.
"""

import sys
import os
import asyncio
import subprocess
from datetime import datetime
from pathlib import Path

# Ensure project root is in sys.path
BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt

from src.core.config import load_app_config, load_candidate_profile
from src.core.database import Database
from src.core.models import JobListing, JobStatus
from src.browser.session import BrowserSession
from src.browser.session_rhythm import SessionRhythm
from src.llm.client import LLMClient
from src.llm.matcher import build_matcher, build_form_solver, search_grid
from src.llm.outreach_gen import OutreachGenerator
from src.modules.resume_generator import (
    ResumeTailorEngine,
    list_generated_resumes,
    display_resumes_table,
    inspect_resume_diff,
    open_generated_folder,
)
from src.modules.discovery import JobDiscoveryEngine
from src.modules.external_apply import build_apply_router
from src.modules.outreach import RecruiterOutreachEngine
from src.modules.naukri.session import NaukriSession
from src.modules.naukri.booster import NaukriProfileBooster
from src.modules.naukri.discovery import NaukriDiscoveryEngine
from src.modules.naukri.apply import NaukriApplyEngine

console = Console()


def display_banner(profile, config):
    console.print(Panel.fit(
        f"[bold cyan]🤖 Autonomous AI Job Application Agent[/bold cyan]\n"
        f"Candidate: [bold white]{profile.personal_info.full_name}[/bold white] ([dim]{profile.personal_info.email}[/dim])\n"
        f"Target Roles: [bold green]{' | '.join(config.search.keywords[:4]) or 'not set yet'}[/bold green]\n"
        f"LLM Engine: [yellow]Google Vertex AI (Gemini 3.7 Flash - Global Region)[/yellow]\n"
        f"LaTeX Resumes: [magenta]Dynamic Tailoring Enabled (Strict ±10% Boundary Guardrail)[/magenta]",
        border_style="cyan"
    ))


def resume_inspection_menu():
    """Interactive submenu to inspect and open tailored resumes."""
    while True:
        resumes = list_generated_resumes()
        console.print("\n[bold magenta]══ Tailored Resume Management & Inspection (±10% Compliance) ══[/bold magenta]\n")
        display_resumes_table(resumes)

        console.print("\n[bold white]Actions:[/bold white]")
        console.print(" [bold green]1[/bold green] -> 🔍 Inspect Diff & Audit against Master Resume for a specific job")
        console.print(" [bold cyan]2[/bold cyan] -> 📂 Open 'resumes/generated/' folder in File Explorer")
        console.print(" [bold red]0[/bold red] -> 🔙 Return to Main Menu\n")

        sub_choice = Prompt.ask("[bold cyan]Select action[/bold cyan]", choices=["1", "2", "0"], default="1")

        if sub_choice == "1":
            if not resumes:
                console.print("[yellow]No resumes available to inspect.[/yellow]")
            else:
                idx = Prompt.ask(f"[bold cyan]Enter resume number (1-{len(resumes)})[/bold cyan]", default="1")
                try:
                    num = int(idx)
                    if 1 <= num <= len(resumes):
                        inspect_resume_diff(resumes[num - 1])
                    else:
                        console.print("[red]Invalid index.[/red]")
                except ValueError:
                    console.print("[red]Please enter a valid number.[/red]")
            input("\nPress Enter to continue...")
        elif sub_choice == "2":
            open_generated_folder()
            console.print("[green]✓ Opened 'resumes/generated/' folder.[/green]")
            input("\nPress Enter to continue...")
        elif sub_choice == "0":
            break


async def generate_custom_resume_flow():
    """Interactive custom resume generation prompt."""
    console.print("\n[bold magenta]══ Generate Custom Tailored LaTeX/PDF Resume ══[/bold magenta]")
    title = Prompt.ask("[bold cyan]Target Job Title[/bold cyan]", default="Senior AI Engineer (Agentic LLMs & RAG)")
    company = Prompt.ask("[bold cyan]Target Company Name[/bold cyan]", default="OpenAI / Google DeepMind")
    jd = Prompt.ask(
        "[bold cyan]Paste Job Description / Key Requirements[/bold cyan]",
        default="Seeking an AI Engineer to design and scale Autonomous Multi-Agent LLM Orchestrators, Vector Search pipelines, and low-latency real-time voice systems using PyTorch and Golang."
    )

    config = load_app_config()
    profile = load_candidate_profile()
    llm = LLMClient(config.llm)
    tailor = ResumeTailorEngine(llm)

    dummy_job = JobListing(
        job_id=f"custom_{int(datetime.now().timestamp())}",
        title=title,
        company=company,
        location="Bengaluru, India",
        url="https://linkedin.com",
        description=jd
    )

    pdf_path = await tailor.tailor_resume(dummy_job, profile)
    console.print(f"\n[bold green]✓ Created Resume files:[/bold green] [cyan]{pdf_path.name}[/cyan]")
    console.print(f"[dim]Full Path: {pdf_path}[/dim]")


async def run_status_sweep_flow():
    """
    Reads LinkedIn's own verdict on applications already submitted.

    Kept separate from the apply run rather than tacked onto its end: it is
    read-only, takes a couple of minutes, and is worth running on days when
    nothing new is applied to. Recruiters act on their own schedule, so the
    interesting changes usually land well after the run that created them.
    """
    from src.modules.status_tracker import run_status_sweep

    config = load_app_config()
    db = Database(config.general.database_path)

    console.print("\n[bold magenta]══ Checking application statuses on LinkedIn ══[/bold magenta]")

    async with BrowserSession(config.browser) as browser:
        if not await browser.check_logged_in():
            console.print("[bold yellow]Not logged in! Opening login page...[/bold yellow]")
            if not await browser.interactive_login():
                return

        page = await browser.ensure_active_page()
        summary = await run_status_sweep(page, db)

    if summary.get("error"):
        console.print(f"[yellow]Sweep did not complete: {summary['error']}[/yellow]")
        return

    console.print(
        f"\n[bold green]Read {summary['seen']} applications, "
        f"{summary['changed']} changed since last check.[/bold green]"
    )
    for status, count in sorted(summary.get("statuses", {}).items()):
        console.print(f"  [cyan]{status:<12}[/cyan] {count}")


def build_rhythm(config, salt: str, force: bool = False):
    """
    Constructs the session governor and applies its start-of-session gate.

    Returns None when the session should not run at all -- a rest day, or outside
    the configured active hours. This is the same gate `src/cli/main.py` applies;
    the desktop flows below used to construct no governor whatsoever, which meant
    the shipped build had no breaks, no active hours, no rest days and no daily
    quota -- every protection in `session_rhythm.py` was dead code in the binary.
    """
    rhythm = SessionRhythm(
        config.build_rhythm_config(),
        quotas={
            "apply": config.limits.max_applications_per_day,
            "evaluate": config.limits.max_jobs_evaluated_per_day,
            "outreach": config.limits.max_outreach_messages_per_day,
        },
        salt=salt,
    )

    ok, why = rhythm.preflight()
    if ok:
        console.print(f"[dim]Session rhythm: {why}[/dim]")
        return rhythm

    if force:
        console.print(f"[yellow]Session rhythm would have stopped this run: {why}[/yellow]")
        console.print("[yellow]Continuing anyway because the run was forced.[/yellow]")
        return rhythm

    console.print(f"\n[bold yellow]Not running: {why}[/bold yellow]")
    return None


def resolve_search_keywords(config, profile, llm) -> list:
    """
    The keywords this run will search, seeds first.

    The customer's own words are searched before anything derived, always: a
    session only gets through part of the grid, so anything appended here is
    spending the tail of the session, never the front of it. See
    `search_grid`, which builds each block with its own daily rotation.

    Derived keywords are re-used from config until the profile or the seed list
    changes, so this is one model call on the first run and none after it.
    """
    seeds = [k for k in (config.search.keywords or []) if k and k.strip()]
    if not seeds or not getattr(config.search, "keyword_expansion", True):
        return []

    from src.core.config import save_expanded_keywords
    from src.llm.keyword_expander import build_keyword_expander

    expander = build_keyword_expander(config, llm)
    fingerprint = expander.fingerprint(profile, seeds)
    limit = max(0, int(getattr(config.search, "max_expanded_keywords", 8) or 0))

    stored = [k for k in (getattr(config.search, "expanded_keywords", None) or []) if k and k.strip()]
    if stored and getattr(config.search, "expanded_from", "") == fingerprint:
        console.print(f"[dim]Also searching your saved extra keywords: {', '.join(stored[:limit])}[/dim]")
        return stored[:limit]

    result = expander.expand(
        profile, seeds, limit=limit,
        exclude_title_keywords=config.matching.exclude_title_keywords,
    )
    if not result:
        console.print(f"[dim]Searching your keywords as entered ({result.source}).[/dim]")
        return []

    save_expanded_keywords(result.keywords, fingerprint)
    console.print(
        f"\n[bold cyan]Also searching {len(result.keywords)} related title(s):[/bold cyan] "
        f"{', '.join(result.keywords)}"
    )
    for keyword in result.keywords:
        reason = result.rationale.get(keyword)
        if reason:
            console.print(f"  [dim]{keyword} — {reason}[/dim]")
    console.print(
        "[dim]Saved to your config.yaml under search.expanded_keywords; "
        "delete any you disagree with.[/dim]"
    )
    return result.keywords


async def run_linkedin_flow(live: bool = False, force: bool = False):
    config = load_app_config()
    config.general.dry_run = not live
    profile = load_candidate_profile()
    db = Database(config.general.database_path)
    llm = LLMClient(config.llm)

    # Built from config, not bare. A plain JobMatcher(llm) has no pre-filter and
    # no cache, so every listing cost a full model call and the same posting was
    # re-scored each time it surfaced under a different keyword.
    matcher = build_matcher(config, profile, llm)
    solver = build_form_solver(config, llm)
    outreach_gen = OutreachGenerator(llm)
    resume_tailor = ResumeTailorEngine(llm)

    rhythm = build_rhythm(config, "linkedin-pipeline", force)
    if rhythm is None:
        return

    extra_keywords = resolve_search_keywords(config, profile, llm)

    console.print(f"\n[bold magenta]══ Starting LinkedIn {'LIVE Application' if live else 'SAFE Dry-Run'} ══[/bold magenta]")

    async with BrowserSession(config.browser) as browser:
        if not await browser.check_logged_in():
            console.print("[bold yellow]Not logged in! Opening login page...[/bold yellow]")
            logged_in = await browser.interactive_login()
            if not logged_in:
                return

        page = await browser.ensure_active_page()
        apply_engine = build_apply_router(
            page, config, profile, db, solver,
            resume_tailor=resume_tailor, browser_session=browser, rhythm=rhythm,
        )

        async def apply_to_match(job) -> bool:
            """
            Apply to a posting the moment it is judged a match.

            Returns False when the session should stop, which is the signal the
            scan loop uses to break out. Discovery restores the results page
            afterwards, so an external application navigating away does not
            cost the rest of the page.
            """
            nonlocal session_over
            await apply_engine.apply_to_job(job)

            if job.recruiter_name or job.recruiter_profile_url:
                await outreach_engine.prepare_or_send_outreach(job)
                if config.outreach.auto_send_connection:
                    rhythm.note_action("outreach")

            if not await rhythm.pace(browser.page, "apply"):
                session_over = True
                return False
            return True

        discovery = JobDiscoveryEngine(
            page, config, profile, db, matcher,
            browser_session=browser, rhythm=rhythm, on_match=apply_to_match,
        )

        outreach_engine = RecruiterOutreachEngine(page, config, profile, db, outreach_gen, browser_session=browser)

        consecutive_failures = 0
        session_over = False

        # Clear the backlog before searching for more. A match that was scored
        # but never applied to is finished work being thrown away, and it piles
        # up whenever a session ends between the two -- the rhythm stopping the
        # run, a quota reached mid-page, the browser dropping. Nothing used to
        # revisit them, so they sat in the database as matches for ever.
        #
        # After `session_over` and `outreach_engine` exist, both of which
        # `apply_to_match` needs: the callback is defined above but must not be
        # *called* until they do.
        backlog = db.get_unapplied_matches()
        if backlog:
            console.print(
                f"\n[bold cyan]Applying to {len(backlog)} match(es) found in earlier "
                f"sessions before searching for more.[/bold cyan]"
            )
            for job in backlog:
                if not await apply_to_match(job):
                    break

        # search_grid interleaves locations and rotates the starting point by
        # date. The nested `for kw: for loc:` this replaces worked the grid in a
        # fixed order every run, so a session that stopped early always stopped
        # in the same place and the tail of the keyword list was never searched.
        for kw, loc in search_grid(config) + search_grid(config, extra_keywords):
            if rhythm.budget_exhausted() or not rhythm.within_active_hours() or rhythm.quota_reached("apply"):
                session_over = True
                break

            console.print(f"\n[bold cyan]Searching LinkedIn for '{kw}' in '{loc}'...[/bold cyan]")
            matched_jobs = await discovery.search_and_evaluate(kw, loc)

            if matched_jobs is None or (len(matched_jobs) == 0 and browser.page and browser.page.is_closed()):
                consecutive_failures += 1
                if consecutive_failures >= 3:
                    console.print("[bold yellow]Browser session disconnected. Halting LinkedIn search loop.[/bold yellow]")
                    return
            else:
                consecutive_failures = 0

            # Applications happen inside the scan now, through `apply_to_match`
            # below, so there is no batch to work through here. `matched_jobs` is
            # kept for the counts and for the disconnect check above.
            if session_over:
                break

        report = matcher.report()
        if report:
            console.print(f"\n[dim]{report}[/dim]")

        if session_over:
            console.print(
                "\n[bold cyan]Session ended on schedule. The rest of the keyword grid "
                "is picked up on the next run.[/bold cyan]"
            )

    console.print("\n[bold green]✓ LinkedIn run completed successfully![/bold green]")


def _naukri_block(keywords, locations) -> list:
    """One interleaved, date-rotated keyword x location block."""
    keywords = [k for k in (keywords or []) if k and k.strip()]
    locations = [l for l in (locations or []) if l and l.strip()]
    if not keywords or not locations:
        return []

    pairs = [
        (keywords[i % len(keywords)], locations[(i // len(keywords)) % len(locations)])
        for i in range(len(keywords) * len(locations))
    ]
    offset = datetime.now().date().toordinal() % len(pairs)
    return pairs[offset:] + pairs[:offset]


def _naukri_grid(config, extra_keywords=None) -> list:
    """
    Naukri's keyword x location grid, interleaved and date-rotated like LinkedIn's.

    Derived keywords form a second block behind every seed pair, for the same
    reason they do on LinkedIn: a session only works the front of the grid, so
    anything derived must not be able to push out a keyword the customer typed.
    """
    seeds = [k for k in (config.naukri.keywords or config.search.keywords) if k and k.strip()]
    locations = config.naukri.locations or config.search.locations
    seen = {s.strip().lower() for s in seeds}
    extra = [k for k in (extra_keywords or []) if k and k.strip() and k.strip().lower() not in seen]
    return _naukri_block(seeds, locations) + _naukri_block(extra, locations)


async def run_naukri_flow(live: bool = False, force: bool = False):
    config = load_app_config()
    config.general.dry_run = not live
    profile = load_candidate_profile()
    db = Database(config.general.database_path)
    llm = LLMClient(config.llm)

    matcher = build_matcher(config, profile, llm)
    solver = build_form_solver(config, llm)

    rhythm = build_rhythm(config, "naukri-pipeline", force)
    if rhythm is None:
        return

    extra_keywords = resolve_search_keywords(config, profile, llm)

    console.print(f"\n[bold magenta]══ Starting Naukri {'LIVE Application' if live else 'SAFE Dry-Run'} ══[/bold magenta]")

    user_data_dir = config.naukri.user_data_dir
    async with NaukriSession(user_data_dir=user_data_dir, headless=config.browser.headless) as session:
        if not await session.check_logged_in():
            console.print("[bold yellow]Not logged in to Naukri! Opening login page...[/bold yellow]")
            logged_in = await session.interactive_login()
            if not logged_in:
                return

        page = await session.ensure_active_page()

        # 1. Daily Profile Boost
        booster = NaukriProfileBooster(page, profile)
        await booster.boost_profile()

        # 2. Discover & Apply
        discovery = NaukriDiscoveryEngine(page, config, profile, db, matcher, rhythm=rhythm)
        apply_engine = NaukriApplyEngine(page, config, profile, db, solver, rhythm=rhythm)

        session_over = False
        for kw, loc in _naukri_grid(config, extra_keywords):
            if rhythm.budget_exhausted() or not rhythm.within_active_hours() or rhythm.quota_reached("apply"):
                session_over = True
                break

            console.print(f"\n[bold cyan]Searching Naukri for '{kw}' in '{loc}'...[/bold cyan]")
            matched_jobs = await discovery.search_and_evaluate(kw, loc)
            for job in sorted(
                (j for j in (matched_jobs or []) if j.status == JobStatus.EVALUATED_MATCH),
                key=lambda j: j.match_score or 0,
                reverse=True,
            ):
                await apply_engine.apply_to_job(job)
                if not await rhythm.pace(page, "apply"):
                    session_over = True
                    break

            if session_over:
                break

        report = matcher.report()
        if report:
            console.print(f"\n[dim]{report}[/dim]")

    console.print("\n[bold green]✓ Naukri run completed successfully![/bold green]")


def main_menu():
    profile = load_candidate_profile()
    config = load_app_config()

    while True:
        os.system('cls' if os.name == 'nt' else 'clear')
        display_banner(profile, config)

        console.print("[bold white]Choose an action:[/bold white]\n")
        console.print(" [bold green]1[/bold green] -> 🚀 [bold green]Start LinkedIn LIVE Applications[/bold green] (Tailored LaTeX Resume + Apply + Outreach)")
        console.print(" [bold yellow]2[/bold yellow] -> 🛡️ [bold yellow]Start LinkedIn SAFE Dry-Run[/bold yellow] (Test tailored resume generation & form filling)")
        console.print(" [bold cyan]3[/bold cyan] -> ⚡ [bold cyan]Start Naukri.com LIVE Applications[/bold cyan] (Daily Boost + Auto-Apply)")
        console.print(" [bold blue]4[/bold blue] -> 🔥 [bold blue]Naukri Daily Profile Boost Only[/bold blue] (Rank profile to top of search)")
        console.print(" [bold magenta]5[/bold magenta] -> 📄 [bold magenta]Generate Custom LaTeX Resume for Any Job[/bold magenta] (AI Engineer / Machine Learning / GenAI)")
        console.print(" [bold magenta]6[/bold magenta] -> 📂 [bold magenta]View, Inspect & Open Tailored Resumes[/bold magenta] (±10% Compliance & Diff Viewer)")
        console.print(" [bold white]7[/bold white] -> 🔑 [bold white]Login to LinkedIn / Naukri[/bold white] (One-time session setup)")
        console.print(" [bold white]8[/bold white] -> 📊 [bold white]View Stats & Activity Dashboard[/bold white]")
        console.print(" [bold cyan]9[/bold cyan] -> 🔍 [bold cyan]Run Full System Diagnostic & Verification[/bold cyan]")
        console.print(" [bold green]S[/bold green] -> 📬 [bold green]Check Application Statuses on LinkedIn[/bold green] (Viewed / Rejected / Interview)")
        console.print(" [bold red]0[/bold red] -> 🚪 Exit\n")

        # Lettered rather than "10" so the existing numbers keep their meaning --
        # they are in the README, in the Windows launchers, and in the operator's
        # muscle memory.
        choice = Prompt.ask(
            "[bold cyan]Enter option number[/bold cyan]",
            choices=["1", "2", "3", "4", "5", "6", "7", "8", "9", "S", "s", "0"],
            default="1",
        ).upper()

        if choice == "1":
            asyncio.run(run_linkedin_flow(live=True))
            input("\nPress Enter to return to menu...")
        elif choice == "2":
            asyncio.run(run_linkedin_flow(live=False))
            input("\nPress Enter to return to menu...")
        elif choice == "3":
            asyncio.run(run_naukri_flow(live=True))
            input("\nPress Enter to return to menu...")
        elif choice == "4":
            async def _boost_only():
                async with NaukriSession(
                    user_data_dir=config.naukri.user_data_dir, headless=False
                ) as session:
                    if not await session.check_logged_in():
                        await session.interactive_login()
                    booster = NaukriProfileBooster(session.page, profile)
                    await booster.boost_profile()
            asyncio.run(_boost_only())
            input("\nPress Enter to return to menu...")
        elif choice == "5":
            asyncio.run(generate_custom_resume_flow())
            input("\nPress Enter to return to menu...")
        elif choice == "S":
            asyncio.run(run_status_sweep_flow())
            input("\nPress Enter to return to menu...")
        elif choice == "6":
            resume_inspection_menu()
        elif choice == "7":
            console.print("\n[bold cyan]Choose platform to log in:[/bold cyan]")
            console.print(" 1 -> LinkedIn")
            console.print(" 2 -> Naukri.com")
            login_choice = Prompt.ask("Platform", choices=["1", "2"], default="1")
            if login_choice == "1":
                async def _li_login():
                    async with BrowserSession(config.browser) as browser:
                        await browser.interactive_login()
                asyncio.run(_li_login())
            else:
                async def _nk_login():
                    async with NaukriSession(
                        user_data_dir=config.naukri.user_data_dir, headless=False
                    ) as session:
                        await session.interactive_login()
                asyncio.run(_nk_login())
            input("\nPress Enter to return to menu...")
        elif choice == "8":
            db = Database(config.general.database_path)
            stats = db.get_statistics()
            console.print(f"\n[bold green]📊 System Statistics:[/bold green]")
            console.print(f" • Total Jobs Discovered: [bold cyan]{stats['total_jobs_found']}[/bold cyan]")
            console.print(f" • Evaluated Matches: [bold green]{stats['total_matches']}[/bold green]")
            console.print(f" • Applications Submitted: [bold magenta]{stats['total_applied']}[/bold magenta]")
            console.print(f" • Today's Applications: [bold yellow]{stats['today_applications']}/25[/bold yellow]")
            input("\nPress Enter to return to menu...")
        elif choice == "9":
            from verify_system import run_diagnostics
            asyncio.run(run_diagnostics())
            input("\nPress Enter to return to menu...")
        elif choice == "0":
            console.print("[bold yellow]Goodbye! Happy job hunting![/bold yellow]")
            break


if __name__ == "__main__":
    main_menu()
