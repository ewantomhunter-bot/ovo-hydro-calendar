from __future__ import annotations

import hashlib
import json
import re
import sys
import time as time_module
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from icalendar import Calendar, Event, vText
from zoneinfo import ZoneInfo

SCRIPT_VERSION = "6.0.0"
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


def fetch(session: requests.Session, url: str) -> str:
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            response = session.get(url, timeout=45)
            response.raise_for_status()
            if len(response.text) < 1000:
                raise RuntimeError(f"Suspiciously short response ({len(response.text)} bytes)")
            return response.text
        except Exception as exc:
            last_error = exc
            if attempt < 3:
                time_module.sleep(attempt * 2)
    raise RuntimeError(f"Could not download {url}: {last_error}")


def listing_links(html: str) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    items: dict[str, tuple[str, str]] = {}
    for anchor in soup.select('a[href*="/events/detail/"]'):
        href = clean(anchor.get("href"))
        title = clean(anchor.get_text(" ", strip=True))
        if not href or not title or title.lower() in {"find tickets", "more info"}:
            continue
        url = urljoin(BASE_URL, href.split("?")[0])
        items[url] = (title, url)
    return list(items.values())


def detail_lines(html: str) -> tuple[str, list[str]]:
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.find("h1")
    if not h1:
        raise RuntimeError("Detail page has no H1 event title")
    title = clean(h1.get_text(" ", strip=True))

    # Convert the page into meaningful text lines. The official site presents
    # each showing before the literal 'View All Showings' marker.
    lines = [clean(line) for line in soup.get_text("\n", strip=True).splitlines()]
    lines = [line for line in lines if line]

    try:
        title_index = next(i for i, line in enumerate(lines) if key(line) == key(title))
    except StopIteration as exc:
        raise RuntimeError(f"Could not locate {title!r} in detail-page text") from exc

    section: list[str] = []
    for line in lines[title_index + 1:title_index + 250]:
        if line.lower() == "view all showings":
            break
        section.append(line)
    return title, section


def parse_detail(html: str, url: str) -> list[Entry]:
    title, lines = detail_lines(html)
    today = datetime.now(TZ).date()
    entries: list[Entry] = []
    current_date: date | None = None
    current_source = ""
    current_doors: time | None = None
    current_show: time | None = None

    def flush() -> None:
        nonlocal current_date, current_source, current_doors, current_show
        if current_date is None or current_date < today:
            current_date = None
            current_doors = None
            current_show = None
            return
        if current_show is not None:
            chosen, timing_source = current_show, "published_show_time"
        elif current_doors is not None:
            chosen, timing_source = current_doors, "published_doors_time"
        else:
            chosen, timing_source = DEFAULT_START, "estimated_19_30"
        start = datetime.combine(current_date, chosen, tzinfo=TZ)
        entries.append(Entry(title, start, start + DEFAULT_DURATION, url, timing_source, current_source))
        current_date = None
        current_doors = None
        current_show = None

    for line in lines:
        event_date = parse_date(line)
        if event_date is not None:
            flush()
            current_date = event_date
            current_source = line
            doors_match = DOORS_RE.search(line)
            if doors_match:
                current_doors = parse_clock(doors_match.group(1))
            continue

        if current_date is None:
            continue
        doors_match = DOORS_RE.search(line)
        if doors_match:
            current_doors = parse_clock(doors_match.group(1))
        show_match = SHOW_RE.search(line)
        if show_match:
            current_show = parse_clock(show_match.group(1))

    flush()
    return entries


def deduplicate(entries: list[Entry]) -> list[Entry]:
    unique: dict[tuple[str, str], Entry] = {}
    for entry in entries:
        unique[(key(entry.title), entry.start.isoformat())] = entry
    return sorted(unique.values(), key=lambda e: (e.start, e.title.lower()))


def uid(entry: Entry) -> str:
    raw = f"{key(entry.title)}|{entry.start.isoformat()}|ovo-hydro"
    return hashlib.sha256(raw.encode()).hexdigest()[:28] + "@ovo-hydro-calendar"


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

    for entry in entries:
        event = Event()
        event.add("uid", uid(entry))
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
        session = requests.Session()
        session.headers.update(HEADERS)

        links = listing_links(fetch(session, EVENTS_URL))
        print(f"Found {len(links)} official event detail pages")
        if len(links) < 20:
            raise RuntimeError("Main listing did not expose enough event links")

        all_entries: list[Entry] = []
        failures: list[str] = []
        for index, (listing_title, url) in enumerate(links, 1):
            try:
                parsed = parse_detail(fetch(session, url), url)
                if not parsed:
                    raise RuntimeError("no future showing rows found")
                all_entries.extend(parsed)
                print(f"[{index}/{len(links)}] {listing_title}: {len(parsed)} showing(s)")
            except Exception as exc:
                failures.append(f"{listing_title}: {exc}")
                print(f"WARNING: {listing_title}: {exc}", file=sys.stderr)

        if failures:
            raise RuntimeError("One or more event pages failed:\n" + "\n".join(failures))

        entries = deduplicate(all_entries)
        validate(entries, len(links))

        OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT_FILE.write_bytes(build_calendar(entries))
        DEBUG_FILE.write_text(
            json.dumps([
                {
                    **asdict(e),
                    "start": e.start.isoformat(),
                    "end": e.end.isoformat(),
                    "script_version": SCRIPT_VERSION,
                }
                for e in entries
            ], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"Published {len(entries)} calendar entries across {len(links)} events")
        return 0
    except Exception as exc:
        print(f"Calendar update blocked: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
