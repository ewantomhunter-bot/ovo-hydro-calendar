from __future__ import annotations

import hashlib
import json
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
READER_URL = f"https://r.jina.ai/{HYDRO_EVENTS_URL}"
HYDRO_BASE_URL = "https://www.ovohydro.com"

OUTPUT_FILE = Path("docs/ovo-hydro.ics")
DEBUG_FILE = Path("docs/events.json")

UK_TIMEZONE = ZoneInfo("Europe/London")
UTC = timezone.utc

VENUE_ADDRESS = "OVO Hydro, Exhibition Way, Glasgow, G3 8YW"

# Used only when the full events listing does not show a precise show time.
DEFAULT_START_TIME = time(19, 30)
DEFAULT_DURATION = timedelta(hours=3)

REQUEST_HEADERS = {
    "Accept": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/150.0 Safari/537.36"
    ),
    # Ask Jina Reader to render JavaScript and avoid using an old cached page.
    "X-Engine": "browser",
    "X-No-Cache": "true",
    "X-Timeout": "30",
}


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
    time_is_estimated: bool


# ---------------------------------------------------------------------------
# TEXT AND URL HELPERS
# ---------------------------------------------------------------------------

def clean_text(value: object) -> str:
    """Collapse repeated spaces and remove surrounding whitespace."""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def absolute_url(value: str) -> str:
    """Convert relative Hydro links into complete URLs."""
    return urljoin(HYDRO_BASE_URL, value)


def stable_uid(title: str, start: datetime) -> str:
    """
    Produce a stable calendar identifier.

    This allows an existing calendar event to update rather than duplicate.
    """
    source = (
        f"{clean_text(title).lower()}|"
        f"{start.date().isoformat()}|ovo-hydro"
    )

    digest = hashlib.sha256(
        source.encode("utf-8")
    ).hexdigest()[:28]

    return f"{digest}@ovo-hydro-calendar"


def classify_event(title: str) -> str:
    """Give each listing a broad calendar category."""
    text = title.lower()

    category_rules = [
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
                "gladiators",
                "giants live",
                "monster jam",
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
                "hot wheels",
                "circus",
                "descendants",
                "zombies",
            ),
        ),
        (
            "Theatre & Dance",
            (
                "musical",
                "theatre",
                "theater",
                "ballet",
                "dance",
                "opera",
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

    for category, phrases in category_rules:
        if any(phrase in text for phrase in phrases):
            return category

    return "Music & Entertainment"


# ---------------------------------------------------------------------------
# DATE PARSING
# ---------------------------------------------------------------------------

MONTH_NUMBERS = {
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

TIME_PATTERN = re.compile(
    r"\b(?P<hour>\d{1,2})"
    r"(?::(?P<minute>\d{2}))?"
    r"\s*(?P<ampm>am|pm)\b",
    re.IGNORECASE,
)

# Examples:
# 17 Aug 2026
# Mon 17 Aug / 26
# 17th August 2026
SINGLE_DATE_PATTERN = re.compile(
    rf"(?:{WEEKDAY_PATTERN}\s+)?"
    rf"(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s+"
    rf"(?P<month>{MONTH_PATTERN})"
    rf"\s*(?:/|\s)\s*"
    rf"(?P<year>\d{{2,4}})",
    re.IGNORECASE,
)

# Examples:
# 25 - 27 Sep 2026
# 25–27 September / 26
SAME_MONTH_RANGE_PATTERN = re.compile(
    rf"(?:{WEEKDAY_PATTERN}\s+)?"
    rf"(?P<start_day>\d{{1,2}})(?:st|nd|rd|th)?"
    rf"\s*[-–]\s*"
    rf"(?P<end_day>\d{{1,2}})(?:st|nd|rd|th)?\s+"
    rf"(?P<month>{MONTH_PATTERN})"
    rf"\s*(?:/|\s)\s*"
    rf"(?P<year>\d{{2,4}})",
    re.IGNORECASE,
)

# Examples:
# 29 Sep - 1 Oct 2026
CROSS_MONTH_RANGE_PATTERN = re.compile(
    rf"(?P<start_day>\d{{1,2}})(?:st|nd|rd|th)?\s+"
    rf"(?P<start_month>{MONTH_PATTERN})"
    rf"\s*[-–]\s*"
    rf"(?P<end_day>\d{{1,2}})(?:st|nd|rd|th)?\s+"
    rf"(?P<end_month>{MONTH_PATTERN})"
    rf"\s*(?:/|\s)\s*"
    rf"(?P<year>\d{{2,4}})",
    re.IGNORECASE,
)

# Examples:
# 19 & 24 Jun 2026
TWO_DATES_PATTERN = re.compile(
    rf"(?P<first_day>\d{{1,2}})(?:st|nd|rd|th)?"
    rf"\s*(?:&|and)\s*"
    rf"(?P<second_day>\d{{1,2}})(?:st|nd|rd|th)?\s+"
    rf"(?P<month>{MONTH_PATTERN})"
    rf"\s*(?:/|\s)\s*"
    rf"(?P<year>\d{{2,4}})",
    re.IGNORECASE,
)


def normalise_year(raw_year: str) -> int:
    year = int(raw_year)

    if year < 100:
        return 2000 + year

    return year


def month_number(raw_month: str) -> int:
    key = raw_month.lower().rstrip(".")

    if key not in MONTH_NUMBERS:
        raise ValueError(f"Unknown month: {raw_month}")

    return MONTH_NUMBERS[key]


def extract_time(text: str) -> tuple[time, bool]:
    """
    Return a published time where one is present.

    Otherwise use 7:30pm and mark it as estimated.
    """
    match = TIME_PATTERN.search(text)

    if match is None:
        return DEFAULT_START_TIME, True

    hour = int(match.group("hour"))
    minute = int(match.group("minute") or 0)
    ampm = match.group("ampm").lower()

    if ampm == "pm" and hour != 12:
        hour += 12

    if ampm == "am" and hour == 12:
        hour = 0

    return time(hour, minute), False


def make_datetime(
    event_date: date,
    event_time: time,
) -> datetime:
    return datetime.combine(
        event_date,
        event_time,
        tzinfo=UK_TIMEZONE,
    )


def parse_dates_from_block(
    block: str,
) -> list[tuple[datetime, datetime, bool]]:
    """
    Extract one or more date ranges from the text surrounding an event link.
    """
    event_time, estimated = extract_time(block)
    results: list[tuple[datetime, datetime, bool]] = []

    # Two separate dates such as "19 & 24 June 2026".
    match = TWO_DATES_PATTERN.search(block)

    if match:
        year = normalise_year(match.group("year"))
        month = month_number(match.group("month"))

        for day_group in ("first_day", "second_day"):
            event_date = date(
                year,
                month,
                int(match.group(day_group)),
            )

            start = make_datetime(event_date, event_time)

            results.append(
                (
                    start,
                    start + DEFAULT_DURATION,
                    estimated,
                )
            )

        return results

    # Date ranges crossing from one month into another.
    match = CROSS_MONTH_RANGE_PATTERN.search(block)

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

        start = make_datetime(start_date, event_time)
        end = (
            make_datetime(end_date, event_time)
            + DEFAULT_DURATION
        )

        return [(start, end, estimated)]

    # Several consecutive days in the same month.
    match = SAME_MONTH_RANGE_PATTERN.search(block)

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

        start = make_datetime(start_date, event_time)
        end = (
            make_datetime(end_date, event_time)
            + DEFAULT_DURATION
        )

        return [(start, end, estimated)]

    # Find every standalone date in the event block.
    for match in SINGLE_DATE_PATTERN.finditer(block):
        year = normalise_year(match.group("year"))
        month = month_number(match.group("month"))
        day = int(match.group("day"))

        try:
            event_date = date(year, month, day)
        except ValueError:
            continue

        start = make_datetime(event_date, event_time)

        results.append(
            (
                start,
                start + DEFAULT_DURATION,
                estimated,
            )
        )

    # Remove repeated dates detected in the same block.
    unique: dict[str, tuple[datetime, datetime, bool]] = {}

    for item in results:
        unique[item[0].isoformat()] = item

    return list(unique.values())


# ---------------------------------------------------------------------------
# JINA READER
# ---------------------------------------------------------------------------

def download_reader_text() -> str:
    """
    Ask Jina Reader to render the complete Hydro events listing.

    JSON mode is preferred, but plain-text output is supported as a fallback.
    """
    response = requests.get(
        READER_URL,
        headers=REQUEST_HEADERS,
        timeout=60,
    )

    response.raise_for_status()

    content_type = response.headers.get(
        "content-type",
        "",
    ).lower()

    if "application/json" in content_type:
        payload = response.json()

        # Jina Reader responses may wrap the converted page in data.content.
        if isinstance(payload, dict):
            data = payload.get("data", payload)

            if isinstance(data, dict):
                for key in (
                    "content",
                    "text",
                    "markdown",
                ):
                    value = data.get(key)

                    if isinstance(value, str) and value.strip():
                        return value

            for key in (
                "content",
                "text",
                "markdown",
            ):
                value = payload.get(key)

                if isinstance(value, str) and value.strip():
                    return value

    text = response.text.strip()

    if not text:
        raise RuntimeError(
            "Jina Reader returned an empty events page."
        )

    return text


# ---------------------------------------------------------------------------
# MARKDOWN EVENT EXTRACTION
# ---------------------------------------------------------------------------

# Matches links such as:
# [Artist Name](https://www.ovohydro.com/events/detail/artist-name)
EVENT_LINK_PATTERN = re.compile(
    r"\[(?P<title>[^\]\n]{2,200})\]"
    r"\("
    r"(?P<url>"
    r"(?:https?://(?:www\.)?ovohydro\.com)?"
    r"/events/(?:detail/)?[^)\s]+"
    r")"
    r"\)",
    re.IGNORECASE,
)

IGNORED_TITLES = {
    "find tickets",
    "buy tickets",
    "more info",
    "read more",
    "view event",
    "events",
    "all events",
    "what's on",
    "ovo hydro",
}


def find_event_block(
    page_text: str,
    link_start: int,
    link_end: int,
) -> str:
    """
    Get the text surrounding an event link.

    Dates on venue listings commonly appear immediately before or after the
    event title, so the parser checks a generous local window.
    """
    before = page_text[
        max(0, link_start - 700):link_start
    ]

    after = page_text[
        link_end:min(len(page_text), link_end + 500)
    ]

    return clean_text(f"{before} {after}")


def extract_events(
    page_text: str,
) -> list[HydroEvent]:
    events: list[HydroEvent] = []

    for link_match in EVENT_LINK_PATTERN.finditer(page_text):
        title = clean_text(link_match.group("title"))

        if title.lower() in IGNORED_TITLES:
            continue

        if title.startswith("Image "):
            continue

        url = absolute_url(link_match.group("url"))
        block = find_event_block(
            page_text,
            link_match.start(),
            link_match.end(),
        )

        parsed_dates = parse_dates_from_block(block)

        if not parsed_dates:
            print(
                f"Warning: no date found for {title!r}",
                file=sys.stderr,
            )
            continue

        for index, (
            start,
            end,
            estimated,
        ) in enumerate(parsed_dates, start=1):

            display_title = title

            if len(parsed_dates) > 1:
                display_title = (
                    f"{title} — Date {index}"
                )

            events.append(
                HydroEvent(
                    title=display_title,
                    start=start,
                    end=end,
                    url=url,
                    category=classify_event(title),
                    time_is_estimated=estimated,
                )
            )

    return deduplicate_events(events)


def deduplicate_events(
    events: list[HydroEvent],
) -> list[HydroEvent]:
    """Remove repeated links and repeated event cards."""
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

        existing = unique.get(key)

        if existing is None:
            unique[key] = event
            continue

        # Prefer a confirmed time over a placeholder.
        if (
            existing.time_is_estimated
            and not event.time_is_estimated
        ):
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

        description_lines: list[str] = []

        if item.time_is_estimated:
            description_lines.append(
                "The full venue listing did not provide a precise "
                "performance time. A temporary start time of 7:30pm "
                "has been used. Check the official event page before "
                "travelling."
            )

        description_lines.append(
            f"Official event page: {item.url}"
        )

        calendar_event.add(
            "description",
            "\n\n".join(description_lines),
        )

        calendar.add_component(calendar_event)

    return calendar


def write_debug_file(
    events: list[HydroEvent],
) -> None:
    debug_data = [
        {
            "title": item.title,
            "start": item.start.isoformat(),
            "end": item.end.isoformat(),
            "category": item.category,
            "url": item.url,
            "time_is_estimated": item.time_is_estimated,
        }
        for item in events
    ]

    DEBUG_FILE.write_text(
        json.dumps(
            debug_data,
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
        page_text = download_reader_text()
        events = extract_events(page_text)

        # Protect the existing calendar from being overwritten if the source
        # or page format temporarily breaks.
        if len(events) < 10:
            raise RuntimeError(
                f"Only {len(events)} event entries were detected. "
                "The existing calendar has not been replaced."
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
