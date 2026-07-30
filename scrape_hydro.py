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
from bs4 import BeautifulSoup, Tag
from dateutil import parser as date_parser
from icalendar import Calendar, Event, vText
from zoneinfo import ZoneInfo


# ---------------------------------------------------------------------------
# SETTINGS
# ---------------------------------------------------------------------------

EVENTS_URL = "https://www.ovohydro.com/events/all"
BASE_URL = "https://www.ovohydro.com"

OUTPUT_FILE = Path("docs/ovo-hydro.ics")
DEBUG_FILE = Path("docs/events.json")

TIMEZONE = ZoneInfo("Europe/London")
UTC = timezone.utc

VENUE_NAME = "OVO Hydro"
VENUE_ADDRESS = "OVO Hydro, Exhibition Way, Glasgow, G3 8YW"

# The event-listing page usually shows dates but not performance times.
# When no precise time is published, the calendar uses 7:30pm.
DEFAULT_START_TIME = time(19, 30)
DEFAULT_EVENT_DURATION = timedelta(hours=3)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/150.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
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
    subtitle: str = ""
    category: str = "Entertainment"
    time_is_estimated: bool = True


# ---------------------------------------------------------------------------
# TEXT HELPERS
# ---------------------------------------------------------------------------

def clean_text(value: object) -> str:
    """Collapse repeated whitespace and remove leading/trailing spaces."""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def make_absolute_url(value: str | None) -> str:
    """Convert an event link into a complete URL."""
    return urljoin(BASE_URL, value or EVENTS_URL)


def make_uid(title: str, start: datetime) -> str:
    """
    Create a stable calendar UID.

    Stable UIDs mean calendar events update instead of being duplicated.
    """
    source = f"{title.lower()}|{start.date().isoformat()}|ovo-hydro"
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:28]
    return f"{digest}@ovo-hydro-calendar"


def classify_event(title: str, subtitle: str = "") -> str:
    """Assign a broad category based on the event title and subtitle."""
    text = f"{title} {subtitle}".lower()

    categories = [
        (
            "Comedy",
            (
                "comedy",
                "comedian",
                "stand-up",
                "stand up",
                "kevin bridges",
                "jack whitehall",
                "paul smith",
                "john bishop",
                "peter kay",
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
                "sport",
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
                "circus",
                "monster jam",
                "hot wheels",
                "descendants",
                "zombies",
                "camp rock",
            ),
        ),
        (
            "Theatre & Dance",
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
        (
            "Music",
            (
                "tour",
                "concert",
                "live",
                "band",
                "singer",
                "special guest",
                "album",
                "world tour",
            ),
        ),
    ]

    for category, keywords in categories:
        if any(keyword in text for keyword in keywords):
            return category

    return "Entertainment"


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

WEEKDAY_PATTERN = (
    r"(?:Mon(?:day)?|Tue(?:sday)?|Wed(?:nesday)?|Thu(?:rsday)?|"
    r"Fri(?:day)?|Sat(?:urday)?|Sun(?:day)?)"
)

MONTH_PATTERN = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
    r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|"
    r"Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
)

DATE_FRAGMENT_PATTERN = re.compile(
    rf"(?:{WEEKDAY_PATTERN}\s+)?"
    rf"\d{{1,2}}(?:st|nd|rd|th)?"
    rf"(?:\s*[-–&]\s*\d{{1,2}}(?:st|nd|rd|th)?)?"
    rf"\s+{MONTH_PATTERN}"
    rf"(?:\s*(?:/|\s)\s*\d{{2,4}})?",
    re.IGNORECASE,
)

FULL_RANGE_PATTERN = re.compile(
    rf"(?:(?:{WEEKDAY_PATTERN})\s+)?"
    rf"(?P<start_day>\d{{1,2}})(?:st|nd|rd|th)?"
    rf"\s*[-–]\s*"
    rf"(?P<end_day>\d{{1,2}})(?:st|nd|rd|th)?"
    rf"\s+"
    rf"(?P<month>{MONTH_PATTERN})"
    rf"\s*(?:/|\s)\s*(?P<year>\d{{2,4}})",
    re.IGNORECASE,
)

SINGLE_DATE_PATTERN = re.compile(
    rf"(?:(?:{WEEKDAY_PATTERN})\s+)?"
    rf"(?P<day>\d{{1,2}})(?:st|nd|rd|th)?"
    rf"\s+"
    rf"(?P<month>{MONTH_PATTERN})"
    rf"\s*(?:/|\s)\s*(?P<year>\d{{2,4}})",
    re.IGNORECASE,
)

AMPERSAND_DATES_PATTERN = re.compile(
    rf"(?P<day1>\d{{1,2}})(?:st|nd|rd|th)?"
    rf"\s*&\s*"
    rf"(?P<day2>\d{{1,2}})(?:st|nd|rd|th)?"
    rf"\s+"
    rf"(?P<month>{MONTH_PATTERN})"
    rf"\s*(?:/|\s)\s*(?P<year>\d{{2,4}})",
    re.IGNORECASE,
)

TIME_PATTERN = re.compile(
    r"\b(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>am|pm)\b",
    re.IGNORECASE,
)


def normalise_year(raw_year: str) -> int:
    year = int(raw_year)
    if year < 100:
        return 2000 + year
    return year


def month_number(raw_month: str) -> int:
    key = raw_month.lower().strip(".")
    if key not in MONTHS:
        raise ValueError(f"Unrecognised month: {raw_month}")
    return MONTHS[key]


def extract_event_time(text: str) -> tuple[time, bool]:
    """
    Return the published performance time where present.

    Otherwise return the configured 7:30pm placeholder.
    """
    match = TIME_PATTERN.search(text)

    if not match:
        return DEFAULT_START_TIME, True

    hour = int(match.group("hour"))
    minute = int(match.group("minute") or 0)
    ampm = match.group("ampm").lower()

    if ampm == "pm" and hour != 12:
        hour += 12

    if ampm == "am" and hour == 12:
        hour = 0

    return time(hour, minute), False


def parse_date_text(
    date_text: str,
    surrounding_text: str,
) -> list[tuple[datetime, datetime, bool]]:
    """
    Parse the date text from one event card.

    Returns one or more start/end pairs because some listings contain two
    separate performance runs.
    """
    cleaned = clean_text(date_text)
    event_time, estimated = extract_event_time(surrounding_text)

    results: list[tuple[datetime, datetime, bool]] = []

    # Example: "19 & 24 Jun 2026"
    ampersand_match = AMPERSAND_DATES_PATTERN.search(cleaned)
    if ampersand_match:
        year = normalise_year(ampersand_match.group("year"))
        month = month_number(ampersand_match.group("month"))

        for day_name in ("day1", "day2"):
            day = int(ampersand_match.group(day_name))
            start = datetime.combine(
                date(year, month, day),
                event_time,
                tzinfo=TIMEZONE,
            )
            results.append(
                (start, start + DEFAULT_EVENT_DURATION, estimated)
            )

        return results

    # Example: "11 Sep - 26 Nov / 26"
    cross_month = re.search(
        rf"(?P<start_day>\d{{1,2}})(?:st|nd|rd|th)?\s+"
        rf"(?P<start_month>{MONTH_PATTERN})\s*[-–]\s*"
        rf"(?P<end_day>\d{{1,2}})(?:st|nd|rd|th)?\s+"
        rf"(?P<end_month>{MONTH_PATTERN})\s*"
        rf"(?:/|\s)\s*(?P<year>\d{{2,4}})",
        cleaned,
        re.IGNORECASE,
    )

    if cross_month:
        year = normalise_year(cross_month.group("year"))

        start_date = date(
            year,
            month_number(cross_month.group("start_month")),
            int(cross_month.group("start_day")),
        )

        end_date = date(
            year,
            month_number(cross_month.group("end_month")),
            int(cross_month.group("end_day")),
        )

        start = datetime.combine(start_date, event_time, tzinfo=TIMEZONE)
        end = datetime.combine(
            end_date,
            event_time,
            tzinfo=TIMEZONE,
        ) + DEFAULT_EVENT_DURATION

        return [(start, end, estimated)]

    # Example: "25 - 27 Sep 2026"
    range_match = FULL_RANGE_PATTERN.search(cleaned)
    if range_match:
        year = normalise_year(range_match.group("year"))
        month = month_number(range_match.group("month"))

        start_date = date(
            year,
            month,
            int(range_match.group("start_day")),
        )

        end_date = date(
            year,
            month,
            int(range_match.group("end_day")),
        )

        start = datetime.combine(start_date, event_time, tzinfo=TIMEZONE)
        end = datetime.combine(
            end_date,
            event_time,
            tzinfo=TIMEZONE,
        ) + DEFAULT_EVENT_DURATION

        results.append((start, end, estimated))

        # Some listings contain a second separate run, for example:
        # "25 - 27 Sep 2026 09 - 11 Oct 2026"
        remaining_text = cleaned[range_match.end():]
        second_range = FULL_RANGE_PATTERN.search(remaining_text)

        if second_range:
            second_year = normalise_year(second_range.group("year"))
            second_month = month_number(second_range.group("month"))

            second_start_date = date(
                second_year,
                second_month,
                int(second_range.group("start_day")),
            )

            second_end_date = date(
                second_year,
                second_month,
                int(second_range.group("end_day")),
            )

            second_start = datetime.combine(
                second_start_date,
                event_time,
                tzinfo=TIMEZONE,
            )

            second_end = datetime.combine(
                second_end_date,
                event_time,
                tzinfo=TIMEZONE,
            ) + DEFAULT_EVENT_DURATION

            results.append((second_start, second_end, estimated))

        return results

    # Example: "Mon 17 Aug / 26"
    single_match = SINGLE_DATE_PATTERN.search(cleaned)
    if single_match:
        year = normalise_year(single_match.group("year"))
        month = month_number(single_match.group("month"))
        day = int(single_match.group("day"))

        start = datetime.combine(
            date(year, month, day),
            event_time,
            tzinfo=TIMEZONE,
        )

        return [(start, start + DEFAULT_EVENT_DURATION, estimated)]

    # Final fallback through dateutil.
    try:
        parsed = date_parser.parse(cleaned, dayfirst=True, fuzzy=True)

        start = datetime.combine(
            parsed.date(),
            event_time,
            tzinfo=TIMEZONE,
        )

        return [(start, start + DEFAULT_EVENT_DURATION, estimated)]

    except (ValueError, TypeError, OverflowError):
        return []


# ---------------------------------------------------------------------------
# HTML PARSING
# ---------------------------------------------------------------------------

def find_event_container(heading: Tag) -> Tag | None:
    """
    Walk upwards from an H3 heading until a reasonably sized event card
    containing both a date and the heading is found.
    """
    current: Tag | None = heading

    for _ in range(8):
        if current is None:
            return None

        text = clean_text(current.get_text(" ", strip=True))

        if (
            DATE_FRAGMENT_PATTERN.search(text)
            and heading.get_text(" ", strip=True) in text
            and len(text) <= 2500
        ):
            return current

        parent = current.parent
        current = parent if isinstance(parent, Tag) else None

    return None


def extract_date_text(container: Tag, title: str) -> str:
    """
    Find date-like text inside an event card.

    Date text normally appears before the event H3 heading.
    """
    strings = [
        clean_text(value)
        for value in container.stripped_strings
        if clean_text(value)
    ]

    title_index = None

    for index, value in enumerate(strings):
        if value == title:
            title_index = index
            break

    search_values = (
        strings[:title_index]
        if title_index is not None
        else strings
    )

    # Work backwards because the date usually sits immediately above the title.
    candidates: list[str] = []

    for value in reversed(search_values):
        if DATE_FRAGMENT_PATTERN.search(value):
            candidates.insert(0, value)

            # Capture at most two date lines for multi-run listings.
            if len(candidates) == 2:
                break

    if candidates:
        return " ".join(candidates)

    container_text = clean_text(container.get_text(" ", strip=True))
    match = DATE_FRAGMENT_PATTERN.search(container_text)

    return match.group(0) if match else ""


def extract_subtitle(heading: Tag, container: Tag) -> str:
    """
    Find an H4 subtitle associated with the event card.
    """
    subtitle_heading = container.find("h4")

    if subtitle_heading:
        subtitle = clean_text(subtitle_heading.get_text(" ", strip=True))

        if subtitle and subtitle.lower() != heading.get_text(
            " ",
            strip=True,
        ).lower():
            return subtitle

    return ""


def extract_event_url(heading: Tag, container: Tag) -> str:
    """Find the event-detail URL attached to a card."""
    heading_link = heading.find_parent("a", href=True)

    if heading_link:
        return make_absolute_url(heading_link.get("href"))

    links = container.find_all("a", href=True)

    for link in links:
        href = clean_text(link.get("href"))

        if "/events/detail/" in href:
            return make_absolute_url(href)

    return EVENTS_URL


def extract_events(soup: BeautifulSoup) -> list[HydroEvent]:
    events: list[HydroEvent] = []

    # Event names on the Hydro listing are displayed as H3 headings.
    for heading in soup.find_all("h3"):
        title = clean_text(heading.get_text(" ", strip=True))

        if not title:
            continue

        if title.lower() in {
            "what's on",
            "upcoming events",
            "venue premium seating",
            "don't miss out",
        }:
            continue

        container = find_event_container(heading)

        if container is None:
            continue

        date_text = extract_date_text(container, title)

        if not date_text:
            continue

        card_text = clean_text(container.get_text(" ", strip=True))
        parsed_dates = parse_date_text(date_text, card_text)

        if not parsed_dates:
            print(
                f"Warning: could not parse date for {title!r}: "
                f"{date_text!r}",
                file=sys.stderr,
            )
            continue

        subtitle = extract_subtitle(heading, container)
        event_url = extract_event_url(heading, container)
        category = classify_event(title, subtitle)

        for number, (start, end, estimated) in enumerate(
            parsed_dates,
            start=1,
        ):
            display_title = title

            if len(parsed_dates) > 1:
                display_title = f"{title} — Run {number}"

            events.append(
                HydroEvent(
                    title=display_title,
                    start=start,
                    end=end,
                    url=event_url,
                    subtitle=subtitle,
                    category=category,
                    time_is_estimated=estimated,
                )
            )

    return deduplicate_events(events)


def deduplicate_events(
    events: list[HydroEvent],
) -> list[HydroEvent]:
    """Remove repeated cards while keeping separate event dates."""
    unique: dict[tuple[str, str], HydroEvent] = {}

    for event in events:
        key = (
            re.sub(r"[^a-z0-9]+", "", event.title.lower()),
            event.start.isoformat(),
        )

        existing = unique.get(key)

        if existing is None:
            unique[key] = event
            continue

        # Prefer whichever duplicate contains more information.
        existing_score = (
            len(existing.subtitle),
            len(existing.url),
            not existing.time_is_estimated,
        )

        new_score = (
            len(event.subtitle),
            len(event.url),
            not event.time_is_estimated,
        )

        if new_score > existing_score:
            unique[key] = event

    return sorted(
        unique.values(),
        key=lambda item: (item.start, item.title.lower()),
    )


# ---------------------------------------------------------------------------
# FETCHING
# ---------------------------------------------------------------------------

def fetch_events() -> list[HydroEvent]:
    response = requests.get(
        EVENTS_URL,
        headers=HEADERS,
        timeout=45,
    )

    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    events = extract_events(soup)

    # Prevent a temporary website problem from replacing a healthy calendar
    # with an incomplete or empty feed.
    if len(events) < 20:
        raise RuntimeError(
            f"Only {len(events)} events were detected. "
            "The OVO Hydro website layout may have changed, so the existing "
            "calendar has not been overwritten."
        )

    return events


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
    calendar.add("x-wr-calname", "OVO Hydro Events")
    calendar.add("x-wr-timezone", "Europe/London")
    calendar.add("refresh-interval;value=duration", "PT12H")
    calendar.add("x-published-ttl", "PT12H")

    generated_at = datetime.now(UTC)

    for item in events:
        calendar_event = Event()

        calendar_event.add(
            "uid",
            make_uid(item.title, item.start),
        )
        calendar_event.add("dtstamp", generated_at)
        calendar_event.add("last-modified", generated_at)
        calendar_event.add("summary", item.title)
        calendar_event.add("dtstart", item.start)
        calendar_event.add("dtend", item.end)
        calendar_event.add("url", item.url)

        calendar_event["location"] = vText(VENUE_ADDRESS)

        calendar_event.add(
            "categories",
            [VENUE_NAME, item.category],
        )

        description_parts: list[str] = []

        if item.subtitle:
            description_parts.append(item.subtitle)

        if item.time_is_estimated:
            description_parts.append(
                "The OVO Hydro event-list page did not provide a precise "
                "performance time. A temporary start time of 7:30pm has been "
                "used. Check the official event page before travelling."
            )

        description_parts.append(
            f"Official event page: {item.url}"
        )

        calendar_event.add(
            "description",
            "\n\n".join(description_parts),
        )

        calendar.add_component(calendar_event)

    return calendar


def write_debug_file(events: list[HydroEvent]) -> None:
    data = [
        {
            "title": event.title,
            "subtitle": event.subtitle,
            "start": event.start.isoformat(),
            "end": event.end.isoformat(),
            "category": event.category,
            "url": event.url,
            "time_is_estimated": event.time_is_estimated,
        }
        for event in events
    ]

    DEBUG_FILE.write_text(
        json.dumps(
            data,
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
        events = fetch_events()

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
            f"Could not download the OVO Hydro events page: {error}",
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
