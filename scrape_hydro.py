from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from dateutil import parser as date_parser
from icalendar import Calendar, Event, vText
from zoneinfo import ZoneInfo

EVENTS_URLS = [
    "https://www.ovohydro.com/events",
    "https://www.ovohydro.com/events/all",
]
BASE_URL = "https://www.ovohydro.com"
OUTPUT = Path("docs/ovo-hydro.ics")
DEBUG_OUTPUT = Path("docs/events.json")
GLASGOW = ZoneInfo("Europe/London")
UTC = timezone.utc
VENUE = "OVO Hydro, Exhibition Way, Glasgow, G3 8YW"
DEFAULT_START_TIME = time(19, 30)
DEFAULT_DURATION = timedelta(hours=3)
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; OVOHydroCalendar/2.0; "
        "+https://github.com/ewantomhunter-bot/ovo-hydro-calendar)"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
}

@dataclass(frozen=True)
class HydroEvent:
    title: str
    start: datetime | date
    end: datetime | date
    url: str
    category: str
    description: str = ""
    time_is_estimated: bool = False


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def absolute_url(value: str | None) -> str:
    return urljoin(BASE_URL, value or EVENTS_URLS[0])


def stable_uid(title: str, start: datetime | date) -> str:
    raw = f"{clean(title).lower()}|{start.isoformat()}|ovo-hydro".encode()
    return hashlib.sha256(raw).hexdigest()[:28] + "@ovo-hydro-calendar"


def classify(title: str, description: str = "") -> str:
    text = f"{title} {description}".lower()
    rules = [
        ("Comedy", ("comedy", "comedian", "stand-up", "stand up", "laugh")),
        ("Sport", ("boxing", "wrestling", "ufc", "mma", "basketball", "netball", "ice hockey", "hockey", "darts", "sport", "football")),
        ("Family", ("family", "kids", "children", "disney", "paw patrol", "circus", "monster jam", "bluey", "harlem globetrotters")),
        ("Theatre & Dance", ("ballet", "dance", "musical", "theatre", "theater", "opera")),
        ("Music", ("tour", "live", "concert", "band", "singer", "orchestra", "tribute", "dj")),
    ]
    for category, words in rules:
        if any(word in text for word in words):
            return category
    return "Entertainment"


def parse_datetime_value(value: Any) -> datetime | date | None:
    if not value:
        return None
    try:
        parsed = date_parser.parse(clean(value), dayfirst=True)
    except (ValueError, TypeError, OverflowError):
        return None
    raw = clean(value)
    has_time = bool(re.search(r"\b\d{1,2}:\d{2}\b|\b(?:am|pm)\b|T\d{2}", raw, re.I))
    if not has_time:
        return parsed.date()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=GLASGOW)
    else:
        parsed = parsed.astimezone(GLASGOW)
    return parsed


def walk_json(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_json(child)


def is_event_object(obj: dict[str, Any]) -> bool:
    object_type = obj.get("@type") or obj.get("type")
    if isinstance(object_type, list):
        return "Event" in object_type
    return clean(object_type).lower() == "event"


def event_from_mapping(obj: dict[str, Any]) -> HydroEvent | None:
    title = clean(obj.get("name") or obj.get("title") or obj.get("eventName") or obj.get("headline"))
    start_raw = obj.get("startDate") or obj.get("start_date") or obj.get("date") or obj.get("eventDate")
    if not title or not start_raw:
        return None
    start = parse_datetime_value(start_raw)
    if start is None:
        return None
    end_raw = obj.get("endDate") or obj.get("end_date")
    end = parse_datetime_value(end_raw)
    estimated = isinstance(start, date) and not isinstance(start, datetime)
    if estimated:
        start = datetime.combine(start, DEFAULT_START_TIME, tzinfo=GLASGOW)
        end = start + DEFAULT_DURATION
    elif end is None:
        end = start + DEFAULT_DURATION
    description = clean(obj.get("description") or obj.get("summary") or obj.get("shortDescription"))
    url_value = obj.get("url")
    if isinstance(url_value, dict):
        url_value = url_value.get("@id") or url_value.get("url")
    return HydroEvent(title, start, end, absolute_url(clean(url_value)), classify(title, description), description, estimated)


def extract_structured_events(soup: BeautifulSoup) -> list[HydroEvent]:
    events: list[HydroEvent] = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        for obj in walk_json(payload):
            if is_event_object(obj):
                event = event_from_mapping(obj)
                if event:
                    events.append(event)
    for script in soup.select('script[type="application/json"], script#__NEXT_DATA__'):
        raw = script.get_text()
        if not raw or len(raw) > 10_000_000:
            continue
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        for obj in walk_json(payload):
            keys = {str(key).lower() for key in obj}
            looks_like_event = any(k in keys for k in ("startdate", "start_date", "eventdate")) and any(k in keys for k in ("name", "title", "eventname"))
            if looks_like_event:
                event = event_from_mapping(obj)
                if event:
                    events.append(event)
    return events

DATE_RE = re.compile(
    r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)?\s*(\d{1,2}(?:st|nd|rd|th)?\s+(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)(?:\s+\d{2,4})?)\b",
    re.I,
)
TIME_RE = re.compile(r"\b(\d{1,2}(?::\d{2})?\s*(?:am|pm))\b", re.I)


def infer_year(month: int, day: int) -> int:
    today = datetime.now(GLASGOW).date()
    candidate = date(today.year, month, day)
    return today.year + 1 if candidate < today - timedelta(days=45) else today.year


def parse_card_date(text: str) -> tuple[datetime, bool] | None:
    date_match = DATE_RE.search(text)
    if not date_match:
        return None
    date_text = re.sub(r"(\d)(st|nd|rd|th)", r"\1", date_match.group(1), flags=re.I)
    try:
        parsed = date_parser.parse(date_text, dayfirst=True, fuzzy=True)
    except (ValueError, TypeError):
        return None
    year_present = bool(re.search(r"\b\d{2,4}\b", date_text))
    year = parsed.year if year_present else infer_year(parsed.month, parsed.day)
    event_date = date(year, parsed.month, parsed.day)
    time_match = TIME_RE.search(text)
    if time_match:
        parsed_time = date_parser.parse(time_match.group(1)).time().replace(second=0, microsecond=0)
        return datetime.combine(event_date, parsed_time, tzinfo=GLASGOW), False
    return datetime.combine(event_date, DEFAULT_START_TIME, tzinfo=GLASGOW), True


def extract_visible_cards(soup: BeautifulSoup) -> list[HydroEvent]:
    events: list[HydroEvent] = []
    seen_containers: set[int] = set()
    for node in soup.find_all(["h2", "h3", "h4", "a"]):
        title = clean(node.get_text(" ", strip=True))
        if len(title) < 3 or len(title) > 180:
            continue
        container = node
        for _ in range(6):
            parent = container.parent
            if parent is None:
                break
            container = parent
            card_text = clean(container.get_text(" ", strip=True))
            if DATE_RE.search(card_text) and len(card_text) < 1800:
                break
        identity = id(container)
        if identity in seen_containers:
            continue
        card_text = clean(container.get_text(" ", strip=True))
        parsed = parse_card_date(card_text)
        if not parsed:
            continue
        start, estimated = parsed
        link = node if node.name == "a" and node.get("href") else container.find("a", href=True)
        url = absolute_url(link.get("href") if link else None)
        if title.lower() in {"events", "what's on", "view all", "find tickets", "buy tickets", "more info", "read more", "ovo hydro"}:
            continue
        events.append(HydroEvent(title, start, start + DEFAULT_DURATION, url, classify(title, card_text), "", estimated))
        seen_containers.add(identity)
    return events


def deduplicate(events: Iterable[HydroEvent]) -> list[HydroEvent]:
    unique: dict[tuple[str, str], HydroEvent] = {}
    for event in events:
        key = (re.sub(r"[^a-z0-9]+", "", event.title.lower()), event.start.date().isoformat())
        current = unique.get(key)
        if current is None:
            unique[key] = event
            continue
        score = (not event.time_is_estimated, len(event.description), len(event.url))
        current_score = (not current.time_is_estimated, len(current.description), len(current.url))
        if score > current_score:
            unique[key] = event
    return sorted(unique.values(), key=lambda item: item.start)


def fetch_events() -> list[HydroEvent]:
    last_error: Exception | None = None
    for url in EVENTS_URLS:
        try:
            response = requests.get(url, headers=HEADERS, timeout=35)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            events = extract_structured_events(soup)
            events.extend(extract_visible_cards(soup))
            events = deduplicate(events)
            if len(events) >= 3:
                return events
        except Exception as exc:
            last_error = exc
    message = "No usable event list was found."
    if last_error:
        message += f" Last error: {last_error}"
    raise RuntimeError(message)


def make_calendar(events: list[HydroEvent]) -> Calendar:
    calendar = Calendar()
    calendar.add("prodid", "-//Ewan Hunter//OVO Hydro Calendar//EN")
    calendar.add("version", "2.0")
    calendar.add("calscale", "GREGORIAN")
    calendar.add("method", "PUBLISH")
    calendar.add("x-wr-calname", "OVO Hydro Events")
    calendar.add("x-wr-timezone", "Europe/London")
    calendar.add("refresh-interval;value=duration", "PT12H")
    calendar.add("x-published-ttl", "PT12H")
    generated = datetime.now(UTC)
    for item in events:
        event = Event()
        event.add("uid", stable_uid(item.title, item.start))
        event.add("dtstamp", generated)
        event.add("last-modified", generated)
        event.add("summary", item.title)
        event.add("dtstart", item.start)
        event.add("dtend", item.end)
        event["location"] = vText(VENUE)
        event.add("url", item.url)
        event.add("categories", ["OVO Hydro", item.category])
        lines = []
        if item.description:
            lines.append(item.description)
        if item.time_is_estimated:
            lines.append("The calendar time is a placeholder because a precise performance time was not available in the venue listing. Check the official event page before travelling.")
        lines.append(f"Official event page: {item.url}")
        event.add("description", "\n\n".join(lines))
        calendar.add_component(event)
    return calendar


def write_debug_json(events: list[HydroEvent]) -> None:
    payload = [{"title": item.title, "start": item.start.isoformat(), "end": item.end.isoformat(), "category": item.category, "url": item.url, "time_is_estimated": item.time_is_estimated} for item in events]
    DEBUG_OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    try:
        events = fetch_events()
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_bytes(make_calendar(events).to_ical())
        write_debug_json(events)
        print(f"Published {len(events)} OVO Hydro events.")
        return 0
    except Exception as exc:
        print(f"Calendar update failed: {exc}", file=sys.stderr)
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
