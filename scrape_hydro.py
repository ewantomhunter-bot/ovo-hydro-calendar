from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import unicodedata
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
HYDRO_BASE_URL = "https://www.ovohydro.com"
JINA_READER_BASE_URL = "https://r.jina.ai/"

OUTPUT_FILE = Path("docs/ovo-hydro.ics")
DEBUG_FILE = Path("docs/events.json")

UK_TIMEZONE = ZoneInfo("Europe/London")
UTC = timezone.utc

VENUE_ADDRESS = "OVO Hydro, SEC, Glasgow, G3 8YW"

DEFAULT_START_TIME = time(19, 30)
DEFAULT_DURATION = timedelta(hours=3)

# Short ranges such as 25–27 September are expanded into every date.
# Longer ranges are treated as summaries and checked against the event page.
MAX_CONTINUOUS_RANGE_DAYS = 14

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


def normalise_title(value: str) -> str:
    text = clean_text(value).lower()
    return re.sub(r"[^a-z0-9]+", "", text)


def stable_uid(title: str, start: datetime) -> str:
    source = (
        f"{normalise_title(title)}|"
        f"{start.date().isoformat()}|"
        "ovo-hydro"
    )

    digest = hashlib.sha256(
        source.encode("utf-8")
    ).hexdigest()[:28]

    return f"{digest}@ovo-hydro-calendar"


def slugify_title(title: str) -> str:
    text = unicodedata.normalize("NFKD", title)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    text = text.replace("&", " and ")
    text = re.sub(r"['’]", "", text)
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def inferred_event_url(title: str) -> str:
    return (
        f"{HYDRO_BASE_URL}/events/detail/"
        f"{slugify_title(title)}"
    )


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
# DATE REGULAR EXPRESSIONS
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
    rf"\s*[-–—]\s*"
    rf"(?P<end_day>\d{{1,2}})(?:st|nd|rd|th)?\s+"
    rf"(?P<month>{MONTH_PATTERN})"
    rf"\s*(?:/|\s)\s*"
    rf"(?P<year>\d{{2,4}})",
    re.IGNORECASE,
)

CROSS_MONTH_RANGE_RE = re.compile(
    rf"(?:{WEEKDAY_PATTERN}\s+)?"
    rf"(?P<start_day>\d{{1,2}})(?:st|nd|rd|th)?\s+"
    rf"(?P<start_month>{MONTH_PATTERN})"
    rf"\s*[-–—]\s*"
    rf"(?P<end_day>\d{{1,2}})(?:st|nd|rd|th)?\s+"
    rf"(?P<end_month>{MONTH_PATTERN})"
    rf"\s*(?:/|\s)\s*"
    rf"(?P<year>\d{{2,4}})",
    re.IGNORECASE,
)

TWO_DATES_RE = re.compile(
    rf"(?:{WEEKDAY_PATTERN}\s+)?"
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

SHOW_TIME_RE = re.compile(
    r"(?:show\s*time|starts?|performance)\s*:?\s*"
    r"(?P<hour>\d{1,2})"
    r"(?:[.:](?P<minute>\d{2}))?"
    r"\s*(?P<ampm>am|pm)?",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# DATE HELPERS
# ---------------------------------------------------------------------------

def normalise_year(raw_year: str) -> int:
    year = int(raw_year)
    return 2000 + year if year < 100 else year


def month_number(raw_month: str) -> int:
    key = raw_month.lower().rstrip(".")
    if key not in MONTHS:
        raise ValueError(f"Unknown month: {raw_month}")
    return MONTHS[key]


def dates_between(start_date: date, end_date: date) -> list[date]:
    if end_date < start_date:
        raise ValueError(
            f"End date {end_date} is before start date {start_date}."
        )

    number_of_days = (end_date - start_date).days

    return [
        start_date + timedelta(days=offset)
        for offset in range(number_of_days + 1)
    ]


def make_datetime(
    event_date: date,
    start_time: time = DEFAULT_START_TIME,
) -> datetime:
    return datetime.combine(
        event_date,
        start_time,
        tzinfo=UK_TIMEZONE,
    )


def parse_single_date(text: str) -> date | None:
    match = SINGLE_DATE_RE.search(clean_text(text))

    if not match:
        return None

    try:
        return date(
            normalise_year(match.group("year")),
            month_number(match.group("month")),
            int(match.group("day")),
        )
    except ValueError:
        return None


def parse_date_expression(
    date_text: str,
) -> tuple[list[date], bool]:
    text = clean_text(date_text)

    match = TWO_DATES_RE.search(text)

    if match:
        year = normalise_year(match.group("year"))
        month = month_number(match.group("month"))

        return [
            date(year, month, int(match.group("first_day"))),
            date(year, month, int(match.group("second_day"))),
        ], False

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

        span_days = (end_date - start_date).days + 1

        if span_days > MAX_CONTINUOUS_RANGE_DAYS:
            return [], True

        return dates_between(start_date, end_date), False

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

        span_days = (end_date - start_date).days + 1

        if span_days > MAX_CONTINUOUS_RANGE_DAYS:
            return [], True

        return dates_between(start_date, end_date), False

    parsed_date = parse_single_date(text)

    if parsed_date:
        return [parsed_date], False

    return [], False


def parse_show_time(text: str) -> time | None:
    match = SHOW_TIME_RE.search(clean_text(text))

    if not match:
        return None

    hour = int(match.group("hour"))
    minute = int(match.group("minute") or "0")
    ampm = (match.group("ampm") or "").lower()

    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0

    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return None

    return time(hour, minute)


# ---------------------------------------------------------------------------
# JINA READER
# ---------------------------------------------------------------------------

def reader_url(target_url: str) -> str:
    return f"{JINA_READER_BASE_URL}{target_url}"


def download_reader_page(target_url: str) -> str:
    if not JINA_API_KEY:
        raise RuntimeError(
            "The JINA_API_KEY GitHub secret is missing or unavailable."
        )

    headers = {
        "Authorization": f"Bearer {JINA_API_KEY}",
        "Accept": "text/plain",
        "User-Agent": "OVO-Hydro-Calendar/2.0",
        "X-Engine": "browser",
        "X-Proxy": "auto",
        "X-No-Cache": "true",
        "X-Timeout": "60",
    }

    response = requests.get(
        reader_url(target_url),
        headers=headers,
        timeout=90,
    )

    if response.status_code == 401:
        raise RuntimeError(
            "Jina rejected the API key. Check the JINA_API_KEY secret."
        )

    if response.status_code == 403:
        raise RuntimeError(
            f"Jina returned 403 Forbidden while reading {target_url}."
        )

    if response.status_code == 404:
        raise RuntimeError(
            f"The event page could not be found: {target_url}"
        )

    response.raise_for_status()

    text = response.text.strip()

    if not text:
        raise RuntimeError(
            f"Jina returned an empty page for {target_url}."
        )

    return text


# ---------------------------------------------------------------------------
# EVENT DETAIL-PAGE PROCESSING
# ---------------------------------------------------------------------------

def heading_matches_title(line: str, event_title: str) -> bool:
    heading = re.sub(r"^#{1,6}\s*", "", line)
    heading = clean_text(heading)

    markdown_match = MARKDOWN_LINK_RE.match(heading)

    if markdown_match:
        heading = clean_text(markdown_match.group("title"))

    return normalise_title(heading) == normalise_title(event_title)


def find_actual_showings(
    title: str,
    event_url: str,
) -> list[tuple[date, time | None, str]]:
    possible_urls = [event_url]

    inferred_url = inferred_event_url(title)

    if inferred_url not in possible_urls:
        possible_urls.append(inferred_url)

    page_text: str | None = None
    successful_url: str | None = None

    for candidate_url in possible_urls:
        try:
            page_text = download_reader_page(candidate_url)
            successful_url = candidate_url
            break
        except Exception as error:
            print(
                f"Warning: could not read detail page "
                f"{candidate_url}: {error}",
                file=sys.stderr,
            )

    if not page_text:
        return []

    lines = [
        clean_text(line)
        for line in page_text.splitlines()
        if clean_text(line)
    ]

    useful_lines: list[str] = []

    for line in lines[:250]:
        if line.startswith("#") and heading_matches_title(
            line,
            title,
        ):
            break

        useful_lines.append(line)

    showings: list[tuple[date, time | None, str]] = []

    for index, line in enumerate(useful_lines):
        event_date = parse_single_date(line)

        if not event_date:
            continue

        event_time: time | None = parse_show_time(line)

        if event_time is None:
            for following_line in useful_lines[index + 1:index + 4]:
                event_time = parse_show_time(following_line)
                if event_time:
                    break

        showings.append(
            (
                event_date,
                event_time,
                clean_text(line),
            )
        )

    unique: dict[date, tuple[date, time | None, str]] = {}

    for showing in showings:
        event_date, event_time, _source_text = showing
        existing = unique.get(event_date)

        if existing is None:
            unique[event_date] = showing
        elif existing[1] is None and event_time is not None:
            unique[event_date] = showing

    results = sorted(
        unique.values(),
        key=lambda item: item[0],
    )

    if results:
        print(
            f"Found {len(results)} actual performance dates "
            f"for {title} from {successful_url}."
        )

    return results


# ---------------------------------------------------------------------------
# MAIN LISTING EXTRACTION
# ---------------------------------------------------------------------------

def extract_heading_details(
    raw_heading: str,
) -> tuple[str, str]:
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

    return heading, inferred_event_url(heading)


def build_event(
    title: str,
    event_date: date,
    event_url: str,
    source_date_text: str,
    confirmed_time: time | None = None,
) -> HydroEvent:
    start_time = confirmed_time or DEFAULT_START_TIME
    start = make_datetime(event_date, start_time)

    return HydroEvent(
        title=title,
        start=start,
        end=start + DEFAULT_DURATION,
        url=event_url,
        category=classify_event(title),
        date_text=source_date_text,
        time_is_estimated=confirmed_time is None,
    )


def extract_events(page_text: str) -> list[HydroEvent]:
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

        if DATE_LINE_RE.search(line):
            pending_date_lines.append(line)

            if len(pending_date_lines) > 5:
                pending_date_lines = pending_date_lines[-5:]

            continue

        if not line.startswith("### "):
            continue

        title, event_url = extract_heading_details(line)

        if not title:
            pending_date_lines = []
            continue

        if title.lower() in ignored_headings:
            pending_date_lines = []
            continue

        if not pending_date_lines:
            continue

        ordinary_dates: list[tuple[date, str]] = []
        contains_long_summary_range = False

        for date_line in pending_date_lines:
            parsed_dates, is_long_summary = parse_date_expression(
                date_line
            )

            if is_long_summary:
                contains_long_summary_range = True
                continue

            for parsed_date in parsed_dates:
                ordinary_dates.append(
                    (
                        parsed_date,
                        date_line,
                    )
                )

        if contains_long_summary_range:
            actual_showings = find_actual_showings(
                title,
                event_url,
            )

            if actual_showings:
                for (
                    performance_date,
                    performance_time,
                    source_text,
                ) in actual_showings:
                    events.append(
                        build_event(
                            title=title,
                            event_date=performance_date,
                            event_url=event_url,
                            source_date_text=source_text,
                            confirmed_time=performance_time,
                        )
                    )
            else:
                print(
                    f"Warning: {title!r} uses a long summary range, "
                    "but no individual performance dates could be found. "
                    "The summary range has not been added as one giant event.",
                    file=sys.stderr,
                )

        for parsed_date, source_text in ordinary_dates:
            events.append(
                build_event(
                    title=title,
                    event_date=parsed_date,
                    event_url=event_url,
                    source_date_text=source_text,
                )
            )

        pending_date_lines = []

    return deduplicate_events(events)


def deduplicate_events(
    events: list[HydroEvent],
) -> list[HydroEvent]:
    unique: dict[
        tuple[str, date],
        HydroEvent,
    ] = {}

    for event in events:
        key = (
            normalise_title(event.title),
            event.start.date(),
        )

        existing = unique.get(key)

        if existing is None:
            unique[key] = event
            continue

        if existing.time_is_estimated and not event.time_is_estimated:
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

def build_calendar(events: list[HydroEvent]) -> Calendar:
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

        if item.time_is_estimated:
            timing_note = (
                "The event time was not confirmed on the listing, "
                "so 7:30pm has been used as a placeholder."
            )
        else:
            timing_note = (
                "The performance time was taken from the individual "
                "OVO Hydro event page."
            )

        description = (
            f"Listed date: {item.date_text}\n\n"
            f"{timing_note} Check the official event page before "
            "travelling.\n\n"
            f"Official event page: {item.url}"
        )

        calendar_event.add(
            "description",
            description,
        )

        calendar.add_component(calendar_event)

    return calendar


def write_debug_file(events: list[HydroEvent]) -> None:
    payload = [
        {
            "title": item.title,
            "date": item.start.date().isoformat(),
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
# VALIDATION
# ---------------------------------------------------------------------------

def validate_events(events: list[HydroEvent]) -> None:
    if len(events) < 20:
        raise RuntimeError(
            f"Only {len(events)} event entries were detected. "
            "The existing calendar has not been overwritten."
        )

    duplicate_keys: set[tuple[str, date]] = set()
    seen_keys: set[tuple[str, date]] = set()

    for event in events:
        key = (
            normalise_title(event.title),
            event.start.date(),
        )

        if key in seen_keys:
            duplicate_keys.add(key)

        seen_keys.add(key)

    if duplicate_keys:
        raise RuntimeError(
            "Duplicate title/date combinations remain after processing: "
            f"{sorted(duplicate_keys)}"
        )


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main() -> int:
    try:
        page_text = download_reader_page(
            HYDRO_EVENTS_URL
        )

        events = extract_events(page_text)

        validate_events(events)

        OUTPUT_FILE.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        calendar = build_calendar(events)

        OUTPUT_FILE.write_bytes(
            calendar.to_ical()
        )

        write_debug_file(events)

        unique_titles = len(
            {
                normalise_title(event.title)
                for event in events
            }
        )

        print(
            f"Successfully published {len(events)} "
            f"calendar entries across {unique_titles} events."
        )

        return 0

    except requests.RequestException as error:
        print(
            f"Could not download an OVO Hydro page: {error}",
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
