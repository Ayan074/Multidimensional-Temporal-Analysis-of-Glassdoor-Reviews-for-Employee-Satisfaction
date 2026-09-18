"""
Glassdoor Review Scraper Engine (v3 — HTTP mode)
=================================================
Approach:
  1. Opens browser ONLY for manual login (CAPTCHA solving)
  2. Extracts cookies from browser session
  3. Closes browser, uses HTTP requests for all page fetching
  4. No browser fingerprinting possible — just HTTP + valid cookies
"""

import json
import time
import random
import logging
import re
import requests as http_requests
from pathlib import Path
from typing import Optional

import undetected_chromedriver as uc
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException,
    NoSuchElementException,
    WebDriverException,
    StaleElementReferenceException,
    InvalidSessionIdException,
)

from . import config
from .parser import parse_reviews_page, get_total_review_count
from .exporter import save_reviews_incremental

logger = logging.getLogger(__name__)


class GlassdoorScraper:
    """
    Automated Glassdoor review scraper with anti-detection,
    checkpoint/resume, and incremental data saving.
    """

    def __init__(self, company_key: str, headless: bool = None, debug: bool = False):
        if company_key not in config.COMPANIES:
            raise ValueError(
                f"Unknown company '{company_key}'. "
                f"Available: {list(config.COMPANIES.keys())}"
            )

        self.company_key = company_key
        self.company = config.COMPANIES[company_key]
        self.headless = headless if headless is not None else config.HEADLESS
        self.debug = debug
        self.driver: Optional[uc.Chrome] = None
        self.total_reviews_scraped = 0
        self.failed_pages = []
        # Tracks when gate is confirmed in HTML (for persistent gate detection)
        self._gate_confirmed = False
        # Proactive session rotation: rotate every N pages BEFORE gate fires
        self._pages_since_rotation = 0
        self._rotation_count = 0
        self.ROTATION_INTERVAL = 40  # rotate every 40 pages (gate fires ~50-80)

        # Debug directory
        if self.debug:
            self.debug_dir = config.DATA_DIR / "debug"
            self.debug_dir.mkdir(parents=True, exist_ok=True)

    # --- Checkpoint System ------------------------------------------------

    def _get_checkpoint_path(self) -> Path:
        return config.CHECKPOINT_DIR / f"{self.company_key}_checkpoint.json"

    def save_checkpoint(self, page: int, total_scraped: int):
        checkpoint = {
            "company_key": self.company_key,
            "last_page": page,
            "total_scraped": total_scraped,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "failed_pages": self.failed_pages,
        }
        checkpoint_path = self._get_checkpoint_path()
        checkpoint_path.write_text(json.dumps(checkpoint, indent=2))
        logger.debug(f"Checkpoint saved: page {page}, {total_scraped} reviews")

    def load_checkpoint(self) -> Optional[dict]:
        checkpoint_path = self._get_checkpoint_path()
        if checkpoint_path.exists():
            try:
                data = json.loads(checkpoint_path.read_text())
                logger.info(
                    f"Checkpoint found: last page {data['last_page']}, "
                    f"{data['total_scraped']} reviews scraped"
                )
                return data
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning(f"Corrupt checkpoint file, ignoring: {e}")
        return None

    def clear_checkpoint(self):
        checkpoint_path = self._get_checkpoint_path()
        if checkpoint_path.exists():
            checkpoint_path.unlink()
            logger.info("Checkpoint cleared")

    # --- HTTP Session Setup -------------------------------------------------

    def _setup_http_session(self):
        """
        Create an HTTP requests.Session with cookies from the browser.
        This is the KEY to avoiding bot detection: after login, we close
        the browser and use plain HTTP requests — no fingerprinting possible.
        """
        self._http = http_requests.Session()

        # Copy cookies from browser
        for cookie in self.driver.get_cookies():
            self._http.cookies.set(
                cookie['name'],
                cookie['value'],
                domain=cookie.get('domain', '.glassdoor.com'),
                path=cookie.get('path', '/'),
            )

        # Get User-Agent from browser (matches the real Chrome)
        try:
            ua = self.driver.execute_script("return navigator.userAgent")
        except Exception:
            ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/151.0.0.0 Safari/537.36")

        # Set headers to mimic a real browser navigation
        self._http.headers.update({
            'User-Agent': ua,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'same-origin',
            'Sec-Fetch-User': '?1',
            'Cache-Control': 'max-age=0',
        })

        logger.info(f"HTTP session created with {len(self._http.cookies)} cookies")
        print(f"[HTTP] Session ready with {len(self._http.cookies)} cookies")

    def _scrape_page_http(self, page_number: int) -> list:
        """
        Fetch and parse a single review page using HTTP requests.
        Returns list of parsed review dicts.
        """
        url = config.get_reviews_url(self.company_key, page_number)

        # Set Referer to mimic natural navigation
        if page_number > 1:
            referer = config.get_reviews_url(self.company_key, page_number - 1)
        else:
            referer = f"https://www.glassdoor.com/Reviews/{self.company['slug']}-Reviews-E{self.company['employer_id']}.htm"
        self._http.headers['Referer'] = referer

        for attempt in range(1, config.MAX_RETRIES + 1):
            try:
                response = self._http.get(url, timeout=config.PAGE_LOAD_TIMEOUT)
                logger.info(f"Page {page_number}: HTTP {response.status_code} ({len(response.text)} bytes)")

                if response.status_code == 403:
                    logger.warning(f"Page {page_number}: 403 Forbidden — cookies may have expired")
                    print(f"\n[WARN] 403 on page {page_number} — session may need refresh")
                    return []

                if response.status_code != 200:
                    logger.warning(f"Page {page_number}: HTTP {response.status_code}")
                    if attempt < config.MAX_RETRIES:
                        self._random_delay(10, 20)
                        continue
                    return []

                html = response.text
                html_lower = html.lower()

                # Check for gate
                if ("get full access by completing" in html_lower
                        or "new-survey-start-wrap" in html_lower
                        or "please select a review type" in html_lower):
                    self._gate_confirmed = True
                    logger.warning(f"Page {page_number}: Give-to-Get gate in response")
                    if attempt < config.MAX_RETRIES:
                        self._random_delay(15, 30)
                        continue
                    return []

                # Check for CF challenge
                if ("verify you are human" in html_lower
                        or "just a moment" in html_lower):
                    logger.warning(f"Page {page_number}: Cloudflare challenge in HTTP response")
                    print(f"\n[CF] Cloudflare challenge on page {page_number} — session expired")
                    return []

                # Parse reviews
                reviews = parse_reviews_page(html)

                if reviews:
                    # Extract sub-ratings from embedded JSON
                    subratings_by_id = self._extract_all_subratings(html)
                    sr_count = 0
                    for review in reviews:
                        rid = review.get("review_id", "")
                        if rid and rid in subratings_by_id:
                            review.update(subratings_by_id[rid])
                            sr_count += 1
                    logger.info(
                        f"Page {page_number}: {len(reviews)} reviews "
                        f"({sr_count} with sub-ratings)"
                    )
                    return reviews
                else:
                    logger.warning(f"Page {page_number}: 0 reviews parsed (attempt {attempt})")
                    if attempt < config.MAX_RETRIES:
                        self._random_delay(8, 15)

            except http_requests.exceptions.RequestException as e:
                logger.error(f"Page {page_number}: HTTP error: {e}")
                if attempt < config.MAX_RETRIES:
                    self._random_delay(10, 20)

        return []

    # --- Session Rotation (Gate Recovery) -----------------------------------

    # Cookies that provide authentication — NEVER delete these.
    _AUTH_COOKIES = frozenset({
        "at",           # Auth token
        "asst",         # Session auth
        "gdId",         # User identity
        "g_state",      # Google auth state
        "cf_clearance", # Cloudflare clearance (expensive to reacquire)
        "__cf_bm",      # Cloudflare bot management
        "_cfuvid",      # Cloudflare unique visitor
    })

    # Cookies that track page views / session activity — delete these to
    # reset the server-side gate counter while staying logged in.
    _TRACKING_COOKIES = frozenset({
        "gdsid",            # Glassdoor session ID (gate counter tied to this)
        "rsSessionId",      # Review session tracking
        "rsReferrerData",   # Referrer tracking
        "rl_session",       # RudderStack session
        "rl_page_init_referrer",
        "rl_page_init_referring_domain",
        "rl_anonymous_id",  # Anonymous tracking ID
        "rl_trait",
        "rl_user_id",
        "ki_s",             # Kissmetrics session
        "ki_t",             # Kissmetrics tracking
        "ki_r",             # Kissmetrics referrer
        "_dd_s",            # Datadog session
        "rttdf",            # Review tracking
        "cdArr",            # Content delivery tracking
        "indeedCtk",        # Indeed tracking
        "otGeoUS",          # Geo tracking
    })

    def _rotate_session(self):
        """
        Reset the Glassdoor gate counter by selectively deleting tracking
        cookies while keeping authentication cookies.

        WHY SELECTIVE:
        - Deleting ALL cookies → anonymous user → only 1 free page → useless
        - Keeping auth cookies → stays logged in → many pages before gate
        - Deleting tracking cookies → resets the server-side view counter
          that triggers the gate, giving a fresh window of pages
        """
        logger.info("Session rotation: deleting tracking cookies (keeping auth)...")
        print("\n[ROTATE] Resetting session tracking (keeping login)...")

        try:
            # Get all current cookies
            all_cookies = self.driver.get_cookies()
            auth_cookies_backup = []
            deleted_count = 0

            # Identify which cookies to keep vs delete
            for cookie in all_cookies:
                name = cookie.get("name", "")
                if name in self._AUTH_COOKIES:
                    auth_cookies_backup.append(cookie)
                    logger.debug(f"  Keeping auth cookie: {name}")

            # Delete ALL cookies first (Selenium has no single-cookie delete)
            self.driver.delete_all_cookies()
            deleted_count = len(all_cookies) - len(auth_cookies_backup)

            # Re-add auth cookies
            for cookie in auth_cookies_backup:
                try:
                    cookie.pop("sameSite", None)
                    self.driver.add_cookie(cookie)
                except Exception:
                    pass

            logger.info(
                f"Deleted {deleted_count} tracking cookies, "
                f"kept {len(auth_cookies_backup)} auth cookies"
            )

            # Navigate to homepage to get fresh tracking cookies assigned
            # The server sees the auth token (logged in) but no session
            # tracking → assigns new gdsid, rsSessionId, etc. with a
            # fresh page view counter.
            self.driver.get("https://www.glassdoor.com/")
            time.sleep(random.uniform(3, 5))

            # Save the refreshed cookies
            self._save_cookies()

            logger.info("Session rotation complete — fresh tracking, still logged in")
            print("[OK] Tracking reset. Still logged in. Resuming...\n")

        except Exception as e:
            logger.error(f"Session rotation failed: {e}")

    def _handle_persistent_gate(self, page_number: int):
        """
        Called when the Give-to-Get gate fires on consecutive pages.

        Strategy:
        1. First: automatically rotate session (clear cookies, fresh anonymous)
        2. If that keeps failing: prompt user to submit a contribution
        """
        # Track how many times we've rotated in this run
        self._rotation_count += 1

        if self._rotation_count <= 3:
            # Automatic session rotation
            self._rotate_session()
        else:
            # Session rotation exhausted — need user contribution
            print("\n" + "="*68)
            print("  [ACCOUNT LOCKED]  Glassdoor Requires Your Contribution")
            print("="*68)
            print()
            print("  Session rotation is not working — Glassdoor may be tracking")
            print("  your IP or browser fingerprint. Manual action needed.")
            print()
            print("  WHAT TO DO NOW:")
            print("  1. Look at the Chrome browser window (it shows the gate form)")
            print("  2. Click one of the boxes:  Company review / Salary /")
            print("     Interview / Benefits")
            print("  3. Fill in the form honestly — even a short, genuine review")
            print("     of any company you have worked at qualifies")
            print("  4. Submit the form")
            print()
            print("  After submitting:")
            print("  ✔ Your account gets 12 months of full, unrestricted access")
            print("  ✔ The scraper will resume automatically from page", page_number)
            print()
            print("  NOTE: If you want to skip, press Ctrl+C to stop.")
            print("="*68)
            input("\n  >>> Press ENTER after you have submitted your contribution... ")
            print()
            print("[OK] Contribution received. Resuming scraping...")
            # Save fresh cookies
            try:
                self._save_cookies()
            except Exception:
                pass
            # Reset rotation counter
            self._rotation_count = 0

        self._gate_confirmed = False

    # --- Browser Management ----------------------------------------------

    def start_browser(self):
        """
        Initialize Chrome browser.
        Strategy: Use the user's REAL Chrome profile so Cloudflare sees a
        legitimate browser with real cookies, history, and fingerprint.

        IMPORTANT: The user must close all Chrome windows first!
        """
        import os

        logger.info("Starting browser...")

        options = uc.ChromeOptions()

        if self.headless:
            options.add_argument("--headless=new")

        # ── Core Chrome arguments ──────────────────────────────────────────
        # Use a CLEAN browser — no Chrome profile copy.
        # Profile copies carry proxy/extension/DNS settings that cause
        # ERR_NAME_NOT_RESOLVED errors. Manual login + saved cookies
        # handles authentication instead.
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--lang=en-US,en")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--no-first-run")
        options.add_argument("--no-default-browser-check")
        options.add_argument("--disable-popup-blocking")
        options.add_argument("--disable-extensions")
        options.add_argument("--dns-prefetch-disable")

        # ── User-Agent: let undetected_chromedriver auto-detect ─────────
        # Do NOT hardcode a UA string — if the installed Chrome version
        # changes (e.g. auto-update), a mismatched UA is a bot signal.

        try:
            # Pin to Chrome 151 (user's installed version)
            self.driver = uc.Chrome(options=options, version_main=151)
            self.driver.set_page_load_timeout(config.PAGE_LOAD_TIMEOUT)
            self.driver.maximize_window()

            # ── Comprehensive stealth JS injection ─────────────────────────
            try:
                self.driver.execute_cdp_cmd(
                    "Page.addScriptToEvaluateOnNewDocument",
                    {
                        "source": """
                            // 1. Remove webdriver flag (primary CF detection vector)
                            Object.defineProperty(navigator, 'webdriver', {
                                get: () => undefined
                            });

                            // 2. Restore chrome object (missing in selenium)
                            window.chrome = {
                                runtime: {},
                                loadTimes: function() {},
                                csi: function() {},
                                app: {}
                            };

                            // 3. Restore realistic plugins list
                            Object.defineProperty(navigator, 'plugins', {
                                get: () => [
                                    { name: 'PDF Viewer', filename: 'internal-pdf-viewer' },
                                    { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai' },
                                    { name: 'Native Client', filename: 'internal-nacl-plugin' }
                                ]
                            });

                            // 4. Realistic language list
                            Object.defineProperty(navigator, 'languages', {
                                get: () => ['en-US', 'en']
                            });

                            // 5. Fix permissions query (CF checks this)
                            const origPermsQuery = window.navigator.permissions.query;
                            window.navigator.permissions.query = (params) => (
                                params.name === 'notifications'
                                    ? Promise.resolve({ state: Notification.permission })
                                    : origPermsQuery(params)
                            );
                        """
                    },
                )
            except Exception:
                pass

            logger.info("Browser started successfully (clean profile)")
        except Exception as e:
            logger.error(f"Failed to start browser: {e}")
            raise

    def restart_browser(self, ask_login: bool = True):
        """Close and restart the browser (for crash recovery)."""
        logger.info("Restarting browser...")
        self.close_browser()
        time.sleep(3)
        self.start_browser()
        if ask_login:
            # Try loading saved cookies first (avoids re-triggering CF)
            loaded = self._load_cookies()
            if not loaded:
                self._do_manual_login()
            else:
                logger.info("Session restored from saved cookies — skipping manual login prompt")

    def close_browser(self):
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None
            logger.info("Browser closed")

    # --- Cookie Persistence ----------------------------------------------

    def _save_cookies(self):
        """
        Save all current browser cookies to disk so the Glassdoor session
        can be restored without a new manual login (and without triggering
        Cloudflare's bot detection on every fresh browser start).
        """
        try:
            cookies = self.driver.get_cookies()
            cookie_file = config.DATA_DIR / "glassdoor_cookies.json"
            cookie_file.write_text(json.dumps(cookies, indent=2))
            logger.info(f"Session saved: {len(cookies)} cookies → {cookie_file}")
        except Exception as e:
            logger.warning(f"Could not save cookies: {e}")

    def _load_cookies(self) -> bool:
        """
        Load previously saved cookies into the browser.
        Must be called AFTER the browser has visited the Glassdoor domain
        at least once (cookies can only be set for the current domain).
        Returns True if cookies were loaded successfully.
        """
        cookie_file = config.DATA_DIR / "glassdoor_cookies.json"
        if not cookie_file.exists():
            logger.info("No saved cookie file found — will do manual login.")
            return False
        try:
            # First navigate to the domain so we can set cookies
            logger.info("Loading saved Glassdoor session cookies...")
            self.driver.get("https://www.glassdoor.com/")
            time.sleep(3)

            cookies = json.loads(cookie_file.read_text())
            loaded = 0
            for cookie in cookies:
                try:
                    # Remove keys that cause issues in some Chrome versions
                    cookie.pop("sameSite", None)
                    self.driver.add_cookie(cookie)
                    loaded += 1
                except Exception:
                    pass

            logger.info(f"Loaded {loaded}/{len(cookies)} cookies from disk")

            # Refresh so the cookies take effect
            self.driver.refresh()
            time.sleep(4)

            # Verify we are actually logged in
            page_text = ""
            try:
                page_text = self.driver.find_element(By.TAG_NAME, "body").text.lower()
            except Exception:
                pass

            # If Glassdoor shows a sign-in button we're not logged in
            if "sign in" in page_text and "my profile" not in page_text:
                logger.warning("Loaded cookies but session appears expired — need manual login")
                return False

            logger.info("Session restored successfully from saved cookies!")
            return True

        except Exception as e:
            logger.warning(f"Could not load cookies: {e}")
            return False

    # --- Login -----------------------------------------------------------

    def _do_manual_login(self):
        """
        Navigate to Glassdoor and let the user log in manually.
        This is the most reliable approach since Glassdoor's login
        changes frequently and has CAPTCHA protection.
        """
        email = config.GLASSDOOR_EMAIL
        password = config.GLASSDOOR_PASSWORD

        # If credentials exist, try automated login first
        if email and password:
            logger.info("Attempting automated login...")
            try:
                self.driver.get(config.LOGIN_URL)
                self._random_delay(3, 5)

                wait = WebDriverWait(self.driver, 15)

                # Email
                try:
                    email_input = wait.until(
                        EC.presence_of_element_located((By.ID, "inlineUserEmail"))
                    )
                    email_input.clear()
                    self._human_type(email_input, email)
                    self._random_delay(0.5, 1.5)

                    # Submit email
                    try:
                        btn = self.driver.find_element(
                            By.CSS_SELECTOR,
                            'button[type="submit"], [data-test="email-form-button"]'
                        )
                        btn.click()
                        self._random_delay(2, 4)
                    except NoSuchElementException:
                        pass

                    # Password
                    try:
                        pw_input = wait.until(
                            EC.presence_of_element_located((By.ID, "inlineUserPassword"))
                        )
                        pw_input.clear()
                        self._human_type(pw_input, password)
                        self._random_delay(0.5, 1)

                        btn = self.driver.find_element(
                            By.CSS_SELECTOR,
                            'button[type="submit"], [data-test="sign-in-button"]'
                        )
                        btn.click()
                        self._random_delay(4, 6)
                    except (TimeoutException, NoSuchElementException):
                        pass

                    # Check success
                    if "login" not in self.driver.current_url.lower():
                        logger.info("Automated login successful!")
                        return True
                except (TimeoutException, NoSuchElementException):
                    pass

                logger.warning("Automated login didn't complete cleanly.")

            except Exception as e:
                logger.warning(f"Automated login failed: {e}")

        # Fallback: Manual login
        if self.headless:
            logger.warning(
                "Cannot do manual login in headless mode. "
                "Run with --no-headless to log in manually."
            )
            return False

        print("\n" + "=" * 60)
        print("  MANUAL LOGIN REQUIRED")
        print("=" * 60)
        print("  The browser is open. Please:")
        print("  1. Log into your Glassdoor account")
        print("  2. Solve any CAPTCHA if prompted")
        print("  3. Make sure you can see the Glassdoor homepage")
        print("  4. Come back here and press ENTER")
        print("=" * 60)

        try:
            self.driver.get("https://www.glassdoor.com/profile/login_input.htm")
        except Exception:
            self.driver.get("https://www.glassdoor.com/")

        input("\n>>> Press ENTER after you have logged in... ")
        print("[OK] Continuing with scraping...\n")

        # Save cookies immediately so future runs (and browser restarts)
        # don't need manual login and are less likely to trigger Cloudflare.
        self._save_cookies()
        return True

    # --- Cloudflare / CAPTCHA Handler ------------------------------------

    def _handle_cloudflare(self, page_number: int, attempt: int):
        """
        Pause scraping and ask the user to solve the Cloudflare challenge
        (either the Turnstile checkbox or an automatic browser check).

        Strategy after the user solves it:
          1. Wait a moment
          2. Navigate to Glassdoor HOMEPAGE to warm the session
          3. Save cookies (so the fresh CF clearance cookie is persisted)
          4. Wait again before the caller retries the review page

        This "warm up" step is critical — if we immediately reload the
        deep review URL after solving CF, Cloudflare often challenges again
        because the clearance cookie hasn't propagated yet.
        """
        if self.headless:
            logger.error(
                f"Page {page_number}: Cloudflare challenge in headless mode -- "
                "cannot solve. Switch to --no-headless."
            )
            return

        print("\n" + "=" * 60)
        print("  [CLOUDFLARE] Human Verification Required")
        print("=" * 60)
        print(f"  Page {page_number} hit Cloudflare's 'Humans only' check.")
        print()
        print("  WHAT TO DO:")
        print("  1. Look at the Chrome browser window")
        print("  2. Click the [ ] 'Verify you are human' checkbox")
        print("  3. Wait until the checkbox shows a green tick (\u2713)")
        print("  4. The page will auto-redirect to the Glassdoor reviews")
        print("  5. Once you see real reviews (NOT the checkbox) come back here")
        print("  6. Press ENTER")
        print()
        print("  NOTE: If no checkbox appears, just wait ~10 seconds for the")
        print("  automatic check to pass, then press ENTER.")
        print("=" * 60)
        input("\n>>> Press ENTER only AFTER the reviews are visible in the browser... ")

        # Wait a beat then warm the session via homepage
        print("[CF] Warming session via homepage before retrying...")
        try:
            self.driver.get("https://www.glassdoor.com/")
            time.sleep(random.uniform(5, 10))
            # Save the CF clearance cookie so it persists
            self._save_cookies()
        except Exception:
            pass

        print("[OK] Resuming scraping...\n")
        # Extra back-off so CF doesn't immediately challenge the next page
        time.sleep(random.uniform(8, 15))

    # --- Debug Helpers ---------------------------------------------------

    def _save_debug_html(self, page_number: int, attempt: int = 1):
        """Save the current page HTML for debugging."""
        if not self.debug:
            return
        try:
            html = self.driver.page_source
            filepath = self.debug_dir / f"page_{page_number}_attempt_{attempt}.html"
            filepath.write_text(html, encoding="utf-8")
            logger.debug(f"Debug HTML saved: {filepath}")

            # Also save a screenshot
            screenshot_path = self.debug_dir / f"page_{page_number}_attempt_{attempt}.png"
            self.driver.save_screenshot(str(screenshot_path))
            logger.debug(f"Debug screenshot saved: {screenshot_path}")
        except Exception as e:
            logger.debug(f"Could not save debug info: {e}")

    def _detect_page_state(self) -> str:
        """
        Analyze current page to determine what Glassdoor is showing.
        Returns: 'reviews', 'login_wall', 'captcha', 'blocked', 'gated', 'unknown'

        DETECTION STRATEGY (order matters!):
        ────────────────────────────────────
        Glassdoor's SSR (server-side rendering) embeds BOTH the review data
        AND the gate form into the same HTML response. The reviews exist in
        the DOM but are hidden behind the gate overlay. This means:

        - find_elements('[data-test="review-detail"]') returns elements even
          on gated pages (they have text content too — just visually hidden)
        - body.text may not include gate text yet if React hasn't hydrated
        - page_source ALWAYS has gate markers immediately (it's the raw SSR)

        Therefore we MUST check page_source for gate markers BEFORE checking
        the live DOM for review elements.
        """
        try:
            url = self.driver.current_url.lower()

            # Check for login redirect
            if "login" in url or "signin" in url:
                return "login_wall"

            # ── Read page title ────────────────────────────────────────────
            try:
                page_title = self.driver.title.lower()
            except Exception:
                page_title = ""

            # ── Read visible body text ─────────────────────────────────────
            try:
                body_text = self.driver.find_element(By.TAG_NAME, "body").text.lower()
            except Exception:
                body_text = ""

            # ── 1. Cloudflare Turnstile (interactive checkbox) ─────────────
            if "humans only" in body_text or "verify you are human" in body_text:
                return "captcha"
            if "humans only" in page_title:
                return "captcha"

            # ── 2. Cloudflare automatic browser check ──────────────────────
            if ("just a moment" in page_title
                    or "checking your browser" in body_text
                    or "just a moment" in body_text):
                return "blocked"

            # ── 2.5. Bot-detection redirect ("Authenticating...") ──────────
            # Glassdoor serves a tiny 787-byte page with title "Authenticating..."
            # that auto-redirects to login?reason=bot-detection. This happens
            # when session cookies are missing or invalid.
            if "authenticating" in page_title:
                return "login_wall"
            if "redirecting to login" in body_text:
                return "login_wall"

            # ── 3. Give-to-Get gate (page_source — the ONLY reliable check) ─
            # page_source is the raw server-sent HTML. It is available
            # IMMEDIATELY after the page loads, before React hydrates.
            # On gated pages, the SSR HTML always contains gate markers
            # even though review-detail elements also exist (hidden behind
            # the gate overlay). This is why we MUST check page_source
            # before checking the DOM for review elements.
            try:
                html_source = self.driver.page_source.lower()
            except Exception:
                html_source = ""

            if html_source:
                gate_markers = (
                    "new-survey-start-wrap" in html_source
                    or "get full access by completing" in html_source
                    or "please select a review type" in html_source
                    or "startsurveylegacy" in html_source
                )
                if gate_markers:
                    return "gated"

            # Also check body text (catches cases where page_source fails)
            if ("get full access by completing" in body_text
                    or "please select a review type" in body_text):
                return "gated"

            # ── 4. Review content (DOM check) ──────────────────────────────
            # Only reached when page_source has NO gate markers — but React
            # may have rendered them AFTER our initial read. Double-check
            # with a fresh page_source before confirming "reviews".
            try:
                review_els = self.driver.find_elements(
                    By.CSS_SELECTOR, '[data-test="review-detail"]'
                )
                if len(review_els) > 0:
                    # Double-check: re-read page_source for gate markers
                    # that React may have rendered since our initial read
                    try:
                        fresh_source = self.driver.page_source.lower()
                        if ("new-survey-start-wrap" in fresh_source
                                or "get full access by completing" in fresh_source
                                or "please select a review type" in fresh_source
                                or "startsurveylegacy" in fresh_source):
                            return "gated"
                    except Exception:
                        pass
                    return "reviews"
            except Exception:
                pass

            # Fallback: check for PROS elements
            try:
                pros = self.driver.find_elements(
                    By.CSS_SELECTOR, '[data-test="review-text-PROS"]'
                )
                if pros:
                    return "reviews"
            except Exception:
                pass

            # ── 5. Login hard-sell overlay ─────────────────────────────────
            if "hardsell" in body_text or "sign in to see" in body_text:
                return "login_wall"

            return "unknown"

        except Exception:
            return "unknown"


    # --- Utility ---------------------------------------------------------

    def _human_type(self, element, text: str):
        for char in text:
            element.send_keys(char)
            time.sleep(random.uniform(0.03, 0.12))

    def _random_delay(self, min_sec: float = None, max_sec: float = None):
        if min_sec is None:
            min_sec = config.MIN_DELAY
        if max_sec is None:
            max_sec = config.MAX_DELAY
        delay = random.uniform(min_sec, max_sec)
        time.sleep(delay)

    def _scroll_page(self):
        """Scroll down the page to trigger lazy-loaded content."""
        try:
            # Single smooth scroll via JS animation (avoids 4 × sleep(1))
            self.driver.execute_script("""
                var total = document.body.scrollHeight;
                window.scrollTo(0, total / 3);
            """)
            time.sleep(0.4)
            self.driver.execute_script(
                "window.scrollTo(0, document.body.scrollHeight * 2 / 3);"
            )
            time.sleep(0.3)
            self.driver.execute_script(
                "window.scrollTo(0, document.body.scrollHeight);"
            )
            time.sleep(0.3)
            self.driver.execute_script("window.scrollTo(0, 0);")
        except Exception:
            pass

    def _dismiss_gate(self):
        """
        Dismiss Glassdoor's "Give to Get" gate (new-survey-start-wrap).

        This gate completely replaces the reviews section with a survey form
        asking the user to submit their own review to unlock access. Unlike
        login overlays it does NOT sit on top of review DOM — the reviews are
        simply absent from the page. We must:
          1. Remove the gate container from the DOM
          2. Restore body scrolling (Glassdoor locks it)
          3. Navigate away and back so a fresh request is made without the
             gate flag — the gate is triggered by a JS config value
             (hardSellPageTrigger) that counts page views per session.

        The most reliable fix: reload the page via JS navigation (sets proper
        Referer) after a short pause, which sometimes gets a clean response.
        """
        logger.info("Detected 'Give to Get' gate — attempting removal...")
        try:
            self.driver.execute_script("""
                // Remove the gate container and any overlay elements
                var gateSelectors = [
                    '[data-test="new-survey-start-wrap"]',
                    '[class*="StartSurveyLegacy"]',
                    '[class*="startFormContainer"]',
                    '[class*="hardsell"]',
                    '[class*="HardSell"]',
                    '[class*="ContentWall"]',
                    '#ContentWallHardsell',
                    '[class*="modal-backdrop"]'
                ];
                gateSelectors.forEach(function(sel) {
                    document.querySelectorAll(sel).forEach(function(el) {
                        el.remove();
                    });
                });
                // Restore scrolling
                document.body.style.overflow = 'auto';
                document.body.style.position = '';
                document.documentElement.style.overflow = 'auto';
            """)
        except Exception as e:
            logger.debug(f"Gate JS removal: {e}")

    def _dismiss_popups(self):
        """Remove Glassdoor's hardsell overlays and login modals via JS + clicks."""
        # JavaScript removal of known overlay elements (most effective)
        try:
            self.driver.execute_script("""
                // Remove hardsell / login overlays
                var selectors = [
                    '#unified-user-auth',
                    '.hardsellOverlay',
                    '#ContentWallHardsell',
                    '[class*="HardsellOverlay"]',
                    '[class*="hardsell"]',
                    '[id*="LoginModal"]',
                    '[data-test="blurred-review-overlay"]',
                    '[class*="BlurredOverlay"]'
                ];
                selectors.forEach(function(sel) {
                    var els = document.querySelectorAll(sel);
                    els.forEach(function(el) { el.remove(); });
                });

                // Restore scrolling (Glassdoor locks body when overlay is up)
                document.body.style.overflow = 'auto';
                document.body.style.position = 'unset';
                document.documentElement.style.overflow = 'auto';
            """)
        except Exception as e:
            logger.debug(f"JS overlay removal: {e}")

        # Click-based dismissal as fallback
        popup_selectors = [
            '[data-test="close-button"]',
            'button[aria-label="Close"]',
            '[class*="CloseButton"]',
            '[class*="modal"] [class*="close"]',
            '[class*="actionBarContainer"] button',
        ]
        for selector in popup_selectors:
            try:
                buttons = self.driver.find_elements(By.CSS_SELECTOR, selector)
                for btn in buttons:
                    if btn.is_displayed():
                        self.driver.execute_script("arguments[0].click();", btn)
                        self._random_delay(0.3, 0.7)
                        logger.debug(f"Dismissed popup: {selector}")
            except (NoSuchElementException, StaleElementReferenceException):
                continue

    def _expand_reviews(self):
        """Click 'Show More' / 'Continue Reading' buttons."""
        expand_selectors = [
            '[class*="showMore"]',
            '[data-test="show-more"]',
            '[class*="ContinueReading"]',
            'button[class*="expand"]',
            'a[class*="readMore"]',
            'span[class*="showMore"]',
            'button[class*="ExpandableText"]',
        ]
        for selector in expand_selectors:
            try:
                buttons = self.driver.find_elements(By.CSS_SELECTOR, selector)
                for btn in buttons:
                    if btn.is_displayed():
                        try:
                            self.driver.execute_script("arguments[0].click();", btn)
                            time.sleep(0.3)
                        except Exception:
                            pass
            except (NoSuchElementException, StaleElementReferenceException):
                continue

        # NOTE: Sub-rating carets are handled by _extract_all_subratings()
        # which extracts sub-ratings from __next_f embedded JSON in the page source.

    # --- Sub-Rating Extraction (from embedded Next.js data) ----------------

    # Mapping from __next_f JSON keys to our CSV column names
    _SUBRATING_JSON_MAP = {
        "ratingCareerOpportunities": "career_opportunities",
        "ratingCompensationAndBenefits": "compensation_benefits",
        "ratingCultureAndValues": "culture_values",
        "ratingDiversityAndInclusion": "diversity_inclusion",
        "ratingSeniorLeadership": "senior_management",
        "ratingWorkLifeBalance": "work_life_balance",
    }

    def _extract_all_subratings(self, html: str) -> dict:
        """
        Extract per-review sub-ratings from the __next_f embedded JSON data
        in the page source.

        Glassdoor (Next.js) streams review data via self.__next_f.push() calls
        embedded in <script> tags. Each review includes fields like:
            "ratingCareerOpportunities": 5,
            "ratingCompensationAndBenefits": 3,
            ...
            "reviewId": 104724687

        Returns a dict mapping reviewId (str) -> sub-ratings dict.
        """
        subratings_by_id = {}

        try:
            # Extract all __next_f data chunks from the HTML
            chunks = re.findall(
                r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', html, re.DOTALL
            )

            for raw_chunk in chunks:
                # Unescape the JSON string
                try:
                    chunk = raw_chunk.encode('utf-8').decode('unicode_escape')
                except (UnicodeDecodeError, ValueError):
                    chunk = raw_chunk

                # Only process chunks that contain review data
                if '"reviewId"' not in chunk:
                    continue

                # Find all review blocks with their sub-ratings
                # Each review has a reviewId and rating fields
                for rid_match in re.finditer(r'"reviewId"\s*:\s*(\d+)', chunk):
                    review_id = rid_match.group(1)

                    # Look backwards from the reviewId to find the rating fields
                    # The ratings appear before the reviewId in the JSON
                    search_start = max(0, rid_match.start() - 600)
                    review_window = chunk[search_start:rid_match.end()]

                    subratings = {}
                    for json_key, our_key in self._SUBRATING_JSON_MAP.items():
                        m = re.search(
                            rf'"{json_key}"\s*:\s*([\d.]+|null)',
                            review_window
                        )
                        if m and m.group(1) != "null":
                            val = float(m.group(1))
                            # 0 means "not rated" by user
                            subratings[our_key] = val if val > 0 else None
                        else:
                            subratings[our_key] = None

                    subratings_by_id[review_id] = subratings

            logger.info(
                f"Extracted sub-ratings for {len(subratings_by_id)} reviews "
                f"from embedded data"
            )

        except Exception as e:
            logger.warning(f"Failed to extract sub-ratings from page data: {e}")

        return subratings_by_id

    def _extract_subratings_from_fiber(self) -> dict:
        """
        Extract sub-ratings from React's fiber tree via JavaScript.

        WHY: When clicking Next (client-side navigation), the DOM updates
        but the __next_f <script> tags stay stale from page 1. However,
        React's internal fiber tree IS updated — it represents the LIVE
        component state. We walk up the fiber tree from each review element
        to find the review data object containing sub-ratings.

        Returns dict mapping reviewId (str) -> sub-ratings dict.
        """
        try:
            raw = self.driver.execute_script("""
                var reviews = document.querySelectorAll('[data-test="review-detail"]');
                var result = {};
                var RATING_KEYS = [
                    'ratingCareerOpportunities',
                    'ratingCompensationAndBenefits',
                    'ratingCultureAndValues',
                    'ratingDiversityAndInclusion',
                    'ratingSeniorLeadership',
                    'ratingWorkLifeBalance'
                ];

                for (var i = 0; i < reviews.length; i++) {
                    var el = reviews[i];

                    // Find the React fiber key on this DOM element
                    var fiberKey = Object.keys(el).find(function(k) {
                        return k.startsWith('__reactFiber$') ||
                               k.startsWith('__reactInternalInstance$');
                    });
                    if (!fiberKey) continue;

                    var fiber = el[fiberKey];
                    var current = fiber;
                    var found = false;

                    // Walk up the fiber tree (max 30 levels)
                    for (var depth = 0; depth < 30 && current; depth++) {
                        var props = current.memoizedProps;
                        if (!props || typeof props !== 'object') {
                            current = current.return;
                            continue;
                        }

                        // Check if props directly has reviewId
                        if (props.reviewId && props.ratingWorkLifeBalance !== undefined) {
                            var ratings = {};
                            for (var r = 0; r < RATING_KEYS.length; r++) {
                                var val = props[RATING_KEYS[r]];
                                ratings[RATING_KEYS[r]] = (val !== null && val !== undefined && val > 0) ? val : null;
                            }
                            result[String(props.reviewId)] = ratings;
                            found = true;
                            break;
                        }

                        // Check all prop values for a nested review object
                        var propKeys = Object.keys(props);
                        for (var j = 0; j < propKeys.length; j++) {
                            var val = props[propKeys[j]];
                            if (val && typeof val === 'object' && val.reviewId &&
                                val.ratingWorkLifeBalance !== undefined) {
                                var ratings = {};
                                for (var r = 0; r < RATING_KEYS.length; r++) {
                                    var rv = val[RATING_KEYS[r]];
                                    ratings[RATING_KEYS[r]] = (rv !== null && rv !== undefined && rv > 0) ? rv : null;
                                }
                                result[String(val.reviewId)] = ratings;
                                found = true;
                                break;
                            }
                            // Check one more level deep (val.review, val.data, etc.)
                            if (val && typeof val === 'object' && !Array.isArray(val)) {
                                var innerKeys = Object.keys(val);
                                for (var k = 0; k < innerKeys.length; k++) {
                                    var inner = val[innerKeys[k]];
                                    if (inner && typeof inner === 'object' && inner.reviewId &&
                                        inner.ratingWorkLifeBalance !== undefined) {
                                        var ratings = {};
                                        for (var r = 0; r < RATING_KEYS.length; r++) {
                                            var rv = inner[RATING_KEYS[r]];
                                            ratings[RATING_KEYS[r]] = (rv !== null && rv !== undefined && rv > 0) ? rv : null;
                                        }
                                        result[String(inner.reviewId)] = ratings;
                                        found = true;
                                        break;
                                    }
                                }
                            }
                            if (found) break;
                        }
                        if (found) break;
                        current = current.return;
                    }
                }
                return result;
            """)

            if raw and isinstance(raw, dict):
                # Map JSON keys to our CSV column names
                mapped = {}
                for rid, ratings in raw.items():
                    sub = {}
                    for json_key, our_key in self._SUBRATING_JSON_MAP.items():
                        val = ratings.get(json_key)
                        sub[our_key] = float(val) if val is not None else None
                    mapped[rid] = sub
                logger.info(f"Fiber extraction: {len(mapped)} reviews with sub-ratings")
                return mapped

        except Exception as e:
            logger.warning(f"Fiber sub-rating extraction failed: {e}")

        return {}



    # --- Client-Side Page Jump -------------------------------------------

    def _jump_to_page(self, target_page: int) -> bool:
        """
        Jump directly to any page using the Next.js router found in
        the React fiber tree.

        WHY PREVIOUS APPROACHES FAILED:
        - driver.get(url) → full HTTP request → Cloudflare blocks
        - Modified <a> href + click → React reads href from INTERNAL
          PROPS, not DOM. URL changed but data stayed on page 1.
        - Fast-forward clicking → too many clicks → rate limited

        WHY THIS WORKS:
        When you click Next, React calls router.push(href) internally.
        This makes an RSC (React Server Components) fetch — a lightweight
        API call, NOT a full page load. Cloudflare never sees it.

        This method finds the SAME router.push() function by walking
        the React fiber tree, and calls it with the target page URL.
        The result is identical to clicking Next — correct URL AND data.
        """
        target_path = (
            f"/Reviews/{self.company['slug']}-Reviews-"
            f"E{self.company['employer_id']}_P{target_page}.htm"
        )

        logger.info(f"Jumping to page {target_page} via React Router...")

        # Capture current review IDs to verify data actually changed
        before_ids = self.driver.execute_script("""
            var els = document.querySelectorAll('[id^="empReview_"]');
            return Array.from(els).map(function(e) { return e.id; });
        """) or []

        try:
            result = self.driver.execute_script("""
                var targetPath = arguments[0];

                // ─── Strategy 1: Find router in React fiber tree ───
                // Walk the fiber tree to find a component whose hooks
                // contain the Next.js router.
                
                // First try to start from the Next button (it definitely has the router context)
                var startEl = document.querySelector('[data-test="pagination-next"]') || 
                              document.querySelector('button[aria-label="Next"]') || 
                              document.querySelector('a[aria-label="Next"]') ||
                              document.body;

                var startFiber = null;
                var keys = Object.keys(startEl);
                for(var j=0; j<keys.length; j++) {
                    if(keys[j].startsWith('__reactFiber$')) {
                        startFiber = startEl[keys[j]];
                        break;
                    }
                }
                
                // Fallback: search all elements
                if (!startFiber) {
                    var allEls = document.querySelectorAll('*');
                    for(var i=0; i<allEls.length; i++) {
                        var k2 = Object.keys(allEls[i]);
                        for(var j=0; j<k2.length; j++) {
                            if(k2[j].startsWith('__reactFiber$')) {
                                startFiber = allEls[i][k2[j]];
                                break;
                            }
                        }
                        if(startFiber) break;
                    }
                }
                if(!startFiber) return 'no_fiber';
                
                function getRootFiber(fiber) {
                    while (fiber.return) {
                        fiber = fiber.return;
                    }
                    return fiber;
                }
                var rootFiber = getRootFiber(startFiber);

                function findRouterInFiber(fiber, depth) {
                    if (!fiber || depth > 1500) return null;

                    // Check this fiber's hooks (memoizedState chain)
                    var hookState = fiber.memoizedState;
                    var hookN = 0;
                    while (hookState && hookN < 40) {
                        hookN++;
                        var val = hookState.memoizedState;

                        // The router object has push, replace, back, etc.
                        if (val && typeof val === 'object' &&
                            typeof val.push === 'function' &&
                            typeof val.replace === 'function' &&
                            typeof val.back === 'function') {
                            return val;
                        }

                        // Some hooks wrap state in queue.lastRenderedState
                        if (hookState.queue &&
                            hookState.queue.lastRenderedState) {
                            var qv = hookState.queue.lastRenderedState;
                            if (qv && typeof qv === 'object' &&
                                typeof qv.push === 'function' &&
                                typeof qv.replace === 'function' &&
                                typeof qv.back === 'function') {
                                return qv;
                            }
                        }
                        hookState = hookState.next;
                    }

                    // Recurse: child first, then sibling
                    return findRouterInFiber(fiber.child, depth + 1) ||
                           findRouterInFiber(fiber.sibling, depth + 1);
                }

                var router = findRouterInFiber(root[fiberKey], 0);
                if (router) {
                    router.push(targetPath);
                    return 'fiber_router';
                }

                // ─── Strategy 2: window.next.router (some versions) ───
                if (window.next && window.next.router &&
                    typeof window.next.router.push === 'function') {
                    window.next.router.push(targetPath);
                    return 'window_router';
                }

                // ─── Strategy 3: history + popstate (last resort) ───
                try {
                    window.history.pushState({}, '', targetPath);
                    window.dispatchEvent(new PopStateEvent('popstate'));
                    return 'popstate';
                } catch(e) {}

                return 'failed';
            """, target_path)

            if not result or result in ('failed', 'no_root', 'no_fiber'):
                logger.warning(f"Jump navigation failed: {result}")
                return False

            logger.info(f"Jump triggered via: {result}")

            # Wait for React to render the new page data
            time.sleep(random.uniform(5, 8))

            # ── Verify the jump: data must have actually changed ──
            page_state = self._detect_page_state()
            if page_state != "reviews":
                logger.warning(f"Page state after jump: {page_state}")
                time.sleep(4)
                page_state = self._detect_page_state()
                if page_state != "reviews":
                    return False

            # Check that review IDs changed (not same as page 1)
            after_ids = self.driver.execute_script("""
                var els = document.querySelectorAll('[id^="empReview_"]');
                return Array.from(els).map(function(e) { return e.id; });
            """) or []

            if before_ids and after_ids and set(before_ids) == set(after_ids):
                logger.error(
                    "Jump changed URL but NOT data — reviews are "
                    "still from the original page!"
                )
                return False

            logger.info(
                f"Successfully jumped to page {target_page} "
                f"(data verified: {len(after_ids)} reviews)"
            )
            return True

        except Exception as e:
            logger.error(f"Jump navigation failed: {e}")
            return False

    # --- Human Navigation ------------------------------------------------

    # Non-review page used to reset Give-to-Get gate counter between pages.
    # Navigation to this URL happens DURING the inter-page delay in run(),
    # so it costs zero extra time — the page loads concurrently with the wait.
    _GATE_RESET_URL = "https://www.glassdoor.com/"

    def _goto(self, url: str, skip_gate_reset: bool = False):
        """
        Navigate to a URL by simulating a link click (NOT driver.get).

        ROOT CAUSE FIX: driver.get(url) creates 'typed' navigation which
        sends NO Referer header. Glassdoor's server sees a direct URL hit
        with no referer from the previous reviews page and redirects to
        their internal bot-detection pods (prod-69b6454d9b-*), causing
        ERR_NAME_NOT_RESOLVED.

        By creating and clicking a link element, the browser sends:
        - Proper Referer header (current page URL)
        - All cookies (including cf_clearance)
        - Maintains localStorage/sessionStorage
        - Looks like natural user navigation
        """
        current_url = ""
        try:
            current_url = self.driver.current_url or ""
        except Exception:
            pass

        # If already on a Glassdoor page, navigate via link click
        if "glassdoor.com" in current_url:
            try:
                # Create a temporary <a> element and click it
                self.driver.execute_script("""
                    var a = document.createElement('a');
                    a.href = arguments[0];
                    a.style.display = 'none';
                    a.id = '_scraper_nav_link';
                    document.body.appendChild(a);
                    a.click();
                """, url)

                # Wait for URL to change (navigation started)
                old_url = current_url
                WebDriverWait(self.driver, config.PAGE_LOAD_TIMEOUT).until(
                    lambda d: d.current_url != old_url
                )
                # Wait for new page to fully load
                WebDriverWait(self.driver, config.PAGE_LOAD_TIMEOUT).until(
                    lambda d: d.execute_script(
                        "return document.readyState"
                    ) == "complete"
                )
                return
            except Exception as e:
                logger.warning(f"Link-click nav failed ({e}), falling back to driver.get()")

        # Fallback / first navigation: use driver.get()
        self.driver.get(url)

    def _navigate_to_review_page(self, page_number: int) -> bool:
        """
        Navigate directly to a specific review page using Chrome DevTools
        Protocol's Page.navigate with the Referer set to page N-1.

        WHY THIS IS NEEDED:
        Glassdoor's server-side bot detection checks the relationship between
        the Referer and the requested page. If a session was on page 1 and
        suddenly requests page 778, the server flags it as bot behavior and
        redirects to an internal pod (prod-*) that can't be resolved externally
        (DNS_PROBE_FINISHED_NXDOMAIN).

        _goto() sends the REAL current page as Referer (page 1), so the server
        sees a 777-page jump → bot detection.

        CDP's Page.navigate accepts an explicit 'referrer' parameter, letting
        us set it to page 777 so the request looks like a natural "Next" click.
        Combined with session rotation (which clears the server-side page view
        counter), the server has no reason to flag the request.

        Returns True if navigation succeeded and reviews are visible.
        """
        target_url = config.get_reviews_url(self.company_key, page_number)
        if page_number > 1:
            referer_url = config.get_reviews_url(self.company_key, page_number - 1)
        else:
            referer_url = f"https://www.glassdoor.com/Reviews/{self.company['slug']}-Reviews-E{self.company['employer_id']}.htm"

        logger.info(
            f"CDP navigate to page {page_number} "
            f"(Referer spoofed to page {page_number - 1})"
        )

        try:
            self.driver.execute_cdp_cmd('Page.navigate', {
                'url': target_url,
                'referrer': referer_url,
            })

            # Wait for page to load
            time.sleep(2)
            try:
                WebDriverWait(self.driver, config.PAGE_LOAD_TIMEOUT).until(
                    lambda d: d.execute_script(
                        "return document.readyState"
                    ) == "complete"
                )
            except TimeoutException:
                logger.warning(f"Page {page_number}: load timeout after CDP navigate")

            # Check for bot-detection redirect (DNS fails for prod-* hostnames)
            current_url = ""
            try:
                current_url = self.driver.current_url or ""
            except Exception:
                pass

            if current_url and "glassdoor.com" not in current_url.lower():
                logger.warning(f"Bot-detection redirect: {current_url}")
                # Escape the broken URL
                try:
                    self.driver.get("about:blank")
                    time.sleep(2)
                    self.driver.get("https://www.glassdoor.com/")
                    time.sleep(random.uniform(3, 5))
                except Exception:
                    pass
                return False

            self._random_delay(2, 3)
            page_state = self._detect_page_state()
            logger.info(f"Page {page_number} state after CDP nav: {page_state}")

            if page_state == "reviews":
                return True
            elif page_state in ("captcha", "blocked"):
                self._handle_cloudflare(page_number, 1)
                # After CF solved, retry the navigation
                self.driver.execute_cdp_cmd('Page.navigate', {
                    'url': target_url,
                    'referrer': referer_url,
                })
                self._random_delay(3, 5)
                return self._detect_page_state() == "reviews"
            elif page_state == "login_wall":
                self._dismiss_popups()
                self._random_delay(1, 2)
                return self._detect_page_state() == "reviews"
            elif page_state == "gated":
                return False
            else:
                # Unknown — check if reviews exist anyway
                try:
                    els = self.driver.find_elements(
                        By.CSS_SELECTOR, '[data-test="review-detail"]'
                    )
                    return len(els) > 0
                except Exception:
                    return False

        except Exception as e:
            logger.error(f"CDP navigation to page {page_number} failed: {e}")
            return False


    # --- Core Scraping ---------------------------------------------------

    def scrape_page(self, page_number: int) -> list[dict]:
        """
        Scrape a single page of reviews.
        Returns list of parsed review dicts.

        CF handling uses a SEPARATE inner while-loop so that solving a
        Cloudflare challenge never consumes one of the MAX_RETRIES error
        attempts.  Previously, 3 CF solves exhausted all retries and the
        page was silently abandoned even though the user had solved each one.
        """
        url = config.get_reviews_url(self.company_key, page_number)
        logger.info(f"Scraping page {page_number}: {url}")

        CF_MAX_HITS = 8  # give up on a page after this many unsolvable CF hits

        for attempt in range(1, config.MAX_RETRIES + 1):
            try:
                # ── Navigate to review page (synchronous) ──────────────────
                self._goto(url)

                # driver.get() waits for initial HTML load, but Glassdoor's
                # gate overlay is rendered by React AFTER the initial load.
                # We need 2-3s for React hydration before checking page_source.
                self._random_delay(2, 3)

                # ── Inner CF loop — never consumes attempt counter ─────────
                cf_hit_count = 0
                page_state = self._detect_page_state()
                logger.info(f"Page {page_number} state: {page_state}")

                while page_state in ("captcha", "blocked", "gated"):
                    cf_hit_count += 1
                    logger.warning(
                        f"Page {page_number}: "
                        + {
                            "captcha": "CF Turnstile challenge",
                            "blocked": "CF auto-check",
                            "gated": "Give-to-Get gate (session limit)",
                        }.get(page_state, page_state)
                        + f" hit #{cf_hit_count}"
                    )

                    if cf_hit_count > CF_MAX_HITS:
                        logger.error(
                            f"Page {page_number}: challenge hit {cf_hit_count}x — skipping."
                        )
                        return []

                    if page_state == "gated":
                        # Gate is server-side — session is locked.
                        # Rotate to a fresh anonymous session (clear cookies).
                        self._gate_confirmed = True
                        logger.info(
                            f"Page {page_number}: gate active — rotating session."
                        )
                        self._rotate_session()
                    else:
                        # CF challenge — pause and let the user solve it
                        self._handle_cloudflare(page_number, cf_hit_count)

                    # Re-navigate to target page
                    # skip_gate_reset=True because either:
                    # - CF handler already visited homepage (counts as reset)
                    # - gated handler already visited homepage above
                    self._goto(url, skip_gate_reset=True)
                    self._random_delay(2, 4)
                    page_state = self._detect_page_state()
                    logger.info(
                        f"Page {page_number} state after handling: {page_state}"
                    )

                # ── Login wall ─────────────────────────────────────────────
                if page_state == "login_wall":
                    logger.warning(f"Page {page_number}: hit login wall")
                    self._dismiss_popups()
                    self._random_delay(0.5, 1)
                    page_state = self._detect_page_state()
                    if page_state == "login_wall":
                        if not self.headless:
                            print(f"\n[WARN]  Login wall on page {page_number}!")
                            print("    Please log in in the browser.")
                            input("    Press ENTER when done... ")
                        else:
                            self._save_debug_html(page_number, attempt)
                            return []

                # ── Scroll, dismiss overlays, expand reviews ───────────────
                self._scroll_page()
                self._dismiss_popups()
                self._expand_reviews()
                self._random_delay(0.5, 1)

                self._save_debug_html(page_number, attempt)

                html = self.driver.page_source

                # Final safety net: catch CF in raw HTML
                html_lower = html.lower()
                if ("verify you are human" in html_lower
                        or "humans only" in html_lower
                        or "just a moment" in html_lower):
                    logger.warning(
                        f"Page {page_number}: CF still in HTML source — "
                        "will retry on next attempt"
                    )
                    if attempt < config.MAX_RETRIES:
                        self._random_delay(10, 20)
                    continue

                reviews = parse_reviews_page(html)

                if reviews:
                    subratings_by_id = self._extract_all_subratings(html)
                    sr_count = 0
                    for review in reviews:
                        rid = review.get("review_id", "")
                        if rid and rid in subratings_by_id:
                            review.update(subratings_by_id[rid])
                            sr_count += 1
                    logger.info(
                        f"Page {page_number}: {len(reviews)} reviews "
                        f"({sr_count} with sub-ratings)"
                    )
                    return reviews

                else:
                    # 0 reviews parsed — check HTML for gate before retrying
                    html_lower = html.lower()
                    is_gate_in_html = (
                        "get full access by completing" in html_lower
                        or "new-survey-start-wrap" in html_lower
                        or "please select a review type" in html_lower
                    )

                    if is_gate_in_html:
                        # Mark for persistent gate detection in run()
                        self._gate_confirmed = True
                        logger.warning(
                            f"Page {page_number}: Give-to-Get gate confirmed in HTML "
                            f"(attempt {attempt}) — rotating session."
                        )
                        # Rotate to fresh anonymous session
                        self._rotate_session()
                        # Force a re-check: let the outer for-loop retry
                        if attempt < config.MAX_RETRIES:
                            continue
                    else:
                        logger.warning(
                            f"Page {page_number}: no reviews parsed (attempt {attempt}), "
                            f"state={page_state}"
                        )
                        if attempt == 1:
                            debug_dir = config.DATA_DIR / "debug"
                            debug_dir.mkdir(parents=True, exist_ok=True)
                            fp = debug_dir / f"page_{page_number}_FAIL.html"
                            fp.write_text(html, encoding="utf-8")
                            try:
                                self.driver.save_screenshot(
                                    str(debug_dir / f"page_{page_number}_FAIL.png")
                                )
                            except Exception:
                                pass
                            logger.info(f"Saved failed page HTML: {fp}")
                        if attempt < config.MAX_RETRIES:
                            self._random_delay(8, 15)

            except (InvalidSessionIdException, WebDriverException) as e:
                is_dead = (
                    isinstance(e, InvalidSessionIdException)
                    or "invalid session id" in str(e).lower()
                    or "no such window" in str(e).lower()
                )
                if is_dead:
                    logger.error(f"Browser session died on page {page_number}")
                    if attempt < config.MAX_RETRIES:
                        try:
                            self.restart_browser(ask_login=True)
                        except Exception as re:
                            logger.error(f"Browser restart failed: {re}")
                            return []
                        continue
                    return []
                else:
                    err_str = str(e).lower()
                    if "err_name_not_resolved" in err_str:
                        # Glassdoor redirected to internal bot-detection pod
                        # (prod-69b6454d9b-*). Can't resolve externally.
                        logger.warning(
                            f"Page {page_number}: Bot-detection redirect "
                            f"(ERR_NAME_NOT_RESOLVED). Resetting session..."
                        )
                        print(f"\n[BOT] Glassdoor bot-detection on page {page_number}. "
                              f"Waiting 45s to reset...")
                        try:
                            # Escape the broken URL
                            self.driver.get("about:blank")
                            time.sleep(random.uniform(40, 55))
                            # Reset via homepage
                            self.driver.get("https://www.glassdoor.com/")
                            time.sleep(random.uniform(5, 8))
                            # Rotate session to get fresh tracking
                            self._rotate_session()
                        except Exception:
                            pass
                        if attempt < config.MAX_RETRIES:
                            continue
                        return []
                    else:
                        logger.error(f"Browser error on page {page_number}: {e}")
                        if attempt < config.MAX_RETRIES:
                            self._random_delay(5, 10)
                        else:
                            return []

            except TimeoutException:
                logger.warning(f"Page {page_number} timed out (attempt {attempt})")
                self._save_debug_html(page_number, attempt)
                if attempt < config.MAX_RETRIES:
                    self._random_delay(10, 20)

        return []

    def get_total_pages(self) -> int:
        """Navigate to first page and determine total review pages."""
        html = self.driver.page_source
        total_count = get_total_review_count(html)

        if total_count:
            total_pages = (total_count + config.REVIEWS_PER_PAGE - 1) // config.REVIEWS_PER_PAGE
            logger.info(f"Total reviews: {total_count} → {total_pages} pages")
            return total_pages
        else:
            logger.warning(
                "Could not determine total review count from page. "
                "Will scrape until no more reviews are found."
            )
            return 0

    def run(
        self,
        max_pages: int = 0,
        start_page: int = 0,
        output_format: str = "both",
    ) -> Path:
        """
        Main scraping loop.

        Parameters
        ----------
        max_pages : int
            Maximum number of pages to scrape (0 = all).
        start_page : int
            Override start page (0 = auto-detect from checkpoint).
        output_format : str
            'csv', 'excel', or 'both'.

        Returns
        -------
        Path to the raw output CSV file.
        """
        from tqdm import tqdm
        import csv as csv_module

        # Determine start page from checkpoint

        if start_page <= 0:
            checkpoint = self.load_checkpoint()
            if checkpoint:
                resume_page = checkpoint["last_page"] + 1
                self.total_reviews_scraped = checkpoint["total_scraped"]
                self.failed_pages = checkpoint.get("failed_pages", [])
                start_page = resume_page
                print(f"\n[RESUME] Resuming from page {start_page} "
                      f"({self.total_reviews_scraped} reviews already scraped)")
            else:
                start_page = 1

        # Start browser
        self.start_browser()

        page = start_page  # Initialize for finally block

        try:
            # -- Step 1: Login / Session restore -----------------------
            cookie_loaded = self._load_cookies()
            if not cookie_loaded:
                self._do_manual_login()

            # -- Step 2: Navigate to page 1 ----------------------------
            print(f"\n[SEARCH] Navigating to {self.company['name']} reviews...")
            page1_url = config.get_reviews_url(self.company_key, 1)
            self.driver.get(page1_url)
            self._random_delay(3, 5)

            # Check page state
            page_state = self._detect_page_state()
            print(f"   Page state: {page_state}")

            if page_state == "gated":
                print("\n[GATE] Gate detected — submit a review on Glassdoor to unlock.")
                return None

            if page_state in ("login_wall", "captcha"):
                self._dismiss_popups()
                self._random_delay(1, 2)
                page_state = self._detect_page_state()
                if page_state in ("login_wall", "captcha") and not self.headless:
                    print(f"\n[WARN]  {page_state.replace('_', ' ').title()} detected!")
                    print("   Please resolve it in the browser window.")
                    input("   Press ENTER when done... ")

            total_pages = self.get_total_pages()

            # -- Step 3: Jump to resume page if needed ------------------
            if start_page > 1:
                print(f"[JUMP] Jumping to page {start_page} via React Router...")
                jumped = self._jump_to_page(start_page)
                if jumped:
                    print(f"[OK] Successfully jumped to page {start_page}")
                else:
                    # Fallback: ask user to navigate manually
                    target_url = config.get_reviews_url(self.company_key, start_page)
                    print(f"\n[MANUAL] Auto-jump failed. Please navigate manually:")
                    print(f"   1. In the browser, press Ctrl+L")
                    print(f"   2. Paste: {target_url}")
                    print(f"   3. Press Enter and wait for reviews to load")
                    input("   >>> Press ENTER here when done... ")

            if max_pages > 0:
                end_page = (
                    min(start_page + max_pages - 1, total_pages)
                    if total_pages > 0
                    else start_page + max_pages - 1
                )
            else:
                end_page = total_pages if total_pages > 0 else 9999

            print(f"[PAGES] Scraping pages {start_page} to {end_page}")
            est_min = (end_page - start_page + 1) * 12 / 60
            est_max = (end_page - start_page + 1) * 18 / 60
            print(f"[TIME]  Estimated time: {est_min:.0f}-{est_max:.0f} minutes\n")

            # -- Step 3: Scraping Loop (click-based navigation) --------
            pbar = tqdm(
                range(start_page, end_page + 1),
                desc=f"Scraping {self.company['name']}",
                unit="page",
                ncols=100,
            )

            consecutive_empty = 0
            consecutive_gate_fails = 0
            raw_csv_path = None

            for page in pbar:
                pbar.set_postfix({
                    "reviews": self.total_reviews_scraped,
                    "fails": len(self.failed_pages),
                })

                # -- Navigation -------------------------------------------
                # Page 1 (or the resume page) is already loaded.
                # For subsequent pages, click the Next button.
                if page > start_page:
                    try:
                        next_btns = self.driver.find_elements(
                            By.CSS_SELECTOR,
                            '[data-test="pagination-next"], '
                            'button[aria-label="Next"], '
                            'a[aria-label="Next"]'
                        )
                        clicked = False
                        for btn in next_btns:
                            if btn.is_displayed() and btn.is_enabled():
                                self.driver.execute_script(
                                    "arguments[0].scrollIntoView({block:'center'});",
                                    btn
                                )
                                time.sleep(random.uniform(0.5, 1.0))
                                btn.click()
                                clicked = True
                                logger.info(f"Clicked Next → page {page}")
                                break
                        if not clicked:
                            logger.warning(f"No Next button found for page {page}")
                            self.failed_pages.append(page)
                            consecutive_empty += 1
                            if consecutive_empty >= 5:
                                break
                            continue
                    except Exception as e:
                        logger.warning(f"Next button click failed: {e}")
                        self.failed_pages.append(page)
                        continue

                    # Wait for the new page to load
                    self._random_delay(3, 5)

                # -- Scrape current page --
                page_state = self._detect_page_state()
                logger.info(f"Page {page} state: {page_state}")

                if page_state == "gated":
                    self._gate_confirmed = True
                    logger.warning(f"Page {page}: gated")

                reviews = []
                if page_state == "reviews":
                    self._scroll_page()
                    self._dismiss_popups()
                    self._expand_reviews()
                    self._random_delay(0.5, 1)

                    html = self.driver.page_source
                    reviews = parse_reviews_page(html)

                    if reviews:
                        # Extract sub-ratings: use fiber tree (always current)
                        # with __next_f as fallback for page 1
                        subratings_by_id = self._extract_subratings_from_fiber()
                        if not subratings_by_id:
                            # Fallback: try __next_f (works on page 1)
                            subratings_by_id = self._extract_all_subratings(html)
                        sr_count = 0
                        for review in reviews:
                            rid = review.get("review_id", "")
                            if rid and rid in subratings_by_id:
                                review.update(subratings_by_id[rid])
                                sr_count += 1
                        logger.info(
                            f"Page {page}: {len(reviews)} reviews "
                            f"({sr_count} with sub-ratings)"
                        )

                if reviews:
                    consecutive_empty = 0
                    consecutive_gate_fails = 0
                    raw_csv_path = save_reviews_incremental(
                        reviews,
                        company_name=self.company["name"],
                        page_number=page,
                    )
                    self.total_reviews_scraped += len(reviews)
                else:
                    consecutive_empty += 1
                    self.failed_pages.append(page)
                    logger.warning(f"Page {page}: no reviews extracted")

                    if self._gate_confirmed:
                        consecutive_gate_fails += 1
                        self._gate_confirmed = False
                        if consecutive_gate_fails >= 3:
                            print("\n[GATE] Account is gated. Submit a review on Glassdoor to unlock.")
                            break
                    else:
                        consecutive_gate_fails = 0

                    if consecutive_empty >= 5:
                        logger.warning("5 consecutive empty pages — stopping.")
                        print("\n[WARN] 5 consecutive empty pages. Stopping.")
                        break

                # Checkpoint every N pages
                if page % config.CHECKPOINT_INTERVAL == 0:
                    self.save_checkpoint(page, self.total_reviews_scraped)

                # --- Delay between pages ──────────────────────────────────
                pages_done = page - start_page + 1

                if pages_done > 0 and pages_done % 20 == 0:
                    long_break = random.uniform(30, 50)
                    logger.info(f"Taking a {long_break:.0f}s breather...")
                    print(f"\n[PAUSE] {long_break:.0f}s break...")
                    time.sleep(long_break)
                else:
                    delay = random.uniform(8, 15)
                    time.sleep(delay)

            # Final checkpoint
            self.save_checkpoint(page, self.total_reviews_scraped)
            pbar.close()

            # Summary
            print(f"\n{'='*60}")
            print(f"  SCRAPING COMPLETE -- {self.company['name']}")
            print(f"{'='*60}")
            print(f"  Total reviews scraped : {self.total_reviews_scraped}")
            print(f"  Pages scraped         : {start_page}-{page}")
            print(f"  Failed pages          : {len(self.failed_pages)}")
            if raw_csv_path:
                print(f"  Raw data saved to     : {raw_csv_path}")
            print(f"{'='*60}\n")

            return raw_csv_path

        except KeyboardInterrupt:
            print("\n\n[STOP]  Interrupted. Progress saved.")
            print(f"   Resume: python -m glassdoor_scraper.main --company {self.company_key}")
            self.save_checkpoint(page, self.total_reviews_scraped)
            return None

        except Exception as e:
            logger.error(f"Scraping failed: {e}", exc_info=True)
            try:
                self.save_checkpoint(page, self.total_reviews_scraped)
            except UnboundLocalError:
                pass
            raise

        finally:
            self.close_browser()
