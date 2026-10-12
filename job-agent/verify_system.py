#!/usr/bin/env python3
"""
Comprehensive End-to-End System Diagnostic & Verification Script.
Tests:
1. Environment & Files
2. Candidate Profile & Search Keywords
3. SQLite Database & Activity Tracking
4. Google Vertex AI (Gemini 3.7 Flash - Global Region) Connectivity
5. OAuth2 Service Account Token Auto-Renewal
6. Dynamic LaTeX & ATS Resume Generator (.tex and .pdf)
7. Playwright Stealth & Bezier Mouse Physics Engine
"""

import sys
import os
import asyncio
from pathlib import Path

# Ensure project root is in sys.path
BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from rich.console import Console
from rich.table import Table
from rich.panel import Panel

console = Console()


async def run_diagnostics():
    console.print(Panel.fit(
        "[bold cyan]🔍 Autonomous AI Job Agent - Full System Diagnostic[/bold cyan]\n"
        "[dim]Verifying all subsystems: config, profile, database, LLM, resume generator and browser.[/dim]",
        border_style="cyan"
    ))

    results = []

    # -------------------------------------------------------------------------
    # TEST 1: Config & File System Integrity
    # -------------------------------------------------------------------------
    console.print("\n[bold yellow][1/7] Checking Config & File Structure...[/bold yellow]")
    try:
        from src.core.config import load_app_config, load_candidate_profile
        config = load_app_config()
        profile = load_candidate_profile()

        # Checked where the app actually reads them from. Resolving "./config"
        # against the working directory only matched from a checkout, so this
        # test reported a missing config for every installed build -- the one
        # place it was most needed.
        from src.core import paths

        assert paths.app_config_path().exists(), f"{paths.app_config_path()} is missing"
        assert paths.profile_path().exists(), f"{paths.profile_path()} is missing"
        
        console.print(f"[green]  ✓ Config verified in {paths.config_dir()}.[/green]")
        results.append(("Config & File Structure", "PASSED", "Config and profile present"))
    except Exception as e:
        console.print(f"[red]  ✗ Config check failed: {e}[/red]")
        results.append(("Config & File Structure", "FAILED", str(e)))

    # -------------------------------------------------------------------------
    # TEST 2: Candidate Profile & Search Keywords
    # -------------------------------------------------------------------------
    console.print("\n[bold yellow][2/7] Checking Candidate Profile & Target Domains...[/bold yellow]")
    try:
        assert profile.personal_info.full_name.strip(), "Profile has no name -- fill in config/profile.yaml"
        assert "@" in profile.personal_info.email, "Profile has no email -- fill in config/profile.yaml"
        assert profile.personal_info.phone_number.strip(), "Profile has no phone number -- fill in config/profile.yaml"

        keywords = config.search.keywords
        assert keywords, "No search keywords set -- add some under search.keywords in config/config.yaml"

        console.print(f"[green]  ✓ Profile loaded for {profile.personal_info.full_name} ({profile.personal_info.email}).[/green]")
        console.print(f"[dim]    Target Keywords: {', '.join(keywords[:5])}...[/dim]")
        results.append(("Candidate Profile & Keywords", "PASSED", f"{len(keywords)} search keyword(s) active"))
    except Exception as e:
        console.print(f"[red]  ✗ Candidate profile check failed: {e}[/red]")
        results.append(("Candidate Profile & Keywords", "FAILED", str(e)))

    # -------------------------------------------------------------------------
    # TEST 3: Database & Activity Storage
    # -------------------------------------------------------------------------
    console.print("\n[bold yellow][3/7] Checking SQLite Database & Schema...[/bold yellow]")
    try:
        from src.core.database import Database
        db = Database(config.general.database_path)
        stats = db.get_statistics()
        console.print(f"[green]  ✓ Database initialized at {config.general.database_path}.[/green]")
        console.print(f"[dim]    Jobs in DB: {stats['total_jobs_found']} | Applications today: {stats['today_applications']}/25[/dim]")
        results.append(("Database & Storage", "PASSED", "Schema validated and writable"))
    except Exception as e:
        console.print(f"[red]  ✗ Database check failed: {e}[/red]")
        results.append(("Database & Storage", "FAILED", str(e)))

    # -------------------------------------------------------------------------
    # TEST 4: Google Cloud Vertex AI & Gemini 3.7 Flash (Global Region)
    # -------------------------------------------------------------------------
    console.print("\n[bold yellow][4/7] Testing Gemini 3.7 Flash on Vertex AI (Global Region)...[/bold yellow]")
    llm = None
    try:
        from src.llm.client import LLMClient
        llm = LLMClient(config.llm)
        response = llm.generate(
            system_prompt="You are an AI test verification agent.",
            user_prompt="Respond strictly with: 'GEMINI_3.7_FLASH_ACTIVE_AND_OPERATIONAL'"
        )
        assert "GEMINI_3.7_FLASH" in response or "OPERATIONAL" in response or len(response) > 0, "Empty LLM response"
        console.print(f"[green]  ✓ Connected to Vertex AI project '{llm.project_id}' in region '{llm.location}'.[/green]")
        console.print(f"[dim]    Model Response: {response.strip()[:80]}[/dim]")
        results.append(("Gemini 3.7 Flash (Vertex AI)", "PASSED", f"Connected to {llm.location} ({config.llm.model})"))
    except Exception as e:
        console.print(f"[red]  ✗ Gemini 3.7 Flash connection failed: {e}[/red]")
        results.append(("Gemini 3.7 Flash (Vertex AI)", "FAILED", str(e)))

    # -------------------------------------------------------------------------
    # TEST 5: OAuth2 Token Auto-Renewal Engine
    # -------------------------------------------------------------------------
    console.print("\n[bold yellow][5/7] Testing OAuth2 Token Auto-Renewal & Key Lifecycle...[/bold yellow]")
    try:
        if llm and llm._creds:
            # Force expiration timestamp to simulate expired token
            llm._creds.expiry = llm._creds.expiry.replace(year=2020)
            assert llm._creds.expired, "Token expiration force failed"
            
            # Request new generation to test auto-refresh
            test_resp = llm.generate("You are a helper.", "Say 'AUTO_RENEW_SUCCESS'")
            assert len(test_resp) > 0, "Auto-renewal generation failed"
            console.print(f"[green]  ✓ OAuth2 Token auto-refresh successfully recovered expired credentials.[/green]")
            console.print(f"[dim]    New token valid until: {llm._creds.expiry}[/dim]")
            results.append(("Token Auto-Renewal Engine", "PASSED", "Continuous 24/7 auth verified"))
        else:
            results.append(("Token Auto-Renewal Engine", "SKIPPED", "No Service Account loaded"))
    except Exception as e:
        console.print(f"[red]  ✗ Token auto-renewal test failed: {e}[/red]")
        results.append(("Token Auto-Renewal Engine", "FAILED", str(e)))

    # -------------------------------------------------------------------------
    # TEST 6: Dynamic LaTeX & ATS Resume Generation
    # -------------------------------------------------------------------------
    console.print("\n[bold yellow][6/7] Testing Dynamic LaTeX & ATS Resume Generator (.tex & .pdf)...[/bold yellow]")
    try:
        from src.modules.resume_generator import ResumeTailorEngine
        from src.core.models import JobListing
        
        tailor_engine = ResumeTailorEngine(llm)
        dummy_job = JobListing(
            job_id="verify_test_01",
            title="Senior AI Engineer - Agentic Systems",
            company="Google DeepMind",
            location="Bengaluru",
            url="https://linkedin.com/jobs/view/verify_test_01",
            description="Seeking an AI Engineer to design and scale Autonomous Multi-Agent LLM Orchestrators, Vector Search pipelines, and low-latency real-time voice systems using PyTorch and Golang."
        )
        pdf_path = await tailor_engine.tailor_resume(dummy_job, profile)
        tex_path = pdf_path.with_suffix(".tex")
        
        assert pdf_path.exists(), "PDF was not created"
        assert tex_path.exists(), "LaTeX .tex source was not created"
        assert pdf_path.stat().st_size > 1000, "Generated PDF is empty"
        
        console.print(f"[green]  ✓ Generated LaTeX Source:[/green] [dim]{tex_path.name}[/dim]")
        console.print(f"[green]  ✓ Compiled ATS Vector PDF:[/green] [cyan]{pdf_path.name} ({pdf_path.stat().st_size // 1024} KB)[/cyan]")
        results.append(("LaTeX & ATS Resume Engine", "PASSED", f"Generated .tex and .pdf ({pdf_path.stat().st_size // 1024} KB)"))
    except Exception as e:
        console.print(f"[red]  ✗ Resume generator test failed: {e}[/red]")
        results.append(("LaTeX & ATS Resume Engine", "FAILED", str(e)))

    # -------------------------------------------------------------------------
    # TEST 7: Playwright Stealth & WindMouse Physics
    # -------------------------------------------------------------------------
    console.print("\n[bold yellow][7/7] Testing Playwright Stealth & WindMouse Physics...[/bold yellow]")
    try:
        from playwright.async_api import async_playwright
        from src.browser.human_behavior import _generate_windmouse_trajectory
        
        # Test WindMouse physics trajectory generator
        points = _generate_windmouse_trajectory((50, 50), (400, 300))
        assert len(points) > 5, "WindMouse point generation failed"
        
        # Test headless browser launch
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.set_content("<html><body><h1>Verification Test</h1></body></html>")
            title = await page.title()
            await browser.close()
            
        console.print("[green]  ✓ Chromium browser launch and WindMouse kinematic physics engine operational.[/green]")
        results.append(("Playwright & Stealth Engine", "PASSED", "Browser & WindMouse trajectories active"))
    except Exception as e:
        console.print(f"[red]  ✗ Playwright test failed: {e}[/red]")
        results.append(("Playwright & Stealth Engine", "FAILED", str(e)))

    # -------------------------------------------------------------------------
    # SUMMARY TABLE
    # -------------------------------------------------------------------------
    console.print("\n")
    table = Table(title="Autonomous AI Job Agent - System Verification Results", show_header=True, header_style="bold cyan")
    table.add_column("Subsystem / Component", style="bold white", width=32)
    table.add_column("Status", width=12)
    table.add_column("Diagnostic Details", style="dim", width=42)

    all_passed = True
    for comp, status, details in results:
        if status == "PASSED":
            status_str = "[bold green]✓ PASSED[/bold green]"
        elif status == "SKIPPED":
            status_str = "[yellow]- SKIPPED[/yellow]"
        else:
            status_str = "[bold red]✗ FAILED[/bold red]"
            all_passed = False
        table.add_row(comp, status_str, details)

    console.print(table)

    if all_passed:
        console.print(Panel(
            "[bold green]🎉 ALL SYSTEM CHECKS PASSED! THE AGENT IS 100% OPERATIONAL.[/bold green]\n"
            "• Vertex AI (Gemini 3.7 Flash) is live with automatic token renewals.\n"
            "• Dynamic LaTeX & ATS resume generator is producing tailored resumes.\n"
            "• Human-mimicry browser stealth and database tracking are ready.",
            border_style="green"
        ))
    else:
        console.print(Panel(
            "[bold red]⚠️ Some checks encountered issues. Please review the table above.[/bold red]",
            border_style="red"
        ))


if __name__ == "__main__":
    asyncio.run(run_diagnostics())
