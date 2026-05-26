"""
Scrape public ACGME program leadership contacts into a JSON checkpoint.

Written by: zapulam
"""

from __future__ import annotations

import argparse
import json
import random
import re
import time

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag

from logger import (
    make_state_progress,
    print_banner,
    print_error,
    print_info,
    print_run_config,
    print_success,
    print_warning,
)


# --- Constants and data models ---
BASE_URL = "https://apps.acgme.org"
SEARCH_URL = f"{BASE_URL}/ads/Public/Programs/Search"
CONTACT_COLUMNS = [
    "Program Code",
    "Program Name",
    "Specialty",
    "State",
    "City",
    "Role",
    "Name",
    "Email",
    "Phone",
    "Source URL",
]
CONTACT_SECTION_TITLES = {
    "director information",
    "coordinator information",
}
STOP_SECTION_HINTS = (
    "accreditation",
    "positions",
    "participating site",
    "osteopathic",
    "legend",
)
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
DETAIL_READY_MARKERS = (
    "Director Information",
    "Coordinator Information",
    "Accreditation Status",
    "Program Information",
)
DETAIL_MAX_ATTEMPTS = 2
DETAIL_FAILURE_COOLDOWN_THRESHOLD = 3
DETAIL_FAILURE_COOLDOWNS = (300, 600, 900)


@dataclass(frozen=True)
class StateOption:
    """A state or territory option from the public ACGME search form."""

    code: str
    name: str


@dataclass(frozen=True)
class ProgramResult:
    """A program row discovered on an ACGME search results page."""

    program_code: str
    program_name: str
    specialty: str
    state: str
    city: str
    detail_url: str


@dataclass(frozen=True)
class ContactRow:
    """A checkpoint-ready leadership contact row for one ACGME program."""

    program_code: str
    program_name: str
    specialty: str
    state: str
    city: str
    role: str
    name: str
    email: str
    phone: str
    source_url: str


class DetailNavigationError(RuntimeError):
    """A concise detail-page navigation error with full diagnostic context."""

    def __init__(
            self,
            message: str,
            *,
            full_error: str | None = None,
        ) -> None:
        """Initialize a detail navigation error with optional full diagnostics."""
        super().__init__(message)
        self.full_error = full_error or message


class DetailLinkMissingError(RuntimeError):
    """Raised when the prepared ACGME results page lacks a program detail link."""


class DetailPageNotReadyError(RuntimeError):
    """Raised when ACGME detail content is not ready before the timeout."""


# --- Helper functions ---
def clean_text(
        value: str | None,
    ) -> str:
    """Normalize whitespace and remove non-content spaces from text."""
    if value is None:
        return ""
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()


def clean_phone(
        value: str | None,
    ) -> str:
    """Normalize a phone value while preserving human-readable formatting."""
    phone = clean_text(value)
    phone = re.sub(r"^(Phone|Tel|Telephone)\s*:\s*", "", phone, flags=re.I)
    return phone.strip()


def clean_email(
        value: str | None,
    ) -> str:
    """Normalize an email value and strip mailto prefixes or query strings."""
    email = clean_text(value)
    email = re.sub(r"^mailto:", "", email, flags=re.I)
    email = email.split("?", 1)[0]
    return email.strip()


def default_role_for_section(
        section_title: str,
    ) -> str:
    """Convert an ACGME contact section title into a useful fallback role."""
    title = clean_text(section_title)
    return re.sub(r"\s+Information$", "", title, flags=re.I).strip() or title


def current_utc_iso() -> str:
    """Return the current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def ensure_parent_dir(
        path: Path,
    ) -> None:
    """Create the parent directory for a file path if one is needed."""
    path.parent.mkdir(parents=True, exist_ok=True)


def parsed_contact_key(
        row: ContactRow,
    ) -> str:
    """Build a parser-level key that keeps distinct roles on one detail page."""
    return "|".join(
        [
            row.program_code.strip().lower(),
            row.role.strip().lower(),
            row.name.strip().lower(),
        ]
    )


def parse_retry_after_seconds(
        retry_after: str | None,
    ) -> float | None:
    """Parse an HTTP Retry-After header into seconds when possible."""
    if not retry_after:
        return None
    retry_after = retry_after.strip()
    if retry_after.isdigit():
        return float(retry_after)
    try:
        retry_at = parsedate_to_datetime(retry_after)
    except (TypeError, ValueError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())


def polite_sleep(
        delay: float,
    ) -> None:
    """Sleep for the configured delay plus light jitter between requests."""
    if delay <= 0:
        return
    time.sleep(delay + random.uniform(0, min(delay * 0.25, 0.75)))


def build_session() -> requests.Session:
    """Create a requests session configured for the public ACGME site."""
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    return session


def concise_error_message(
        error: Exception | str,
) -> str:
    """Return the first useful line from an exception for terminal logging."""
    message = str(error)
    return clean_text(message.splitlines()[0] if message else "")


def full_error_message(
        error: Exception | str,
) -> str:
    """Return full diagnostic text stored for checkpoint error records."""
    return str(getattr(error, "full_error", error))


def program_error_message(
        program: ProgramResult,
        error: Exception | str,
) -> str:
    """Return a concise program-scoped error without duplicating the code."""
    message = concise_error_message(error)
    prefix = f"{program.program_code}:"
    if message.startswith(prefix):
        return message
    return f"{program.program_code}: {message}"


def cooldown_seconds_for_level(
        cooldown_level: int,
) -> int:
    """Return the throttle cooldown duration for a zero-based cooldown level."""
    index = min(cooldown_level, len(DETAIL_FAILURE_COOLDOWNS) - 1)
    return DETAIL_FAILURE_COOLDOWNS[index]


def sleep_for_throttle_cooldown(
        state_name: str,
        cooldown_seconds: int,
) -> None:
    """Pause scraping to let likely ACGME throttling clear."""
    minutes = max(1, round(cooldown_seconds / 60))
    print_warning(
        f"Possible throttling detected; waiting {minutes} minutes before continuing."
    )
    time.sleep(cooldown_seconds)
    print_warning(f"Cooldown complete; resuming {state_name}.")


# --- HTML parsing functions ---
def parse_state_options(
        html: str,
    ) -> list[StateOption]:
    """Extract state and territory options from the ACGME search form."""
    soup = BeautifulSoup(html, "html.parser")
    state_select = soup.find("select", id="stateFilter") or soup.find(
        "select",
        attrs={"name": "stateId"},
    )
    if not state_select:
        raise ValueError("Could not find the ACGME state selector.")

    options: list[StateOption] = []
    for option in state_select.find_all("option"):
        code = clean_text(option.get("value"))
        name = clean_text(option.get_text(" ", strip=True))
        if code and name and "search by" not in name.lower():
            options.append(StateOption(code=code, name=name))
    return options


def parse_request_verification_token(
        html: str,
    ) -> str:
    """Extract the anti-forgery token from an ACGME page."""
    soup = BeautifulSoup(html, "html.parser")
    token_input = soup.find("input", attrs={"name": "__RequestVerificationToken"})
    token = clean_text(token_input.get("value") if token_input else "")
    if not token:
        raise ValueError("Could not find __RequestVerificationToken on the page.")
    return token


def parse_program_results(
        html: str,
        state_name: str,
    ) -> list[ProgramResult]:
    """Parse program search results from an ACGME results page."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id=re.compile("programsListView", re.I)) or soup.find("table")
    if not table:
        return []

    header_cells = table.find("tr").find_all(["th", "td"]) if table.find("tr") else []
    headers = [clean_text(cell.get_text(" ", strip=True)).lower() for cell in header_cells]
    code_index = headers.index("code") if "code" in headers else 1
    specialty_index = headers.index("specialty") if "specialty" in headers else 2
    name_index = headers.index("name") if "name" in headers else 3
    city_index = headers.index("city") if "city" in headers else 4

    results: list[ProgramResult] = []
    for row in table.find_all("tr")[1:]:
        cells = row.find_all("td")
        if len(cells) <= max(code_index, specialty_index, name_index, city_index):
            continue

        program_code = clean_text(cells[code_index].get_text(" ", strip=True))
        if not program_code or "no programs found" in program_code.lower():
            continue

        detail_link = None
        for link in row.find_all("a", href=True):
            href = link["href"]
            if "/Programs/Detail" in href or "Programs/Detail" in href:
                detail_link = href
                break
        if not detail_link:
            continue

        results.append(
            ProgramResult(
                program_code=program_code,
                program_name=clean_text(cells[name_index].get_text(" ", strip=True)),
                specialty=clean_text(cells[specialty_index].get_text(" ", strip=True)),
                state=state_name,
                city=clean_text(cells[city_index].get_text(" ", strip=True)),
                detail_url=urljoin(BASE_URL, detail_link),
            )
        )
    return results


def parse_detail_header(
        html: str,
    ) -> dict[str, str]:
    """Extract fallback program metadata from an ACGME detail page header."""
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.find("h1")
    text = clean_text(heading.get_text(" ", strip=True) if heading else "")
    pattern = re.compile(
        r"^(?P<code>\d+)\s*-\s*(?P<name>.*?)\s+"
        r"(?P<specialty>.*?)\s*-\s*(?P<city>.*?),\s*(?P<state>[A-Z]{2})$"
    )
    match = pattern.match(text)
    if not match:
        return {}
    return {key: clean_text(value) for key, value in match.groupdict().items()}


def parse_definition_values(
        container: Tag,
    ) -> dict[str, str]:
    """Parse nearby definition-list labels and values from a contact block."""
    values: dict[str, str] = {}
    for label in container.find_all("dt"):
        key = clean_text(label.get_text(" ", strip=True)).rstrip(":").lower()
        value_node = label.find_next_sibling("dd")
        if not key or not value_node:
            continue
        if key == "email":
            mail_link = value_node.find("a", href=re.compile(r"^mailto:", re.I))
            values[key] = clean_email(
                mail_link.get("href") if mail_link else value_node.get_text(" ", strip=True)
            )
        elif key in {"phone", "tel", "telephone"}:
            values["phone"] = clean_phone(value_node.get_text(" ", strip=True))
        else:
            values[key] = clean_text(value_node.get_text(" ", strip=True))
    return values


def extract_contact_from_block(
        block: Tag,
        section_title: str,
        program: ProgramResult,
    ) -> ContactRow | None:
    """Extract one contact row from a director or coordinator block."""
    list_items = [
        clean_text(item.get_text(" ", strip=True))
        for item in block.find_all("li")
        if clean_text(item.get_text(" ", strip=True))
    ]
    name = list_items[0] if list_items else ""
    role = list_items[1] if len(list_items) > 1 else default_role_for_section(section_title)
    values = parse_definition_values(block)
    email = values.get("email", "")
    phone = values.get("phone", "")

    if not name or "no information currently present" in name.lower():
        return None

    return ContactRow(
        program_code=program.program_code,
        program_name=program.program_name,
        specialty=program.specialty,
        state=program.state,
        city=program.city,
        role=role,
        name=name,
        email=email,
        phone=phone,
        source_url=program.detail_url,
    )


def heading_text(
        node: Tag,
    ) -> str:
    """Return the normalized panel heading text for a page element."""
    heading = node.find(["h1", "h2", "h3", "h4", "h5", "h6"])
    return clean_text(heading.get_text(" ", strip=True) if heading else node.get_text(" ", strip=True))


def is_contact_section_title(
        title: str,
) -> bool:
    """Return whether a section title is a contact-leadership section."""
    normalized = clean_text(title).lower()
    return normalized in CONTACT_SECTION_TITLES


def is_stop_section_title(
        title: str,
    ) -> bool:
    """Return whether a section title marks the end of contact sections."""
    normalized = clean_text(title).lower()
    return any(hint in normalized for hint in STOP_SECTION_HINTS)


def find_first_content_sibling(
        heading_container: Tag,
    ) -> Tag | None:
    """Find the first useful content block after a contact heading."""
    for sibling in heading_container.find_next_siblings():
        if not isinstance(sibling, Tag):
            continue
        title = heading_text(sibling)
        if sibling.find(class_=re.compile("panel-heading")) and (
            is_contact_section_title(title) or is_stop_section_title(title)
        ):
            return None
        if sibling.find("ul", class_=re.compile("list-unstyled")):
            return sibling
    return None


def parse_contact_rows(
        html: str,
        program: ProgramResult,
    ) -> list[ContactRow]:
    """Parse contact leadership rows from an ACGME program detail page."""
    soup = BeautifulSoup(html, "html.parser")
    rows: list[ContactRow] = []
    seen: set[str] = set()

    for panel_heading in soup.find_all(class_=re.compile(r"\bpanel-heading\b")):
        title = heading_text(panel_heading)
        if not is_contact_section_title(title):
            continue

        heading_container = panel_heading
        while heading_container.parent and isinstance(heading_container.parent, Tag):
            parent = heading_container.parent
            if parent.name == "div" and parent.find(class_=re.compile(r"\bpanel-heading\b")):
                heading_container = parent
                if heading_container.parent and heading_container.parent.name == "div":
                    heading_container = heading_container.parent
                break
            heading_container = parent

        contact_block = heading_container
        if not contact_block.find("ul", class_=re.compile("list-unstyled")):
            contact_block = find_first_content_sibling(heading_container) or contact_block

        row = extract_contact_from_block(contact_block, title, program)
        if not row:
            continue
        key = parsed_contact_key(row)
        if key in seen:
            continue
        seen.add(key)
        rows.append(row)

    return rows


# --- Checkpoint functions ---
def empty_checkpoint() -> dict[str, Any]:
    """Create an empty checkpoint structure."""
    return {
        "created_at": current_utc_iso(),
        "updated_at": current_utc_iso(),
        "completed_program_codes": [],
        "rows": [],
        "errors": [],
    }


def load_checkpoint(
        checkpoint_path: Path,
    ) -> dict[str, Any]:
    """Load a scraper checkpoint from disk or return a new checkpoint."""
    if not checkpoint_path.exists():
        return empty_checkpoint()
    with checkpoint_path.open("r", encoding="utf-8") as handle:
        checkpoint = json.load(handle)
    if not isinstance(checkpoint, dict):
        raise ValueError("Checkpoint JSON must contain an object at the top level.")
    checkpoint.setdefault("created_at", current_utc_iso())
    checkpoint.setdefault("updated_at", current_utc_iso())
    checkpoint.setdefault("completed_program_codes", [])
    checkpoint.setdefault("rows", [])
    checkpoint.setdefault("errors", [])
    return checkpoint


def save_checkpoint(
        checkpoint_path: Path,
        checkpoint: dict[str, Any],
    ) -> None:
    """Persist scraper progress to a checkpoint file."""
    ensure_parent_dir(checkpoint_path)
    checkpoint["updated_at"] = current_utc_iso()
    temporary_path = checkpoint_path.with_suffix(checkpoint_path.suffix + ".tmp")
    with temporary_path.open("w", encoding="utf-8") as handle:
        json.dump(checkpoint, handle, indent=2, sort_keys=True)
    temporary_path.replace(checkpoint_path)


def contact_row_to_checkpoint_dict(
        row: ContactRow,
    ) -> dict[str, str]:
    """Convert a contact row object into the configured checkpoint row mapping."""
    return {
        "Program Code": row.program_code,
        "Program Name": row.program_name,
        "Specialty": row.specialty,
        "State": row.state,
        "City": row.city,
        "Role": row.role,
        "Name": row.name,
        "Email": row.email,
        "Phone": row.phone,
        "Source URL": row.source_url,
    }


def append_rows_to_checkpoint(
        checkpoint: dict[str, Any],
        rows: list[ContactRow],
    ) -> None:
    """Append raw contact rows to the checkpoint without export-time de-duplication."""
    checkpoint.setdefault("rows", [])
    for row in rows:
        checkpoint["rows"].append(contact_row_to_checkpoint_dict(row))


def mark_program_completed(
        checkpoint: dict[str, Any],
        program_code: str,
    ) -> None:
    """Record a program code as completed in the checkpoint."""
    completed = set(checkpoint.get("completed_program_codes", []))
    completed.add(program_code)
    checkpoint["completed_program_codes"] = sorted(completed)


def record_error(
        checkpoint: dict[str, Any],
        program: ProgramResult | None,
        stage: str,
        error: Exception | str,
    ) -> None:
    """Add a scrape error record without aborting the whole run."""
    checkpoint["errors"].append(
        {
            "time": current_utc_iso(),
            "stage": stage,
            "program_code": program.program_code if program else "",
            "program_name": program.program_name if program else "",
            "source_url": program.detail_url if program else "",
            "error": full_error_message(error),
        }
    )


# --- HTTP scraping functions ---
def fetch_with_retries(
        session: requests.Session,
        method: str,
        url: str,
        *,
        delay: float,
        max_retries: int = 3,
        **kwargs: Any,
    ) -> requests.Response:
    """Fetch a URL with polite retry behavior for transient failures."""
    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        response: requests.Response | None = None
        try:
            response = session.request(method, url, timeout=45, **kwargs)
            if response.status_code not in {429, 500, 502, 503, 504}:
                response.raise_for_status()
                return response
            last_error = requests.HTTPError(f"HTTP {response.status_code} for {url}")
        except requests.RequestException as exc:
            last_error = exc

        if attempt >= max_retries:
            break

        retry_after = parse_retry_after_seconds(
            response.headers.get("Retry-After") if response is not None else None
        )
        sleep_for = retry_after if retry_after is not None else delay * (attempt + 1)
        time.sleep(max(sleep_for, 1.0))

    raise RuntimeError(f"Failed to fetch {url}: {last_error}")


def get_search_page(
        session: requests.Session,
        delay: float,
    ) -> str:
    """Fetch the public ACGME search page."""
    response = fetch_with_retries(
        session,
        "GET",
        SEARCH_URL,
        delay=delay,
        headers={**DEFAULT_HEADERS, "Referer": SEARCH_URL},
    )
    return response.text


def search_programs_for_state(
        session: requests.Session,
        state: StateOption,
        token: str,
        delay: float,
    ) -> list[ProgramResult]:
    """Post a state search and parse program results from the response."""
    polite_sleep(delay)
    response = fetch_with_retries(
        session,
        "POST",
        SEARCH_URL,
        delay=delay,
        headers={
            **DEFAULT_HEADERS,
            "Origin": BASE_URL,
            "Referer": SEARCH_URL,
        },
        data={
            "__RequestVerificationToken": token,
            "accreditationTypeId": "2",
            "specialtyId": "",
            "specialtyCategoryTypeId": "",
            "stateId": state.code,
            "city": "",
            "numCode": "",
            "ShowProgramsList": "True",
            "g-recaptcha-response": "",
        },
    )
    return parse_program_results(response.text, state.name)


def detail_url_for_program(
        program: ProgramResult,
    ) -> str:
    """Return a normalized ACGME detail URL for a program result."""
    return urljoin(BASE_URL, detail_path_for_program(program))


def detail_path_for_program(
        program: ProgramResult,
    ) -> str:
    """Return a relative ACGME detail path for browser-side navigation."""
    org_code = org_code_for_program(program)
    return f"/ads/Public/Programs/Detail?orgCode={org_code}"


def org_code_for_program(
        program: ProgramResult,
    ) -> str:
    """Return the ACGME organization code from a program detail URL or program code."""
    parsed = urlparse(program.detail_url)
    return parse_qs(parsed.query).get("orgCode", [program.program_code])[0]


# --- Browser detail functions ---
class PlaywrightDetailFetcher:
    """Reusable Playwright browser session for ACGME program detail pages."""

    def __init__(
            self,
            delay: float,
            timeout_ms: int = 30000,
        ) -> None:
        """Initialize a lazy Playwright detail fetcher."""
        self.delay = delay
        self.timeout_ms = timeout_ms
        self.playwright: Any = None
        self.browser: Any = None
        self.page: Any = None
        self.prepared_state: str | None = None
        self.navigation_failures = 0

    def __enter__(
            self,
        ) -> "PlaywrightDetailFetcher":
        """Enter the context manager without starting the browser until needed."""
        return self

    def __exit__(
            self,
            exc_type: Any,
            exc_value: Any,
            traceback: Any,
        ) -> None:
        """Close Playwright resources when leaving the context manager."""
        self.close()

    def ensure_started(
            self,
        ) -> None:
        """Start Playwright and create one reusable page if not already started."""
        if self.page is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright is not installed; install it with `pip install playwright` "
                "and `python -m playwright install chromium`."
            ) from exc

        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(headless=True)
        self.page = self.browser.new_page(user_agent=DEFAULT_HEADERS["User-Agent"])
        self.page.set_default_timeout(self.timeout_ms)
        self.page.set_default_navigation_timeout(self.timeout_ms)
        self.page.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type in {"font", "image", "media", "stylesheet"}
            else route.continue_(),
        )

    def close(
            self,
        ) -> None:
        """Close the browser and Playwright driver if they were started."""
        if self.browser is not None:
            self.browser.close()
            self.browser = None
        if self.playwright is not None:
            self.playwright.stop()
            self.playwright = None
        self.page = None
        self.prepared_state = None
        self.navigation_failures = 0

    def prepare_state(
            self,
            state_name: str,
            *,
            force: bool = False,
        ) -> None:
        """Load ACGME search context for a state before opening detail pages."""
        if not force and self.prepared_state == state_name:
            return
        self.prepared_state = None
        self.ensure_started()
        self.page.goto(
            SEARCH_URL,
            wait_until="domcontentloaded",
            timeout=self.timeout_ms,
        )
        self.page.select_option("#accreditationTypeFilter", "2")
        self.page.select_option("#stateFilter", label=state_name)
        polite_sleep(self.delay)
        self.page.locator("form").nth(1).locator("button[type='submit']").click(
            timeout=self.timeout_ms,
        )
        self.page.wait_for_load_state(
            "domcontentloaded",
            timeout=self.timeout_ms,
        )
        self.prepared_state = state_name

    def invalidate_prepared_state(
            self,
            *,
            restart_browser: bool = False,
    ) -> None:
        """Clear prepared search state and optionally recreate browser resources."""
        self.prepared_state = None
        if restart_browser:
            self.close()

    def return_to_search_results(
            self,
    ) -> None:
        """Return from a detail page to the prepared search results when possible."""
        try:
            self.page.go_back(
                wait_until="domcontentloaded",
                timeout=self.timeout_ms,
            )
        except Exception:
            self.prepared_state = None

    def mark_navigation_failure(
            self,
    ) -> None:
        """Track a detail navigation failure and recycle the browser if needed."""
        self.navigation_failures += 1
        self.invalidate_prepared_state(
            restart_browser=self.navigation_failures >= DETAIL_FAILURE_COOLDOWN_THRESHOLD,
        )

    def mark_navigation_success(
            self,
    ) -> None:
        """Reset browser failure tracking after a successful detail scrape."""
        self.navigation_failures = 0

    def detail_content_is_ready(
            self,
            html: str,
    ) -> bool:
        """Return whether fetched HTML appears to contain a loaded detail page."""
        if "Please return to the search page" in html:
            raise DetailPageNotReadyError(
                "Detail page required search context and returned a bounce message."
            )
        return any(marker in html for marker in DETAIL_READY_MARKERS)

    def wait_for_detail_content(
            self,
            org_code: str,
    ) -> str:
        """Wait for ACGME detail content using bounded polling."""
        deadline = time.monotonic() + (self.timeout_ms / 1000)
        last_url = ""
        last_length = 0
        last_error = ""
        while time.monotonic() < deadline:
            try:
                last_url = self.page.url
                html = self.page.content()
            except Exception as exc:
                last_error = concise_error_message(exc)
                time.sleep(0.5)
                continue
            last_length = len(html)
            if org_code in last_url and self.detail_content_is_ready(html):
                return html
            time.sleep(0.5)
        raise DetailPageNotReadyError(
            f"Detail page did not become ready for orgCode={org_code}; "
            f"last_url={last_url}; last_html_length={last_length}; "
            f"last_error={last_error}"
        )

    def click_detail_link(
            self,
            program: ProgramResult,
    ) -> None:
        """Click a rendered detail link as a fallback navigation path."""
        org_code = org_code_for_program(program)
        detail_link = self.page.locator(
            f'a[href*="Programs/Detail"][href*="orgCode={org_code}"]'
        ).first
        if detail_link.count() == 0:
            raise DetailLinkMissingError(
                f"Could not find detail link for program {program.program_code}."
            )

        detail_link.click(
            force=True,
            no_wait_after=True,
            timeout=self.timeout_ms,
        )

    def navigate_to_detail(
            self,
            program: ProgramResult,
    ) -> None:
        """Open a detail page from the prepared browser search context."""
        try:
            self.page.evaluate(
                "(path) => { window.location.assign(path); }",
                detail_path_for_program(program),
            )
        except Exception:
            self.click_detail_link(program)

    def fetch_detail_html_once(
            self,
            program: ProgramResult,
        ) -> str:
        """Fetch one ACGME detail page from an already prepared search context."""
        org_code = org_code_for_program(program)
        self.navigate_to_detail(program)
        try:
            return self.wait_for_detail_content(org_code)
        finally:
            self.return_to_search_results()

    def fetch_detail_html(
            self,
            program: ProgramResult,
        ) -> str:
        """Fetch one ACGME detail page with one search-context refresh retry."""
        errors: list[str] = []
        for attempt in range(DETAIL_MAX_ATTEMPTS):
            try:
                self.prepare_state(
                    program.state,
                    force=attempt > 0,
                )
                polite_sleep(self.delay)
                html = self.fetch_detail_html_once(program)
                self.mark_navigation_success()
                return html
            except Exception as exc:
                errors.append(str(exc))
                self.mark_navigation_failure()
                if attempt + 1 < DETAIL_MAX_ATTEMPTS:
                    print_warning(
                        f"{program.program_code}: detail navigation failed; "
                        "refreshing search context."
                    )

        message = f"{program.program_code}: detail navigation failed after 2 attempts"
        raise DetailNavigationError(
            message,
            full_error="\n\n".join(errors),
        )


# --- Core workflow functions ---
def select_states(
        all_states: list[StateOption],
        requested_states: list[str] | None,
    ) -> list[StateOption]:
    """Select all states or a CLI-requested subset by abbreviation/name/code."""
    if not requested_states:
        return all_states

    state_abbreviations = {
        "AL": "Alabama",
        "AK": "Alaska",
        "AZ": "Arizona",
        "AR": "Arkansas",
        "CA": "California",
        "CO": "Colorado",
        "CT": "Connecticut",
        "DE": "Delaware",
        "DC": "District of Columbia",
        "FL": "Florida",
        "GA": "Georgia",
        "HI": "Hawaii",
        "ID": "Idaho",
        "IL": "Illinois",
        "IN": "Indiana",
        "IA": "Iowa",
        "KS": "Kansas",
        "KY": "Kentucky",
        "LA": "Louisiana",
        "ME": "Maine",
        "MD": "Maryland",
        "MA": "Massachusetts",
        "MI": "Michigan",
        "MN": "Minnesota",
        "MS": "Mississippi",
        "MO": "Missouri",
        "MT": "Montana",
        "NE": "Nebraska",
        "NV": "Nevada",
        "NH": "New Hampshire",
        "NJ": "New Jersey",
        "NM": "New Mexico",
        "NY": "New York",
        "NC": "North Carolina",
        "ND": "North Dakota",
        "OH": "Ohio",
        "OK": "Oklahoma",
        "OR": "Oregon",
        "PA": "Pennsylvania",
        "PR": "Puerto Rico",
        "RI": "Rhode Island",
        "SC": "South Carolina",
        "SD": "South Dakota",
        "TN": "Tennessee",
        "TX": "Texas",
        "UT": "Utah",
        "VT": "Vermont",
        "VA": "Virginia",
        "WA": "Washington",
        "WV": "West Virginia",
        "WI": "Wisconsin",
        "WY": "Wyoming",
        "GU": "Guam",
    }
    selected: list[StateOption] = []
    selected_codes: set[str] = set()
    missing: list[str] = []
    for item in requested_states:
        requested = item.strip()
        requested_name = state_abbreviations.get(requested.upper(), requested).lower()
        match = next(
            (
                state
                for state in all_states
                if state.code == requested or state.name.lower() == requested_name
            ),
            None,
        )
        if match is None:
            missing.append(requested)
            continue
        if match.code not in selected_codes:
            selected.append(match)
            selected_codes.add(match.code)
    if missing:
        raise ValueError(f"Could not match requested states: {', '.join(missing)}")
    return selected


def merge_detail_metadata(
        program: ProgramResult,
        detail_html: str,
    ) -> ProgramResult:
    """Fill program metadata gaps from the detail-page heading when available."""
    metadata = parse_detail_header(detail_html)
    if not metadata:
        return program
    return ProgramResult(
        program_code=program.program_code or metadata.get("code", ""),
        program_name=program.program_name or metadata.get("name", ""),
        specialty=program.specialty or metadata.get("specialty", ""),
        state=program.state or metadata.get("state", ""),
        city=program.city or metadata.get("city", ""),
        detail_url=program.detail_url,
    )


def scrape_program_contacts(
        detail_fetcher: PlaywrightDetailFetcher,
        program: ProgramResult,
    ) -> list[ContactRow]:
    """Fetch and parse contact rows for one program through reusable Playwright."""
    detail_html = detail_fetcher.fetch_detail_html(program)
    resolved_program = merge_detail_metadata(program, detail_html)
    return parse_contact_rows(detail_html, resolved_program)


def run_scrape(
        args: argparse.Namespace,
    ) -> int:
    """Run the full ACGME state-by-state scrape workflow."""
    print_banner("ACGME CONTACTS")

    checkpoint_path = Path(args.checkpoint)
    checkpoint = load_checkpoint(checkpoint_path)
    session = build_session()

    print_info("Fetching ACGME search page")
    search_html = get_search_page(session, args.delay)
    token = parse_request_verification_token(search_html)
    states = select_states(parse_state_options(search_html), args.states)
    completed = set(checkpoint.get("completed_program_codes", []))
    program_count = 0
    state_label = "all" if not args.states else ", ".join(state.name for state in states)

    print_run_config(
        "Scrape configuration",
        [
            ("Checkpoint", checkpoint_path),
            ("States", state_label),
            ("Delay", f"{args.delay:.2f}s"),
            ("Max programs", args.max_programs if args.max_programs is not None else "none"),
            ("Force", "yes" if args.force else "no"),
        ],
    )
    if completed and not args.force:
        print_warning(
            f"Resume checkpoint has {len(completed)} completed programs; "
            "completed details will be skipped."
        )
    elif completed and args.force:
        print_warning(
            f"Force mode enabled; {len(completed)} completed programs may be re-scraped."
        )

    with PlaywrightDetailFetcher(delay=args.delay) as detail_fetcher:
        for state in states:
            if args.max_programs is not None and program_count >= args.max_programs:
                save_checkpoint(checkpoint_path, checkpoint)
                print_warning(
                    f"Reached --max-programs={args.max_programs}; "
                    f"saved checkpoint to {checkpoint_path}"
                )
                return 0

            try:
                programs = search_programs_for_state(session, state, token, args.delay)
            except Exception as exc:
                record_error(checkpoint, None, f"state search: {state.name}", exc)
                save_checkpoint(checkpoint_path, checkpoint)
                print_error(f"{state.name}: {concise_error_message(exc)}")
                continue

            remaining_programs = None
            if args.max_programs is not None:
                remaining_programs = max(args.max_programs - program_count, 0)
            state_programs = programs[:remaining_programs] if remaining_programs is not None else programs
            state_contacts = 0
            state_skipped = 0
            state_errors = 0
            consecutive_detail_failures = 0
            cooldown_level = 0

            print_info("")
            print_info(f"{state.name}: found {len(programs)} programs")
            with make_state_progress() as progress:
                task_id = progress.add_task(
                    state.name,
                    total=len(state_programs),
                    contacts=0,
                    skipped=0,
                    errors=0,
                )
                for program in state_programs:
                    program_count += 1
                    try:
                        if not args.force and program.program_code in completed:
                            state_skipped += 1
                            continue

                        rows = scrape_program_contacts(detail_fetcher, program)
                        append_rows_to_checkpoint(checkpoint, rows)
                        mark_program_completed(checkpoint, program.program_code)
                        completed.add(program.program_code)
                        state_contacts += len(rows)
                        consecutive_detail_failures = 0
                        cooldown_level = 0
                        save_checkpoint(checkpoint_path, checkpoint)
                    except Exception as exc:
                        state_errors += 1
                        consecutive_detail_failures += 1
                        record_error(checkpoint, program, "program detail", exc)
                        save_checkpoint(checkpoint_path, checkpoint)
                        print_error(program_error_message(program, exc))
                        if consecutive_detail_failures >= DETAIL_FAILURE_COOLDOWN_THRESHOLD:
                            cooldown_seconds = cooldown_seconds_for_level(cooldown_level)
                            sleep_for_throttle_cooldown(
                                state.name,
                                cooldown_seconds,
                            )
                            consecutive_detail_failures = 0
                            cooldown_level += 1
                    finally:
                        progress.update(
                            task_id,
                            advance=1,
                            contacts=state_contacts,
                            skipped=state_skipped,
                            errors=state_errors,
                        )

            if args.max_programs is not None and program_count >= args.max_programs:
                save_checkpoint(checkpoint_path, checkpoint)
                print_warning(
                    f"Reached --max-programs={args.max_programs}; "
                    f"saved checkpoint to {checkpoint_path}"
                )
                return 0

    save_checkpoint(checkpoint_path, checkpoint)
    print_success(
        f"Saved {len(checkpoint.get('rows', []))} raw contact rows "
        f"to checkpoint {checkpoint_path}"
    )
    return 0


# --- CLI functions ---
def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser for the ACGME scraper."""
    parser = argparse.ArgumentParser(
        description="Scrape public ACGME program leadership contacts into a JSON checkpoint.",
    )
    parser.add_argument(
        "--checkpoint",
        default="data/acgme_checkpoint.json",
        help="Path to the resumable checkpoint JSON file.",
    )
    parser.add_argument(
        "--states",
        nargs="+",
        help="Optional state/territory names, abbreviations, or ACGME state IDs to scrape.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Base delay in seconds between live ACGME requests.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-scrape programs already marked complete in the checkpoint.",
    )
    parser.add_argument(
        "--max-programs",
        type=int,
        help="Optional cap for smoke tests.",
    )
    return parser


def main(
        argv: list[str] | None = None,
    ) -> int:
    """Parse CLI arguments and run the scraper."""
    parser = build_parser()
    args = parser.parse_args(argv)
    return run_scrape(args)


if __name__ == "__main__":
    raise SystemExit(main())
