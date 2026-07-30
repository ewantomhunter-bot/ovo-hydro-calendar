from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from icalendar import Calendar, Event, vText
from zoneinfo import ZoneInfo


# ---------------------------------------------------------------------------
# SETTINGS
# ---------------------------------------------------------------------------

HYDRO_EVENTS_URL = "https://www.ovohydro.com/events/all"
JINA_READER_URL = f"https://r.jina.ai/{HYDRO_EVENTS_URL}"
HYDRO_BASE_URL = "https://www.ovohydro.com"

OUTPUT_FILE = Path("docs/ovo-hydro.ics")
DEBUG_FILE = Path("docs/events.json")

UK_TIMEZONE = ZoneInfo("Europe/London")
UTC = timezone.utc

VENUE_ADDRESS = "OVO Hydro, SEC, Glasgow, G3 8YW"

# The main Hydro listing generally does not provide confirmed performance times.
# A placeholder of 7:30pm is therefore used.
DEFAULT_START_TIME = time(19, 30)
DEFAULT_DURATION = timedelta(hours=3)

JINA_API_KEY = os.environ.get("JINA_API_KEY", "").strip()


# ---------------------------------------------------------------------------
# EVENT MODEL
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class HydroEvent:
    title: str
    start: datetime
    end: datetime
    url: str
    category: str
    date_text: str
    time_is_estimated: bool = True


# ---------------------------------------------------------------------------
# GENERAL HELPERS
# ---------------------------------------------------------------------------

def clean_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def stable_uid(title: str, start: datetime) -> str:
    source = (
        f"{clean_text(title).lower()}|"
        f"{start.date().isoformat()}|"
        "ovo-hydro"
    )

    digest = hashlib.sha256(
        source.encode("utf-8")
    ).hexdigest()[:28]

    return f"{digest}@ovo-hydro-calendar"


def classify_event(title: str) -> str:
    text = title.lower()

    rules = [
        (
            "Comedy",
            (
                "comedy",
                "stand-up",
                "stand up",
                "kevin bridges",
                "peter kay",
                "jack whitehall",
                "john bishop",
                "paul smith",
                "jimmy carr",
                "micky flanagan",
            ),
        ),
        (
            "Sport",
            (
                "boxing",
                "wrestling",
                "aew",
                "ufc",
                "mma",
                "basketball",
                "netball",
                "hockey",
                "darts",
                "commonwealth games",
                "giants live",
                "gladiators",
            ),
        ),
        (
            "Family",
            (
                "family",
                "children",
                "kids",
                "disney",
                "paw patrol",
                "bluey",
                "monster jam",
                "hot wheels",
                "descendants",
                "zombies",
                "camp rock",
            ),
        ),
        (
            "Theatre & Musicals",
            (
                "theatre",
                "theater",
                "musical",
                "ballet",
                "dance",
                "opera",
                "two doors down",
            ),
        ),
        (
            "Classical",
            (
                "orchestra",
                "classical",
                "hans zimmer",
                "andré rieu",
                "andre rieu",
                "anna lapwood",
                "lord of the rings",
            ),
        ),
    ]

    for category, phrases in rules:
        if any(phrase in text for phrase in phrases):
            return category

    return "Music & Entertainment"


# ---------------------------------------------------------------------------
# DATE PARSING
# ---------------------------------------------------------------------------

MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}

MONTH_PATTERN = (
    r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
    r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|"
    r"Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?"
)

WEEKDAY_PATTERN = (
    r"Mon(?:day)?|Tue(?:sday)?|Wed(?:nesday)?|Thu(?:rsday)?|"
    r"Fri(?:day)?|Sat(?:urday)?|Sun(?:day)?"
)

SINGLE_DATE_RE = re.compile(
    rf"(?:{WEEKDAY_PATTERN}\s+)?"
    rf"(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s+"
    rf"(?P<month>{MONTH_PATTERN})"
    rf"\s*(?:/|\s)\s*"
    rf"(?P<year>\d{{2,4}})",
    re.IGNORECASE,
)

SAME_MONTH_RANGE_RE = re.compile(
    rf"(?:{WEEKDAY_PATTERN}\s+)?"
    rf"(?P<start_day>\d{{1,2}})(?:st|nd|rd|th)?"
    rf"\s*[-–]\s*"
    rf"(?P<end_day>\d{{1,2}})(?:st|nd|rd|th)?\s+"
    rf"(?P<month>{MONTH_PATTERN})"
    rf"\s*(?:/|\s)\s*"
    rf"(?P<year>\d{{2,4}})",
    re.IGNORECASE,
)

CROSS_MONTH_RANGE_RE = re.compile(
    rf"(?P<start_day>\d{{1,2}})(?:st|nd|rd|th)?\s+"
    rf"(?P<start_month>{MONTH_PATTERN})"
    rf"\s*[-–]\s*"
    rf"(?P<end_day>\d{{1,2}})(?:st|nd|rd|th)?\s+"
    rf"(?P<end_month>{MONTH_PATTERN})"
    rf"\s*(?:/|\s)\s*"
    rf"(?P<year>\d{{2,4}})",
    re.IGNORECASE,
)

TWO_DATES_RE = re.compile(
    rf"(?P<first_day>\d{{1,2}})(?:st|nd|rd|th)?"
    rf"\s*(?:&|and)\s*"
    rf"(?P<second_day>\d{{1,2}})(?:st|nd|rd|th)?\s+"
    rf"(?P<month>{MONTH_PATTERN})"
    rf"\s*(?:/|\s)\s*"
    rf"(?P<year>\d{{2,4}})",
    re.IGNORECASE,
)

DATE_LINE_RE = re.compile(
    rf"(?:{WEEKDAY_PATTERN}\s+)?"
    rf"\d{{1,2}}(?:st|nd|rd|th)?"
    rf".*?"
    rf"(?:{MONTH_PATTERN})"
    rf".*?"
    rf"\d{{2,4}}",
    re.IGNORECASE,
)

MARKDOWN_LINK_RE = re.compile(
    r"^\[(?P<title>.+?)\]\((?P<url>https?://[^)]+|/[^)]+)\)$"
)


def normalise_year(raw_year: str) -> int:
    year = int(raw_year)

    if year < 100:
        return 2000 + year

    return year


def month_number(raw_month: str) -> int:
    key = raw_month.lower().rstrip(".")

    if key not in MONTHS:
        raise ValueError(f"Unknown month: {raw_month}")

    return MONTHS[key]


def make_datetime(event_date: date) -> datetime:
    return datetime.combine(
        event_date,
        DEFAULT_START_TIME,
        tzinfo=UK_TIMEZONE,
    )


def parse_date_line(
    date_text: str,
) -> list[tuple[datetime, datetime]]:
    """
    Parse a single date line from the Hydro event listing.

    Supported examples:
    - Mon 17 Aug / 26
    - 25 - 27 Sep 2026
    - 30 Sep - 01 Oct / 26
    - 08 & 10 Mar 2027
    - 11 Sep - 26 Nov / 26
    """
    text = clean_text(date_text)
    results: list[tuple[datetime, datetime]] = []

    match = TWO_DATES_RE.search(text)

    if match:
        year = normalise_year(match.group("year"))
        month = month_number(match.group("month"))

        for day_key in ("first_day", "second_day"):
            event_date = date(
                year,
                month,
                int(match.group(day_key)),
            )

            start = make_datetime(event_date)
            results.append(
                (start, start + DEFAULT_DURATION)
            )

        return results

    match = CROSS_MONTH_RANGE_RE.search(text)

    if match:
        year = normalise_year(match.group("year"))

        start_date = date(
            year,
            month_number(match.group("start_month")),
            int(match.group("start_day")),
        )

        end_date = date(
            year,
            month_number(match.group("end_month")),
            int(match.group("end_day")),
        )

        start = make_datetime(start_date)
        end = make_datetime(end_date) + DEFAULT_DURATION

        return [(start, end)]

    match = SAME_MONTH_RANGE_RE.search(text)

    if match:
        year = normalise_year(match.group("year"))
        month = month_number(match.group("month"))

        start_date = date(
            year,
            month,
            int(match.group("start_day")),
        )

        end_date = date(
            year,
            month,
            int(match.group("end_day")),
        )

        start = make_datetime(start_date)
        end = make_datetime(end_date) + DEFAULT_DURATION

        return [(start, end)]

    match = SINGLE_DATE_RE.search(text)

    if match:
        event_date = date(
            normalise_year(match.group("year")),
            month_number(match.group("month")),
            int(match.group("day")),
        )

        start = make_datetime(event_date)

        return [(start, start + DEFAULT_DURATION)]

    return []


# ---------------------------------------------------------------------------
# JINA READER DOWNLOAD
# ---------------------------------------------------------------------------

def download_hydro_listing() -> str:
    if not JINA_API_KEY:
        raise RuntimeError(
            "The JINA_API_KEY GitHub secret is missing or unavailable."
        )

    headers = {
        "Authorization": f"Bearer {JINA_API_KEY}",
        "Accept": "text/plain",
        "User-Agent": "OVO-Hydro-Calendar/1.0",
        "X-Engine": "browser",
        "X-Proxy": "auto",
        "X-No-Cache": "true",
        "X-Timeout": "60",
    }

    response = requests.get(
        JINA_READER_URL,
        headers=headers,
        timeout=90,
    )

    if response.status_code == 401:
        raise RuntimeError(
            "Jina rejected the API key. Check the JINA_API_KEY secret."
        )

    if response.status_code == 403:
        raise RuntimeError(
            "Jina returned 403 Forbidden despite authentication. "
            "Check that the secret contains the complete API key."
        )

    response.raise_for_status()

    text = response.text.strip()

    if not text:
        raise RuntimeError(
            "Jina Reader returned an empty Hydro events page."
        )

    return text


# ---------------------------------------------------------------------------
# MARKDOWN EXTRACTION
# ---------------------------------------------------------------------------

def extract_heading_details(
    raw_heading: str,
) -> tuple[str, str]:
    """
    Extract an event title and URL from either:

    ### Event Name

    or:

    ### [Event Name](https://...)
    """
    heading = clean_text(
        re.sub(r"^#{1,6}\s*", "", raw_heading)
    )

    link_match = MARKDOWN_LINK_RE.match(heading)

    if link_match:
        title = clean_text(link_match.group("title"))
        url = urljoin(
            HYDRO_BASE_URL,
            link_match.group("url"),
        )
        return title, url

    return heading, HYDRO_EVENTS_URL


def extract_events(
    page_text: str,
) -> list[HydroEvent]:
    lines = [
        clean_text(line)
        for line in page_text.splitlines()
    ]

    events: list[HydroEvent] = []
    pending_date_lines: list[str] = []

    ignored_headings = {
        "what's on",
        "events",
        "upcoming events",
        "sort by month",
        "sort by category",
        "venue premium seating",
        "don't miss out",
    }

    for line in lines:
        if not line:
            continue

        # Store date lines until the following H3 event title appears.
        if DATE_LINE_RE.search(line):
            pending_date_lines.append(line)

            # An event normally has no more than two distinct date lines.
            if len(pending_date_lines) > 3:
                pending_date_lines = pending_date_lines[-3:]

            continue

        if not line.startswith("### "):
            continue

        title, url = extract_heading_details(line)

        if not title:
            pending_date_lines = []
            continue

        if title.lower() in ignored_headings:
            pending_date_lines = []
            continue

        if not pending_date_lines:
            continue

        parsed_occurrences: list[
            tuple[datetime, datetime, str]
        ] = []

        for date_line in pending_date_lines:
            for start, end in parse_date_line(date_line):
                parsed_occurrences.append(
                    (start, end, date_line)
                )

        if not parsed_occurrences:
            print(
                f"Warning: no usable date found for {title!r} "
                f"from {pending_date_lines!r}",
                file=sys.stderr,
            )
            pending_date_lines = []
            continue

        for occurrence_number, (
            start,
            end,
            source_date_text,
        ) in enumerate(parsed_occurrences, start=1):

            display_title = title

            if len(parsed_occurrences) > 1:
                display_title = (
                    f"{title} — Date {occurrence_number}"
                )

            events.append(
                HydroEvent(
                    title=display_title,
                    start=start,
                    end=end,
                    url=url,
                    category=classify_event(title),
                    date_text=source_date_text,
                )
            )

        pending_date_lines = []

    return deduplicate_events(events)


def deduplicate_events(
    events: list[HydroEvent],
) -> list[HydroEvent]:
    unique: dict[
        tuple[str, str],
        HydroEvent,
    ] = {}

    for event in events:
        title_key = re.sub(
            r"[^a-z0-9]+",
            "",
            event.title.lower(),
        )

        key = (
            title_key,
            event.start.isoformat(),
        )

        if key not in unique:
            unique[key] = event

    return sorted(
        unique.values(),
        key=lambda item: (
            item.start,
            item.title.lower(),
        ),
    )


# ---------------------------------------------------------------------------
# CALENDAR CREATION
# ---------------------------------------------------------------------------

def build_calendar(
    events: list[HydroEvent],
) -> Calendar:
    calendar = Calendar()

    calendar.add(
        "prodid",
        "-//Ewan Hunter//OVO Hydro Calendar//EN",
    )
    calendar.add("version", "2.0")
    calendar.add("calscale", "GREGORIAN")
    calendar.add("method", "PUBLISH")
    calendar.add(
        "x-wr-calname",
        "OVO Hydro Events",
    )
    calendar.add(
        "x-wr-timezone",
        "Europe/London",
    )
    calendar.add(
        "refresh-interval;value=duration",
        "PT12H",
    )
    calendar.add(
        "x-published-ttl",
        "PT12H",
    )

    generated_at = datetime.now(UTC)

    for item in events:
        calendar_event = Event()

        calendar_event.add(
            "uid",
            stable_uid(
                item.title,
                item.start,
            ),
        )
        calendar_event.add(
            "dtstamp",
            generated_at,
        )
        calendar_event.add(
            "last-modified",
            generated_at,
        )
        calendar_event.add(
            "summary",
            item.title,
        )
        calendar_event.add(
            "dtstart",
            item.start,
        )
        calendar_event.add(
            "dtend",
            item.end,
        )
        calendar_event.add(
            "url",
            item.url,
        )
        calendar_event.add(
            "categories",
            [
                "OVO Hydro",
                item.category,
            ],
        )

        calendar_event["location"] = vText(
            VENUE_ADDRESS
        )

        description = (
            f"Listed date: {item.date_text}\n\n"
            "The main OVO Hydro listing does not normally provide a "
            "confirmed show time, so 7:30pm has been used as a placeholder. "
            "Check the official event page before travelling.\n\n"
            f"Official listing: {item.url}"
        )

        calendar_event.add(
            "description",
            description,
        )

        calendar.add_component(calendar_event)

    return calendar


def write_debug_file(
    events: list[HydroEvent],
) -> None:
    payload = [
        {
            "title": item.title,
            "start": item.start.isoformat(),
            "end": item.end.isoformat(),
            "category": item.category,
            "date_text": item.date_text,
            "url": item.url,
            "time_is_estimated": item.time_is_estimated,
        }
        for item in events
    ]

    DEBUG_FILE.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main() -> int:
    try:
        page_text = download_hydro_listing()
        events = extract_events(page_text)

        # Prevent a partial scrape from replacing a healthy feed.
        if len(events) < 20:
            raise RuntimeError(
                f"Only {len(events)} event entries were detected. "
                "The existing calendar has not been overwritten."
            )

        OUTPUT_FILE.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        calendar = build_calendar(events)

        OUTPUT_FILE.write_bytes(
            calendar.to_ical()
        )

        write_debug_file(events)

        print(
            f"Successfully published {len(events)} "
            "OVO Hydro calendar entries."
        )

        return 0

    except requests.RequestException as error:
        print(
            f"Could not download the rendered Hydro page: {error}",
            file=sys.stderr,
        )
        return 1

    except Exception as error:
        print(
            f"Calendar update failed: {error}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
