"""
Browser session manager for Naukri.com with persistent cookies, enterprise stealth, and session profile.
"""

import asyncio
from typing import Optional
from playwright.async_api import async_playwright, BrowserContext, Page, Playwright
from playwright_stealth import Stealth

from src.core import paths
from src.core.logger import logger, console
from src.browser.utils import human_delay
from src.browser.session import (
    apply_fingerprint,
    apply_stealth_layer,
    build_stealth,
    clear_stale_profile_lock,
)


class NaukriSession:
    def __init__(self, user_data_dir: Optional[str] = None, headless: bool = False):
        # Re-rooted rather than resolved against the working directory: several
        # menu entries construct this with a bare "./data/naukri_browser_profile"
        # literal, which bypassed the normalisation load_app_config() does and
        # put a Chromium profile inside the read-only bundle.
        self.user_data_dir = paths.resolve_writable(
            user_data_dir, paths.browser_profile_dir("naukri")
        )
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        self.headless = headless

        self.playwright: Optional[Playwright] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        # `build_stealth()` takes no arguments -- passing `languages=` raised a
        # TypeError here, so constructing a NaukriSession at all failed on both
        # platforms and every Naukri command was dead on arrival. The language
        # list is not needed anyway: the context below sets locale="en-IN",
        # which is what drives Accept-Language, and the navigator.languages
        # evasion is deliberately off because it contradicts that header.
        self.stealth = build_stealth()

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def start(self) -> Page:
        if not self.playwright:
            self.playwright = await async_playwright().start()

        args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-notifications",
            "--disable-popup-blocking",
            "--start-maximized",
        ]

        logger.info(f"Launching browser for Naukri (headless={self.headless})...")
        
        launch_kwargs = {
            "user_data_dir": str(self.user_data_dir),
            "headless": self.headless,
            "slow_mo": 0,
            "no_viewport": True,
            "args": args,
            "locale": "en-IN",
            "timezone_id": "Asia/Kolkata",
            "has_touch": False,
            "is_mobile": False,
        }

        # Same force-quit residue as the LinkedIn session: a lock owned by a
        # dead pid never releases, so retrying into it only spends the retries.
        clear_stale_profile_lock(self.user_data_dir)

        last_err = None
        for attempt in range(3):
            try:
                self.context = await self.playwright.chromium.launch_persistent_context(
                    channel="chrome",
                    **launch_kwargs
                )
                logger.info("✓ Naukri session launched with native Google Chrome channel.")
                break
            except Exception as chrome_err:
                last_err = chrome_err
                try:
                    self.context = await self.playwright.chromium.launch_persistent_context(
                        **launch_kwargs
                    )
                    break
                except Exception as bundled_err:
                    last_err = bundled_err
                    logger.debug(f"Naukri launch attempt {attempt + 1} failed ({bundled_err}), waiting for profile lock release...")
                    clear_stale_profile_lock(self.user_data_dir)
                    await asyncio.sleep(1.0)
        else:
            raise RuntimeError(f"Failed to launch Naukri browser persistent context: {last_err}")

        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()

        await apply_fingerprint(self.context, self.page)
        await apply_stealth_layer(self.context, self.stealth, self.page)

        self.page.set_default_timeout(30000)
        return self.page

    async def close(self):
        if self.context:
            try:
                await self.context.close()
            except Exception as e:
                logger.debug(f"Error closing Naukri context: {e}")
            self.context = None
        self.page = None
        if self.playwright:
            try:
                await self.playwright.stop()
            except Exception as e:
                logger.debug(f"Error stopping playwright: {e}")
            self.playwright = None

    async def check_logged_in(self) -> bool:
        page = await self.ensure_active_page()
        if not page:
            return False
        
        try:
            await page.goto("https://www.naukri.com/mnjuser/homepage", wait_until="domcontentloaded")
            await asyncio.sleep(2.0)
            
            # Check if user dashboard or profile drawer is present
            if "/mnjuser/homepage" in page.url or "/mnjuser/profile" in page.url:
                logger.info("[green]✓ Verified active Naukri login session.[/green]")
                return True

            profile_icon = page.locator(".nI-gNb-drawer, .view-profile-wrapper")
            if await profile_icon.count() > 0:
                return True
                
            return False
        except Exception as e:
            logger.warning(f"Naukri session check error: {e}")
            return False

    async def ensure_active_page(self) -> Page:
        """Guarantees an active, open, and responsive Playwright Page is available for Naukri."""
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
                        self.page.set_default_timeout(30000)
                        return self.page
                self.page = await self.context.new_page()
                self.page.set_default_timeout(30000)
                return self.page
            except Exception as e:
                logger.info(f"Naukri browser window was closed or disconnected ({e}). Re-launching session...")

        if self.context:
            try:
                await self.context.close()
            except Exception:
                pass
            self.context = None
        self.page = None

        return await self.start()

    async def interactive_login(self):
        console.print("\n[bold yellow]══ Naukri.com Manual Login & Session Setup ══[/bold yellow]")
        console.print("Opening Naukri login page in the browser...")
        console.print("[dim]Please enter your mobile/email, password, or Google login and OTP in the opened browser window.[/dim]\n")

        page = await self.ensure_active_page()
        await page.goto("https://www.naukri.com/nlogin/login", wait_until="domcontentloaded")

        max_wait_seconds = 300
        interval = 2
        elapsed = 0

        with console.status("[bold cyan]Waiting for you to log in to Naukri in the browser...[/bold cyan]"):
            while elapsed < max_wait_seconds:
                await asyncio.sleep(interval)
                elapsed += interval
                
                try:
                    if self.page and not self.page.is_closed() and ("/mnjuser/homepage" in self.page.url or "/mnjuser/profile" in self.page.url):
                        console.print("\n[bold green]✓ Naukri Login detected successfully! Session saved for future runs.[/bold green]")
                        console.print("[bold yellow]⚠️  KEEP BROWSER OPEN: Do NOT close the browser window![/bold yellow]\n")
                        await human_delay(2, 4)
                        await self.ensure_active_page()
                        return True
                except Exception:
                    pass

        console.print("\n[bold red]✗ Login timed out (5 minutes).[/bold red]\n")
        return False
