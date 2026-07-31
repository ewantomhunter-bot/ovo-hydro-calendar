from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

from playwright.sync_api import BrowserContext, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright
from bs4 import BeautifulSoup
from icalendar import Calendar, Event, vText
from zoneinfo import ZoneInfo

SCRIPT_VERSION = "8.1.0"
EVENTS_URL = "https://www.ovohydro.com/events/all"
BASE_URL = "https://www.ovohydro.com"
OUTPUT_FILE = Path("docs/ovo-hydro.ics")
DEBUG_FILE = Path("docs/events.json")
VENUE = "OVO Hydro, SEC, Glasgow, G3 8YW"
TZ = ZoneInfo("Europe/London")
UTC = timezone.utc
DEFAULT_START = time(19, 30)
DEFAULT_DURATION = timedelta(hours=3)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "Chrome/126.0 Safari/537.36 OVO-Hydro-Calendar/6.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    "Cache-Control": "no-cache",
}

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2,
    "mar": 3, "march": 3, "apr": 4, "april": 4, "may": 5,
    "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}
MONTH_RE = (
    r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|"
    r"Nov(?:ember)?|Dec(?:ember)?"
)
WEEKDAY_RE = r"Mon(?:day)?|Tue(?:sday)?|Wed(?:nesday)?|Thu(?:rsday)?|Fri(?:day)?|Sat(?:urday)?|Sun(?:day)?"
DATE_RE = re.compile(
    rf"(?:{WEEKDAY_RE}\s+)?(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s+"
    rf"(?P<month>{MONTH_RE})\s*(?:/|\s)\s*(?P<year>\d{{2,4}})",
    re.I,
)
DOORS_RE = re.compile(r"\bDOORS?\s*:?\s*(\d{1,2}(?:[.:]\d{2})?\s*(?:am|pm)?)", re.I)
SHOW_RE = re.compile(r"\bSHOW\s*TIME\s*:?\s*(\d{1,2}(?:[.:]\d{2})?\s*(?:am|pm)?)", re.I)
CLOCK_RE = re.compile(r"^(\d{1,2})(?:[.:](\d{2}))?\s*(am|pm)?$", re.I)


@dataclass(frozen=True)
class Entry:
    title: str
    start: datetime
    end: datetime
    url: str
    timing_source: str
    source_text: str


def clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", clean(value).lower())


def parse_date(text: str) -> date | None:
    match = DATE_RE.search(text)
    if not match:
        return None
    year = int(match.group("year"))
    if year < 100:
        year += 2000
    month_name = match.group("month").lower().rstrip(".")
    try:
        return date(year, MONTHS[month_name], int(match.group("day")))
    except (KeyError, ValueError):
        return None


def parse_clock(text: str) -> time | None:
    match = CLOCK_RE.match(clean(text))
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    marker = (match.group(3) or "").lower()
    if marker == "pm" and hour != 12:
        hour += 12
    elif marker == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    return time(hour, minute)


def open_page(page: Page, url: str) -> None:
    """Load a Hydro page in a real Chromium browser and wait for rendered content."""
    last_error: Exception | None = None

    for attempt in range(1, 4):
        try:
            response = page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=90_000,
            )

            if response is not None and response.status >= 400:
                raise RuntimeError(
                    f"HTTP {response.status} while loading {url}"
                )

            page.wait_for_selector("body", timeout=30_000)
            page.wait_for_timeout(1_500)

            body_text = page.locator("body").inner_text(timeout=30_000)
            if len(body_text.strip()) < 500:
                raise RuntimeError(
                    f"Rendered page was suspiciously short "
                    f"({len(body_text)} characters)"
                )

            return

        except Exception as exc:
            last_error = exc
            if attempt < 3:
                page.wait_for_timeout(attempt * 2_000)

    raise RuntimeError(f"Could not render {url}: {last_error}")


def expand_current_event_listing(page: Page) -> None:
    """
    Expand the official What's On page until no further current event cards
    can be revealed. Hidden or archived detail links are ignored.
    """
    button_pattern = re.compile(
        r"(load|show|view)\s+more(\s+events)?",
        re.IGNORECASE,
    )

    for _ in range(30):
        clicked = False

        candidates = [
            page.get_by_role("button", name=button_pattern),
            page.get_by_role("link", name=button_pattern),
            page.get_by_text(button_pattern),
        ]

        for locator in candidates:
            try:
                for index in range(locator.count()):
                    candidate = locator.nth(index)
                    if not candidate.is_visible():
                        continue

                    before = page.locator(
                        'main a[href*="/events/detail/"]'
                    ).count()

                    candidate.scroll_into_view_if_needed()
                    candidate.click(timeout=10_000)
                    page.wait_for_timeout(1_000)

                    after = page.locator(
                        'main a[href*="/events/detail/"]'
                    ).count()

                    clicked = after > before
                    if clicked:
                        break
            except Exception:
                continue

            if clicked:
                break

        if not clicked:
            return


def listing_links(page: Page) -> list[tuple[str, str]]:
    """
    Return only event pages represented by visible cards on the current
    official OVO Hydro What's On listing.

    This prevents stale or archived event pages from entering the feed.
    """
    open_page(page, EVENTS_URL)
    expand_current_event_listing(page)

    items: dict[str, tuple[str, str]] = {}

    anchors = page.locator('main a[href*="/events/detail/"]')
    if anchors.count() == 0:
        anchors = page.locator('body a[href*="/events/detail/"]')

    ignored_text = {
        "find tickets",
        "more info",
        "view event",
        "book now",
        "buy tickets",
    }

    for index in range(anchors.count()):
        anchor = anchors.nth(index)

        try:
            if not anchor.is_visible():
                continue
        except Exception:
            continue

        href = clean(anchor.get_attribute("href"))
        if not href:
            continue

        url = urljoin(BASE_URL, href.split("?")[0])

        title = clean(
            anchor.evaluate(
                """
                (node) => {
                    const card = node.closest(
                        'article, li, [class*="event"], [class*="card"]'
                    ) || node.parentElement;
                    const heading = card
                        ? card.querySelector('h1, h2, h3, h4')
                        : null;
                    return heading
                        ? heading.textContent
                        : node.textContent;
                }
                """
            )
        )

        if not title or title.lower() in ignored_text:
            continue

        items[url] = (title, url)

    return list(items.values())


def expand_all_showings(page: Page) -> None:
    """Expand collapsed performance rows before reading the event page."""
    selectors = [
        page.get_by_text("View All Showings", exact=True),
        page.get_by_role("button", name=re.compile(r"view all showings", re.I)),
        page.get_by_role("link", name=re.compile(r"view all showings", re.I)),
    ]

    for locator in selectors:
        try:
            if locator.count() == 0:
                continue

            candidate = locator.first
            if not candidate.is_visible():
                continue

            before = page.locator("body").inner_text(timeout=30_000)
            candidate.scroll_into_view_if_needed()
            candidate.click(timeout=15_000)
            page.wait_for_timeout(1_000)

            # Some versions animate or populate the remaining rows after click.
            for _ in range(10):
                after = page.locator("body").inner_text(timeout=30_000)
                if len(after) > len(before) + 100:
                    return
                page.wait_for_timeout(300)

            return

        except Exception:
            continue


def detail_lines(page: Page, expected_title: str) -> tuple[str, list[str]]:
    expand_all_showings(page)

    h1 = page.locator("h1").first
    if h1.count() == 0:
        raise RuntimeError("Detail page has no H1 event title")

    title = clean(h1.inner_text())
    body_text = page.locator("body").inner_text(timeout=30_000)
    lines = [clean(line) for line in body_text.splitlines()]
    lines = [line for line in lines if line]

    possible_titles = {key(title), key(expected_title)}

    try:
        title_index = next(
            i for i, line in enumerate(lines)
            if key(line) in possible_titles
        )
    except StopIteration as exc:
        raise RuntimeError(
            f"Could not locate {title!r} in rendered detail-page text"
        ) from exc

    section: list[str] = []
    for line in lines[title_index + 1:title_index + 400]:
        lowered = line.lower()

        # After expansion this control normally sits after the complete
        # performance list. If it appears before any dates, ignore it.
        if lowered == "view all showings":
            if any(parse_date(existing) is not None for existing in section):
                break
            continue

        section.append(line)

    return title, section


def parse_detail(page: Page, url: str, expected_title: str) -> list[Entry]:
    """
    Parse only the site's actual performance rows.

    Reading the whole page body is unsafe: presale dates, reschedule notices,
    FAQ copy and related content can all contain valid-looking dates. The
    `.showing_item` elements are the authoritative list of performances.
    """
    expand_all_showings(page)

    h1 = page.locator("h1").first
    if h1.count() == 0:
        raise RuntimeError("Detail page has no H1 event title")
    title = clean(h1.inner_text()) or clean(expected_title)

    rows = page.locator(".showings_list .showing_item")
    if rows.count() == 0:
        rows = page.locator(".showings .showing_item")

    print(f"  rendered showing rows for {title}: {rows.count()}")

    today = datetime.now(TZ).date()
    entries: list[Entry] = []

    for index in range(rows.count()):
        row = rows.nth(index)
        source_text = clean(row.inner_text())

        date_locator = row.locator(".date").first
        if date_locator.count() == 0:
            continue

        event_date = parse_date(clean(date_locator.inner_text()))
        if event_date is None or event_date < today:
            continue

        doors: time | None = None
        show: time | None = None

        doors_locator = row.locator(".doors").first
        if doors_locator.count() > 0:
            doors_text = clean(doors_locator.inner_text())
            doors_match = DOORS_RE.search(doors_text)
            if doors_match:
                doors = parse_clock(doors_match.group(1))

        description_locator = row.locator(".showing_description").first
        if description_locator.count() > 0:
            description_text = clean(description_locator.inner_text())
            show_match = SHOW_RE.search(description_text)
            if show_match:
                show = parse_clock(show_match.group(1))

        # Some site variants put the timing text directly in the row.
        if doors is None:
            doors_match = DOORS_RE.search(source_text)
            if doors_match:
                doors = parse_clock(doors_match.group(1))
        if show is None:
            show_match = SHOW_RE.search(source_text)
            if show_match:
                show = parse_clock(show_match.group(1))

        if show is not None:
            chosen, timing_source = show, "published_show_time"
        elif doors is not None:
            chosen, timing_source = doors, "published_doors_time"
        else:
            chosen, timing_source = DEFAULT_START, "estimated_19_30"

        start = datetime.combine(event_date, chosen, tzinfo=TZ)
        entries.append(
            Entry(
                title,
                start,
                start + DEFAULT_DURATION,
                url,
                timing_source,
                source_text,
            )
        )

    return entries


def deduplicate(entries: list[Entry]) -> list[Entry]:
    """
    Remove duplicate representations of the same performance.

    Some Hydro pages repeat a date in more than one rendered block. One copy
    may contain a doors time while another has no time and therefore receives
    the provisional 7:30pm fallback. Those are not two separate performances.

    For each event and calendar date:
      1. Keep all distinct official SHOW times, where present.
      2. Otherwise keep all distinct official DOORS times.
      3. Otherwise keep one provisional entry.

    This preserves genuine same-day matinee/evening performances because their
    official show times are different.
    """
    exact: dict[tuple[str, str], Entry] = {}
    for entry in entries:
        exact[(key(entry.title), entry.start.isoformat())] = entry

    grouped: dict[tuple[str, date], list[Entry]] = {}
    for entry in exact.values():
        grouped.setdefault((key(entry.title), entry.start.date()), []).append(entry)

    result: list[Entry] = []

    for group in grouped.values():
        official_show = [
            entry for entry in group
            if entry.timing_source == "published_show_time"
        ]
        official_doors = [
            entry for entry in group
            if entry.timing_source == "published_doors_time"
        ]
        provisional = [
            entry for entry in group
            if entry.timing_source == "estimated_19_30"
        ]

        if official_show:
            chosen = official_show
        elif official_doors:
            chosen = official_doors
        else:
            chosen = provisional[:1]

        # A final exact-time pass protects against repeated rendered blocks.
        by_start: dict[str, Entry] = {}
        for entry in chosen:
            by_start[entry.start.isoformat()] = entry
        result.extend(by_start.values())

    return sorted(result, key=lambda e: (e.start, e.title.lower()))


def uid(entry: Entry, performances_for_url: int) -> str:
    """
    Use the event page URL as the stable identity for a one-performance event.

    If a one-off event is rescheduled, Apple Calendar receives the same UID
    with a revised DTSTART instead of retaining the old date as a separate
    event. Multi-performance events still use date/time-specific UIDs.
    """
    canonical_url = entry.url.split("?")[0].rstrip("/").lower()

    if performances_for_url == 1:
        raw = f"{canonical_url}|ovo-hydro"
    else:
        raw = (
            f"{canonical_url}|{entry.start.isoformat()}|"
            "ovo-hydro"
        )

    return (
        hashlib.sha256(raw.encode("utf-8")).hexdigest()[:28]
        + "@ovo-hydro-calendar"
    )


def build_calendar(entries: list[Entry]) -> bytes:
    cal = Calendar()
    cal.add("prodid", "-//Ewan Hunter//OVO Hydro Calendar//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    cal.add("x-wr-calname", "OVO Hydro Events")
    cal.add("x-wr-timezone", "Europe/London")
    cal.add("refresh-interval;value=duration", "PT12H")
    cal.add("x-published-ttl", "PT12H")
    generated = datetime.now(UTC)

    performance_counts: dict[str, int] = {}
    for entry in entries:
        canonical_url = entry.url.split("?")[0].rstrip("/").lower()
        performance_counts[canonical_url] = (
            performance_counts.get(canonical_url, 0) + 1
        )

    for entry in entries:
        event = Event()
        canonical_url = entry.url.split("?")[0].rstrip("/").lower()
        event.add(
            "uid",
            uid(entry, performance_counts[canonical_url]),
        )
        event.add("dtstamp", generated)
        event.add("last-modified", generated)
        event.add("summary", entry.title)
        event.add("dtstart", entry.start)
        event.add("dtend", entry.end)
        event.add("url", entry.url)
        event["location"] = vText(VENUE)
        if entry.timing_source == "published_show_time":
            note = "Time shown is the official published show time."
        elif entry.timing_source == "published_doors_time":
            note = "Time shown is the official doors time; the show time has not yet been confirmed."
        else:
            note = "No official time was published, so 7:30pm is provisional."
        event.add("description", f"{note}\n\nOfficial event page: {entry.url}")
        cal.add_component(event)
    return cal.to_ical()


def validate(entries: list[Entry], title_count: int) -> None:
    if title_count < 20:
        raise RuntimeError(f"Only {title_count} event pages found; refusing to publish")
    if len(entries) < title_count:
        raise RuntimeError(f"Only {len(entries)} showings from {title_count} event pages; refusing to publish")

    # Regression checks for the two events that exposed the previous bugs.
    by_title: dict[str, list[Entry]] = {}
    for entry in entries:
        by_title.setdefault(key(entry.title), []).append(entry)

    bridges = next((v for k, v in by_title.items() if "kevinbridges" in k), None)
    if bridges is not None and len(bridges) < 12:
        raise RuntimeError(f"Kevin Bridges returned only {len(bridges)} showings; refusing to publish")

    doors = next((v for k, v in by_title.items() if k == "twodoorsdown"), None)
    if doors is not None:
        if len(doors) < 10:
            raise RuntimeError(f"Two Doors Down returned only {len(doors)} showings; refusing to publish")
        if not any(e.start.hour == 14 for e in doors):
            raise RuntimeError("Two Doors Down matinee performances are missing")
        if not any(e.start.hour == 18 and e.start.minute == 30 for e in doors):
            raise RuntimeError("Two Doors Down evening performances are missing")


def main() -> int:
    try:
        print(f"Starting OVO Hydro calendar scraper V{SCRIPT_VERSION}")

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)

            context: BrowserContext = browser.new_context(
                locale="en-GB",
                timezone_id="Europe/London",
                viewport={"width": 1440, "height": 1200},
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/128.0.0.0 Safari/537.36"
                ),
            )

            page = context.new_page()
            page.set_default_timeout(30_000)

            links = listing_links(page)
            print(f"Found {len(links)} official event detail pages")

            if len(links) < 20:
                raise RuntimeError(
                    f"Only {len(links)} event detail pages were found"
                )

            entries: list[Entry] = []

            for position, (listing_title, url) in enumerate(links, start=1):
                try:
                    open_page(page, url)
                    event_entries = parse_detail(
                        page,
                        url,
                        listing_title,
                    )

                    if event_entries:
                        entries.extend(event_entries)
                        print(
                            f"[{position}/{len(links)}] "
                            f"{listing_title}: "
                            f"{len(event_entries)} showing(s)"
                        )
                    else:
                        print(
                            f"[{position}/{len(links)}] "
                            f"{listing_title}: "
                            "no future showing rows found"
                        )

                except Exception as exc:
                    print(
                        f"[{position}/{len(links)}] "
                        f"{listing_title}: skipped: {exc}"
                    )

            browser.close()

        entries = deduplicate(entries)

        if len(entries) < 20:
            raise RuntimeError(
                f"Only {len(entries)} future calendar entries were parsed; "
                "refusing to overwrite the existing feed."
            )

        # Known multi-show events are logged prominently for checking.
        for expected in ("Kevin Bridges", "Two Doors Down"):
            matches = [
                entry for entry in entries
                if expected.lower() in entry.title.lower()
            ]
            if matches:
                print(
                    f"CHECK {expected}: "
                    f"{len(matches)} calendar entries"
                )

        OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT_FILE.write_bytes(build_calendar(entries))

        DEBUG_FILE.write_text(
            json.dumps(
                [
                    {
                        **asdict(entry),
                        "start": entry.start.isoformat(),
                        "end": entry.end.isoformat(),
                        "script_version": SCRIPT_VERSION,
                        "source": "visible_current_ovo_listing",
                    }
                    for entry in entries
                ],
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        print(
            f"Successfully published {len(entries)} "
            "future calendar entries."
        )
        return 0

    except Exception as exc:
        print(f"Calendar update blocked: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
