"""
Command Line Interface (CLI) for the LinkedIn AI Agent.
"""

import asyncio
from typing import Optional
import typer
from rich.table import Table
from rich.panel import Panel

import time
from src.core.config import load_app_config, load_candidate_profile
from src.core.database import Database
from src.core.logger import console, logger
from src.core.models import JobListing, JobStatus, OutreachStatus

from src.browser.session import BrowserSession
from src.browser.session_rhythm import SessionRhythm
from src.llm.client import LLMClient
from src.llm.matcher import JobMatcher, build_matcher, build_form_solver, search_grid
from src.llm.form_solver import FormSolver
from src.llm.outreach_gen import OutreachGenerator
from src.modules.discovery import JobDiscoveryEngine
from src.modules.external_apply import build_apply_router
from src.modules.outreach import RecruiterOutreachEngine
from src.modules.resume_generator import (
    ResumeTailorEngine,
    list_generated_resumes,
    display_resumes_table,
    inspect_resume_diff,
    open_generated_folder,
)


app = typer.Typer(
    help="Autonomous AI Job Search, Application & Outreach Agent for LinkedIn",
    rich_markup_mode="rich",
)


def _build_rhythm(config, salt: str, force: bool) -> Optional[SessionRhythm]:
    """
    Constructs the session governor and applies its start-of-session gate.

    Returns None when the session should not run at all (rest day, or outside the
    configured active hours). `--force` bypasses the gate: the check exists to protect
    the account, not to lock the user out of their own tool, and a person who wants to
    run it at 2am should be told why that is a bad idea and then allowed to.
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
        console.print("[yellow]Continuing anyway because --force was given.[/yellow]")
        return rhythm

    console.print(f"\n[bold yellow]Not running: {why}[/bold yellow]")
    console.print("[dim]Re-run with --force to override, or adjust the 'rhythm' section of config.yaml.[/dim]\n")
    return None


@app.command("import-cv")
def import_cv():
    """Rebuild the profile's experience, skills and education from your CV."""
    from src.core import paths
    from src.llm.client import LLMClient
    from src.modules import profile_import
    import shutil, yaml

    profile_path = paths.profile_path()
    existing = yaml.safe_load(profile_path.read_text(encoding="utf-8")) or {}
    stored = existing.get("resume_path")
    if not stored:
        console.print("[bold red]No base resume on file. Upload your CV first.[/bold red]")
        raise typer.Exit(1)

    console.print(f"[cyan]Reading {stored}...[/cyan]")
    try:
        extracted = profile_import.extract_profile(stored, LLMClient(load_app_config().llm))
    except Exception as exc:
        console.print(f"[bold red]{exc}[/bold red]")
        raise typer.Exit(1)

    backup = profile_path.with_suffix(".yaml.bak")
    shutil.copy2(profile_path, backup)
    merged = profile_import.merge_into_profile(extracted, existing)
    profile_path.write_text(
        yaml.safe_dump(merged, sort_keys=False, allow_unicode=True, width=100), encoding="utf-8"
    )
    console.print(f"\n[dim]Previous profile saved to {backup.name}[/dim]")
    console.print(profile_import.summarise(extracted))
    console.print("\n[bold green]Profile updated from your CV.[/bold green]")


@app.command()
def login():
    """Launch browser to log in to LinkedIn and save persistent session."""
    async def _login():
        config = load_app_config()
        # Force headful for interactive login
        config.browser.headless = False
        async with BrowserSession(config.browser) as browser:
            if await browser.check_logged_in():
                console.print("[bold green]✓ You are already logged in to LinkedIn![/bold green]")
                return
            await browser.interactive_login()

    asyncio.run(_login())


@app.command()
def search(
    keyword: Optional[str] = typer.Option(None, "--keyword", "-k", help="Specific job title keyword"),
    location: Optional[str] = typer.Option(None, "--location", "-l", help="Target location"),
    force: bool = typer.Option(False, "--force", help="Run even outside the configured active hours / on a rest day"),
):
    """Search LinkedIn for jobs and evaluate match scores using LLM."""
    async def _search():
        config = load_app_config()
        rhythm = _build_rhythm(config, "linkedin-search", force)
        if rhythm is None:
            return

        profile = load_candidate_profile()
        db = Database(config.general.database_path)
        llm = LLMClient(config.llm)
        matcher = build_matcher(config, profile, llm)

        if keyword or location:
            pairs = [
                (kw, loc)
                for kw in ([keyword] if keyword else config.search.keywords)
                for loc in ([location] if location else config.search.locations)
            ]
        else:
            from launcher import resolve_search_keywords

            pairs = search_grid(config) + search_grid(
                config, resolve_search_keywords(config, profile, llm)
            )

        async with BrowserSession(config.browser) as browser:
            if not await browser.check_logged_in():
                console.print("[bold red]Not logged in! Run 'python run.py login' first.[/bold red]")
                return

            page = await browser.ensure_active_page()
            discovery = JobDiscoveryEngine(
                page, config, profile, db, matcher,
                browser_session=browser, rhythm=rhythm,
            )

            for kw, loc in pairs:
                # The keyword x location grid is far more searches than one
                # sitting should perform. Working through all of it at once is
                # the volume pattern that gets an account looked at, so the
                # governor ends the session partway through -- and `search_grid`
                # rotates the starting point by date so the tail of the list is
                # reached on later days instead of never.
                if not rhythm.within_active_hours() or rhythm.budget_exhausted() or rhythm.quota_reached("evaluate"):
                    console.print("[bold cyan]Session finished for today; remaining searches will run next time.[/bold cyan]")
                    break
                console.print(f"\n[bold magenta]══ Searching for '{kw}' in '{loc}' ══[/bold magenta]")
                matches = await discovery.search_and_evaluate(kw, loc)
                console.print(f"[bold green]Processed {len(matches)} job listings for {kw}.[/bold green]")

            report = matcher.report()
            if report:
                console.print(f"[dim]{report}[/dim]")

    asyncio.run(_search())


@app.command()
def apply(
    dry_run: bool = typer.Option(False, "--dry-run", help="Simulate form filling without submitting"),
    human_in_the_loop: bool = typer.Option(False, "--interactive", "-i", help="Ask confirmation before each submission"),
    force: bool = typer.Option(False, "--force", help="Run even outside the configured active hours / on a rest day"),
):
    """Apply to matched Easy Apply jobs in the database."""
    async def _apply():
        config = load_app_config()
        if dry_run:
            config.general.dry_run = True
        if human_in_the_loop:
            config.general.human_in_the_loop = True

        rhythm = _build_rhythm(config, "linkedin-apply", force)
        if rhythm is None:
            return

        profile = load_candidate_profile()
        db = Database(config.general.database_path)
        llm = LLMClient(config.llm)
        solver = build_form_solver(config, llm)

        async with BrowserSession(config.browser) as browser:
            if not await browser.check_logged_in():
                console.print("[bold red]Not logged in! Run 'python run.py login' first.[/bold red]")
                return

            # Fetch pending matched jobs
            with db._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT job_id FROM jobs 
                    WHERE status = ? AND is_easy_apply = 1
                    ORDER BY match_score DESC
                """, (JobStatus.EVALUATED_MATCH.value,))
                job_ids = [row[0] for row in cursor.fetchall()]

            if not job_ids:
                console.print("[yellow]No pending matched jobs found in database. Run 'search' first.[/yellow]")
                return

            console.print(f"\n[bold green]Found {len(job_ids)} matched jobs ready for application.[/bold green]")
            page = await browser.ensure_active_page()
            engine = build_apply_router(
                page, config, profile, db, solver,
                browser_session=browser, rhythm=rhythm,
            )

            for jid in job_ids:
                job = db.get_job(jid)
                if not job:
                    continue
                await engine.apply_to_job(job)
                # The governor decides on breaks and when to stop for the day. It
                # returns False once the session is spent; honouring that is the whole
                # point, so the queue is deliberately left unfinished.
                if not await rhythm.pace(page, "apply"):
                    break

    asyncio.run(_apply())


@app.command()
def outreach(
    send: bool = typer.Option(False, "--send", help="Send invitations immediately instead of queuing"),
    force: bool = typer.Option(False, "--force", help="Run even outside the configured active hours / on a rest day"),
):
    """Generate or send personalized recruiter connection notes (Option 3)."""
    async def _outreach():
        config = load_app_config()
        if send:
            config.outreach.auto_send_connection = True

        # Only gate on rhythm when invitations will actually be sent. Drafting notes
        # into the database touches LinkedIn not at all, and connection invitations are
        # the fastest route to an account restriction, so they get the tighter leash.
        rhythm = None
        if config.outreach.auto_send_connection:
            rhythm = _build_rhythm(config, "linkedin-outreach", force)
            if rhythm is None:
                return

        profile = load_candidate_profile()
        db = Database(config.general.database_path)
        llm = LLMClient(config.llm)
        gen = OutreachGenerator(llm)

        async with BrowserSession(config.browser) as browser:
            if not await browser.check_logged_in():
                console.print("[bold red]Not logged in! Run 'python run.py login' first.[/bold red]")
                return

            # Get jobs with recruiter info
            with db._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT job_id FROM jobs 
                    WHERE recruiter_profile_url IS NOT NULL
                    AND status IN (?, ?)
                """, (JobStatus.EVALUATED_MATCH.value, JobStatus.APPLIED.value))
                job_ids = [row[0] for row in cursor.fetchall()]

            if not job_ids:
                console.print("[yellow]No jobs with identifiable recruiters found yet.[/yellow]")
                return

            page = await browser.ensure_active_page()
            engine = RecruiterOutreachEngine(
                page, config, profile, db, gen, browser_session=browser
            )

            for jid in job_ids:
                job = db.get_job(jid)
                if not job:
                    continue
                await engine.prepare_or_send_outreach(job)
                if rhythm is not None:
                    rhythm.note_action("outreach")
                    if not await rhythm.pace(page, "outreach"):
                        break

    asyncio.run(_outreach())


@app.command()
def run_all(
    dry_run: bool = typer.Option(True, "--dry-run/--live", help="Run in dry-run mode (default) or live mode"),
    force: bool = typer.Option(False, "--force", help="Run even outside the configured active hours / on a rest day"),
):
    """Full autonomous workflow: Discover jobs -> Evaluate -> Apply -> Outreach."""
    async def _run_all():
        config = load_app_config()
        config.general.dry_run = dry_run

        # One governor shared by every engine in the pipeline: they draw on separate
        # daily quotas but the same session clock, so breaks and the end of the day are
        # decided once rather than three times independently.
        rhythm = _build_rhythm(config, "linkedin-pipeline", force)
        if rhythm is None:
            return

        profile = load_candidate_profile()
        db = Database(config.general.database_path)
        llm = LLMClient(config.llm)

        matcher = build_matcher(config, profile, llm)
        solver = build_form_solver(config, llm)
        outreach_gen = OutreachGenerator(llm)

        console.print(Panel.fit(
            f"[bold cyan]LinkedIn Autonomous AI Agent Running[/bold cyan]\n"
            f"Mode: {'[yellow]DRY-RUN (Safe)' if dry_run else '[bold green]LIVE APPLICATION'}[/]\n"
            f"Target Candidate: [bold white]{profile.personal_info.full_name}[/bold white]\n"
            f"Min Match Score: [green]{config.search.min_match_score}%[/green]\n"
            f"Experience tolerance: [green]±{config.matching.experience_tolerance:.0%}[/green] "
            f"around each posting's stated range",
            border_style="cyan"
        ))

        async with BrowserSession(config.browser) as browser:
            if not await browser.check_logged_in():
                console.print("[bold red]Not logged in! Run 'python run.py login' first.[/bold red]")
                return

            page = await browser.ensure_active_page()
            discovery = JobDiscoveryEngine(
                page, config, profile, db, matcher,
                browser_session=browser, rhythm=rhythm,
            )
            apply_engine = build_apply_router(
                page, config, profile, db, solver,
                browser_session=browser, rhythm=rhythm,
            )
            outreach_engine = RecruiterOutreachEngine(
                page, config, profile, db, outreach_gen,
                browser_session=browser,
            )

            from launcher import resolve_search_keywords

            extra_keywords = resolve_search_keywords(config, profile, llm)

            session_over = False
            for kw, loc in search_grid(config) + search_grid(config, extra_keywords):
                if rhythm.budget_exhausted() or not rhythm.within_active_hours() or rhythm.quota_reached("apply"):
                    session_over = True
                    break

                console.print(f"\n[bold magenta]══ Pipeline: Searching '{kw}' in '{loc}' ══[/bold magenta]")
                matched_jobs = await discovery.search_and_evaluate(kw, loc)

                # Best matches first. Discovery scans the results page in a
                # deliberately non-linear order, so without this the strongest
                # listing in a batch could be the one the session runs out of
                # quota just before reaching.
                matched_jobs = sorted(
                    (j for j in matched_jobs if j.status == JobStatus.EVALUATED_MATCH),
                    key=lambda j: j.match_score or 0,
                    reverse=True,
                )

                for job in matched_jobs:
                    await apply_engine.apply_to_job(job)

                    # Prepare/send recruiter outreach (Option 3)
                    if job.recruiter_profile_url or job.recruiter_name:
                        await outreach_engine.prepare_or_send_outreach(job)
                        if config.outreach.auto_send_connection:
                            rhythm.note_action("outreach")

                    if not await rhythm.pace(browser.page, "apply"):
                        session_over = True
                        break

                if session_over:
                    break

            report = matcher.report()
            if report:
                console.print(f"\n[dim]{report}[/dim]")

            if session_over:
                console.print(
                    "\n[bold cyan]Session ended on schedule. The remaining keyword/location grid "
                    "will be picked up on the next run.[/bold cyan]"
                )

        console.print("\n[bold green]✓ Pipeline execution finished successfully![/bold green]")

    asyncio.run(_run_all())


@app.command()
def stats():
    """Show database statistics, daily rate limits, and application metrics."""
    config = load_app_config()
    db = Database(config.general.database_path)
    data = db.get_statistics()

    table = Table(title="LinkedIn AI Agent Dashboard & Activity", show_header=True, header_style="bold magenta")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="bold white")
    table.add_column("Daily Limit / Status", style="dim green")

    table.add_row("Total Jobs Discovered", str(data["total_jobs_found"]), "-")
    table.add_row("Matching Opportunities", str(data["matches_found"]), "-")
    table.add_row("Applications Submitted (Total)", str(data["applied_jobs"]), "-")
    table.add_row(
        "Applications Today",
        f"{data['today_applications']}",
        f"Limit: {config.limits.max_applications_per_day}/day"
    )
    table.add_row(
        "Outreach Sent Today",
        f"{data['today_outreach']}",
        f"Limit: {config.limits.max_outreach_messages_per_day}/day"
    )
    table.add_row("Outreach Notes Queued", str(data["outreach_queued"]), "Ready for review")
    table.add_row("Outreach Notes Sent", str(data["outreach_sent"]), "Completed")

    console.print(table)


if __name__ == "__main__":
    app()


# ==============================================================================
# Naukri.com Platform CLI Subcommands
# ==============================================================================

from src.modules.naukri.session import NaukriSession
from src.modules.naukri.booster import NaukriProfileBooster
from src.modules.naukri.discovery import NaukriDiscoveryEngine
from src.modules.naukri.apply import NaukriApplyEngine


def _naukri_keywords(config) -> list:
    """Naukri keywords, falling back to the LinkedIn list rather than to hardcoded ones."""
    return [k for k in (config.naukri.keywords or config.search.keywords) if k and k.strip()]


def _naukri_locations(config) -> list:
    return [l for l in (config.naukri.locations or config.search.locations) if l and l.strip()]


def _naukri_grid(keywords: list, locations: list) -> list:
    """Same interleave-and-rotate treatment the LinkedIn grid gets; see search_grid."""
    import datetime as _dt

    if not keywords or not locations:
        return []
    pairs = [
        (keywords[i % len(keywords)], locations[(i // len(keywords)) % len(locations)])
        for i in range(len(keywords) * len(locations))
    ]
    offset = _dt.date.today().toordinal() % len(pairs)
    return pairs[offset:] + pairs[:offset]


@app.command("naukri-login")
def naukri_login():
    """Launch browser to log in to Naukri.com and save persistent session."""
    async def _n_login():
        config = load_app_config()
        user_data_dir = config.naukri.user_data_dir
        async with NaukriSession(user_data_dir=user_data_dir, headless=False) as session:
            if await session.check_logged_in():
                console.print("[bold green]✓ You are already logged in to Naukri.com![/bold green]")
                return
            await session.interactive_login()

    asyncio.run(_n_login())


@app.command("naukri-boost")
def naukri_boost():
    """Refresh Naukri profile headline to boost recruiter search ranking (Resdex)."""
    async def _boost():
        profile = load_candidate_profile()
        config = load_app_config()
        user_data_dir = config.naukri.user_data_dir
        async with NaukriSession(user_data_dir=user_data_dir, headless=config.browser.headless) as session:
            if not await session.check_logged_in():
                console.print("[bold red]Not logged in to Naukri! Run 'python run.py naukri-login' first.[/bold red]")
                return
            booster = NaukriProfileBooster(session.page, profile)
            await booster.boost_profile()

    asyncio.run(_boost())


@app.command("naukri-search")
def naukri_search(
    keyword: Optional[str] = typer.Option(None, "--keyword", "-k", help="Job keyword for Naukri"),
    location: Optional[str] = typer.Option(None, "--location", "-l", help="Location for Naukri"),
    force: bool = typer.Option(False, "--force", help="Run even outside the configured active hours / on a rest day"),
):
    """Search Naukri.com for jobs and score fit using LLM."""
    async def _n_search():
        config = load_app_config()
        rhythm = _build_rhythm(config, "naukri-search", force)
        if rhythm is None:
            return
        profile = load_candidate_profile()
        db = Database(config.general.database_path)
        llm = LLMClient(config.llm)
        matcher = build_matcher(config, profile, llm)

        # The naukri: block in config.yaml had no model behind it, so AppConfig
        # dropped it and these fell back to hardcoded AI-engineering keywords --
        # which for the audit profile meant every Naukri run searched for jobs
        # the candidate had never applied for.
        keywords = [keyword] if keyword else _naukri_keywords(config)
        locations = [location] if location else _naukri_locations(config)

        user_data_dir = config.naukri.user_data_dir
        async with NaukriSession(user_data_dir=user_data_dir, headless=config.browser.headless) as session:
            if not await session.check_logged_in():
                console.print("[bold red]Not logged in to Naukri! Run 'python run.py naukri-login' first.[/bold red]")
                return

            discovery = NaukriDiscoveryEngine(session.page, config, profile, db, matcher, rhythm=rhythm)
            for kw, loc in _naukri_grid(keywords, locations):
                if rhythm.budget_exhausted() or not rhythm.within_active_hours() or rhythm.quota_reached("evaluate"):
                    console.print("[bold cyan]Session finished for today; remaining searches will run next time.[/bold cyan]")
                    break
                console.print(f"\n[bold magenta]══ Naukri: Searching '{kw}' in '{loc}' ══[/bold magenta]")
                matches = await discovery.search_and_evaluate(kw, loc)
                console.print(f"[bold green]Processed {len(matches)} Naukri listings for {kw}.[/bold green]")

            report = matcher.report()
            if report:
                console.print(f"[dim]{report}[/dim]")

    asyncio.run(_n_search())


@app.command("naukri-run-all")
def naukri_run_all(
    dry_run: bool = typer.Option(True, "--dry-run/--live", help="Run Naukri pipeline in dry-run or live mode"),
    force: bool = typer.Option(False, "--force", help="Run even outside the configured active hours / on a rest day"),
):
    """Full autonomous pipeline for Naukri: Boost Profile -> Discover -> Match -> Apply."""
    async def _n_run_all():
        config = load_app_config()
        config.general.dry_run = dry_run
        rhythm = _build_rhythm(config, "naukri-pipeline", force)
        if rhythm is None:
            return
        profile = load_candidate_profile()
        db = Database(config.general.database_path)
        llm = LLMClient(config.llm)

        matcher = build_matcher(config, profile, llm)
        solver = build_form_solver(config, llm)

        console.print(Panel.fit(
            f"[bold cyan]Naukri.com Autonomous AI Agent Running[/bold cyan]\n"
            f"Mode: {'[yellow]DRY-RUN (Safe)' if dry_run else '[bold green]LIVE APPLICATION'}[/]\n"
            f"Target Candidate: [bold white]{profile.personal_info.full_name}[/bold white]",
            border_style="cyan"
        ))

        user_data_dir = config.naukri.user_data_dir
        async with NaukriSession(user_data_dir=user_data_dir, headless=config.browser.headless) as session:
            if not await session.check_logged_in():
                console.print("[bold red]Not logged in to Naukri! Run 'python run.py naukri-login' first.[/bold red]")
                return

            # 1. Daily Profile Boost
            if config.naukri.auto_boost_profile:
                booster = NaukriProfileBooster(session.page, profile)
                await booster.boost_profile()

            # 2. Discover & Apply
            discovery = NaukriDiscoveryEngine(session.page, config, profile, db, matcher, rhythm=rhythm)
            apply_engine = NaukriApplyEngine(session.page, config, profile, db, solver, rhythm=rhythm)

            session_over = False
            for kw, loc in _naukri_grid(_naukri_keywords(config), _naukri_locations(config)):
                if rhythm.budget_exhausted() or not rhythm.within_active_hours() or rhythm.quota_reached("apply"):
                    session_over = True
                    break
                console.print(f"\n[bold magenta]══ Naukri Pipeline: Searching '{kw}' in '{loc}' ══[/bold magenta]")
                matched_jobs = await discovery.search_and_evaluate(kw, loc)
                for job in sorted(
                    (j for j in matched_jobs if j.status == JobStatus.EVALUATED_MATCH),
                    key=lambda j: j.match_score or 0, reverse=True,
                ):
                    await apply_engine.apply_to_job(job)
                    if not await rhythm.pace(session.page, "apply"):
                        session_over = True
                        break
                if session_over:
                    break

            report = matcher.report()
            if report:
                console.print(f"\n[dim]{report}[/dim]")

            if session_over:
                console.print("\n[bold cyan]Session ended on schedule; the rest resumes next run.[/bold cyan]")

        console.print("\n[bold green]✓ Naukri pipeline execution finished successfully![/bold green]")

    asyncio.run(_n_run_all())


@app.command("view-resumes")
def view_resumes():
    """List all AI-generated tailored resumes with fidelity scores and ±10% compliance."""
    resumes = list_generated_resumes()
    display_resumes_table(resumes)


@app.command("inspect-resume")
def inspect_resume(
    index: int = typer.Argument(1, help="Index of the resume to inspect (from 'view-resumes' list)")
):
    """View side-by-side Diff comparing original Master Resume to a generated Tailored Resume."""
    resumes = list_generated_resumes()
    if not resumes:
        console.print("[yellow]No tailored resumes have been generated yet.[/yellow]")
        return
    if 1 <= index <= len(resumes):
        inspect_resume_diff(resumes[index - 1])
    else:
        console.print(f"[red]Invalid index {index}. Total available resumes: {len(resumes)}[/red]")


@app.command("open-resumes")
def open_resumes():
    """Open the 'resumes/generated/' folder in native OS file explorer."""
    open_generated_folder()
    console.print("[green]✓ Opened 'resumes/generated/' folder.[/green]")


@app.command("tailor-resume")
def tailor_resume_cmd(
    title: str = typer.Option("Target Role", "--title", "-t", help="Target Job Title"),
    company: str = typer.Option("Target Company", "--company", "-c", help="Target Company Name"),
    jd: str = typer.Option(
        "",
        "--jd",
        "-j",
        help="Target Job Description snippet"
    )
):
    """Generate a custom LaTeX & ATS vector PDF resume tailored for any specific job within ±10% boundary."""
    async def _tailor():
        config = load_app_config()
        profile = load_candidate_profile()
        llm = LLMClient(config.llm)
        tailor = ResumeTailorEngine(llm)

        dummy_job = JobListing(
            job_id=f"custom_{int(time.time())}",
            title=title,
            company=company,
            location="Bengaluru, India",
            url="https://linkedin.com",
            description=jd
        )
        pdf_path = await tailor.tailor_resume(dummy_job, profile)
        console.print(f"\n[bold green]✓ Created Resume files in:[/bold green] [cyan]{pdf_path.parent}[/cyan]")

    asyncio.run(_tailor())

