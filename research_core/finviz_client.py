"""
Finviz Elite Client — Authentication, session management, and screener export.

Handles login via email/password, session cookie persistence across requests,
CSV export fetching, and HTML table parsing as fallback. Designed for
headless execution in GitHub Actions.

Environment Variables:
    FINVIZ_EMAIL    — Finviz Elite account email
    FINVIZ_PASSWORD — Finviz Elite account password
"""

import os
import re
import csv
import time
import logging
import io
from typing import Optional
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────
BASE_URL = "https://elite.finviz.com"
LOGIN_URL = f"{BASE_URL}/login_submit.ashx"
EXPORT_URL = f"{BASE_URL}/export.ashx"
SCREENER_URL = f"{BASE_URL}/screener.ashx"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": f"{BASE_URL}/login.ashx",
}

MAX_RETRIES = 3
RETRY_DELAY = 5  # seconds
REQUEST_DELAY = 2  # seconds between requests (rate limiting)


class FinvizAuthError(Exception):
    """Raised when Finviz login fails."""
    pass


class FinvizFetchError(Exception):
    """Raised when screener fetch fails after retries."""
    pass


class FinvizClient:
    """
    Authenticated Finviz Elite client.

    Usage:
        client = FinvizClient()
        client.login()
        results = client.fetch_screen("https://elite.finviz.com/screener.ashx?v=111&f=...")
    """

    def __init__(self, email: Optional[str] = None, password: Optional[str] = None):
        self.email = email or os.environ.get("FINVIZ_EMAIL", "")
        self.password = password or os.environ.get("FINVIZ_PASSWORD", "")
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self._authenticated = False

        if not self.email or not self.password:
            raise FinvizAuthError(
                "FINVIZ_EMAIL and FINVIZ_PASSWORD must be set as environment variables "
                "or passed to the constructor."
            )

    def login(self) -> bool:
        """
        Authenticate with Finviz Elite.
        Returns True on success, raises FinvizAuthError on failure.
        """
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                logger.info(f"Finviz login attempt {attempt}/{MAX_RETRIES}")

                # Step 1: GET the login page to capture any CSRF tokens / cookies
                login_page = self.session.get(
                    f"{BASE_URL}/login.ashx", timeout=30
                )
                login_page.raise_for_status()

                # Step 2: POST credentials
                payload = {
                    "email": self.email,
                    "password": self.password,
                    "remember": "true",
                }
                resp = self.session.post(
                    LOGIN_URL,
                    data=payload,
                    timeout=30,
                    allow_redirects=True,
                )
                resp.raise_for_status()

                # Step 3: Validate login by checking for auth indicators
                if self._verify_auth():
                    self._authenticated = True
                    logger.info("✅ Finviz Elite login successful")
                    return True
                else:
                    logger.warning(f"Login attempt {attempt} — auth verification failed")

            except requests.RequestException as e:
                logger.warning(f"Login attempt {attempt} failed: {e}")

            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)

        raise FinvizAuthError(
            "Failed to authenticate with Finviz Elite after all retries. "
            "Check FINVIZ_EMAIL and FINVIZ_PASSWORD."
        )

    def _verify_auth(self) -> bool:
        """Verify we're logged in by checking the screener page for Elite features."""
        try:
            resp = self.session.get(SCREENER_URL, timeout=30)
            text = resp.text.lower()
            # If we see the login form or "sign in", auth failed
            # If we see "elite" branding or export button, auth succeeded
            if "login.ashx" in text and "email" in text and "password" in text:
                return False
            # Check for elite-specific elements
            if "export" in text or "elite" in text:
                return True
            # Fallback: if no login form, assume success
            return "login_submit" not in text
        except Exception:
            return False

    def _ensure_auth(self):
        """Re-login if session has expired."""
        if not self._authenticated:
            self.login()
        elif not self._verify_auth():
            logger.info("Session expired — re-authenticating")
            self._authenticated = False
            self.login()

    def fetch_screen(self, screener_url: str) -> list[dict]:
        """
        Fetch screener results from a Finviz Elite screener URL.
        Tries CSV export first, falls back to HTML table parsing.

        Args:
            screener_url: Full Finviz Elite screener URL

        Returns:
            List of dicts, each representing a row (ticker + columns)
        """
        self._ensure_auth()

        # Try CSV export first (cleanest data)
        try:
            results = self._fetch_csv_export(screener_url)
            if results:
                return results
        except Exception as e:
            logger.warning(f"CSV export failed, falling back to HTML: {e}")

        # Fallback: parse HTML table with pagination
        return self._fetch_html_table(screener_url)

    def _screener_url_to_export_url(self, screener_url: str) -> str:
        """Convert screener.ashx URL to export.ashx URL."""
        parsed = urlparse(screener_url)
        params = parse_qs(parsed.query, keep_blank_values=True)

        # Remove pagination and view params that don't apply to export
        params.pop("r", None)

        # Flatten single-value lists
        flat_params = {k: v[0] if len(v) == 1 else v for k, v in params.items()}

        export_url = urlunparse((
            parsed.scheme,
            parsed.netloc,
            "/export.ashx",
            parsed.params,
            urlencode(flat_params, doseq=True),
            parsed.fragment,
        ))
        return export_url

    def _fetch_csv_export(self, screener_url: str) -> list[dict]:
        """Fetch results via CSV export endpoint."""
        export_url = self._screener_url_to_export_url(screener_url)
        logger.info(f"Fetching CSV export: {export_url[:80]}...")

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = self.session.get(export_url, timeout=60)
                resp.raise_for_status()

                content_type = resp.headers.get("Content-Type", "")
                if "text/csv" in content_type or "application/octet" in content_type or "," in resp.text[:200]:
                    reader = csv.DictReader(io.StringIO(resp.text))
                    results = [row for row in reader]
                    logger.info(f"CSV export returned {len(results)} results")
                    return results
                else:
                    logger.warning(f"Unexpected content type: {content_type}")
                    return []

            except requests.RequestException as e:
                logger.warning(f"CSV fetch attempt {attempt} failed: {e}")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY)

        return []

    def _fetch_html_table(self, screener_url: str) -> list[dict]:
        """Parse screener results from HTML table, handling pagination."""
        all_results = []
        page_start = 1

        # Strip existing pagination from URL
        parsed = urlparse(screener_url)
        params = parse_qs(parsed.query, keep_blank_values=True)
        params.pop("r", None)

        while True:
            params["r"] = [str(page_start)]
            flat_params = {k: v[0] if isinstance(v, list) and len(v) == 1 else v for k, v in params.items()}
            page_url = urlunparse((
                parsed.scheme, parsed.netloc, parsed.path,
                parsed.params, urlencode(flat_params, doseq=True), parsed.fragment,
            ))

            logger.info(f"Fetching HTML page (r={page_start}): {page_url[:80]}...")

            try:
                resp = self.session.get(page_url, timeout=30)
                resp.raise_for_status()
            except requests.RequestException as e:
                logger.error(f"HTML fetch failed: {e}")
                break

            soup = BeautifulSoup(resp.text, "html.parser")

            # Find the screener results table
            table = soup.find("table", {"id": "screener-views-table"})
            if not table:
                # Try alternative table selector
                tables = soup.find_all("table", class_="table-light")
                table = tables[-1] if tables else None

            if not table:
                logger.warning("Could not find results table on page")
                break

            rows = table.find_all("tr")
            if len(rows) < 2:
                break

            # Extract headers from first row
            headers = [th.get_text(strip=True) for th in rows[0].find_all(["th", "td"])]

            page_results = []
            for row in rows[1:]:
                cells = [td.get_text(strip=True) for td in row.find_all("td")]
                if cells and len(cells) == len(headers):
                    page_results.append(dict(zip(headers, cells)))

            if not page_results:
                break

            all_results.extend(page_results)
            logger.info(f"Page yielded {len(page_results)} results (total: {len(all_results)})")

            # Check if there are more pages (Finviz shows 20 per page)
            if len(page_results) < 20:
                break

            page_start += 20
            time.sleep(REQUEST_DELAY)

            # Safety cap at 500 results
            if len(all_results) >= 500:
                logger.info("Hit 500-result cap, stopping pagination")
                break

        logger.info(f"HTML parsing returned {len(all_results)} total results")
        return all_results

    def close(self):
        """Close the session."""
        self.session.close()

    def __enter__(self):
        self.login()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
