"""Publication-date-aware arXiv listing selection.

The arXiv RSS feed only exposes the *current* announcement. A PaperFlow run is
labelled with a publication date, but GitHub may deliver the cron trigger many
hours early or late, so the live RSS listing can belong to a different
announcement. Before this guard existed, such runs silently published an empty
listing (Nov 2 and Nov 11, 2026) or re-read the previous day's listing (Nov 5).

Rules, for a publication date D whose announcement is made on the evening of D:

* RSS dated D + 1 is the matching listing and is used as-is.
* RSS dated earlier than D + 1 is not published yet: the run is skipped so a
  later trigger picks the date up (``ArxivListingNotReady``).
* RSS dated later than D + 1 means D's listing has already rotated out, so the
  listing is rebuilt from the arXiv API using D's submission window.

Fridays and Saturdays have no arXiv announcement; those dates keep the legacy
behaviour because there is no listing to protect.
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock_time
from email.utils import parsedate_to_datetime
from enum import StrEnum
from urllib.parse import quote
from zoneinfo import ZoneInfo

from paperflow.arxiv_client import (
    ArxivClient,
    ArxivSourceError,
    HttpTransport,
    parse_arxiv_atom_candidate,
)
from paperflow.models import AnnounceType, RawArxivEntry
from paperflow.normalize import normalize_arxiv_id

ARXIV_TIMEZONE = ZoneInfo("America/New_York")
SUBMISSION_CUTOFF = clock_time(14, 0)
_ATOM = "{http://www.w3.org/2005/Atom}"
_ARXIV = "{http://arxiv.org/schemas/atom}"
_OPENSEARCH = "{http://a9.com/-/spec/opensearch/1.1/}"


class ArxivListingNotReady(ArxivSourceError):
    """The live RSS listing for the publication date has not been published yet."""


class ListingMatch(StrEnum):
    MATCH = "match"
    NOT_READY = "not_ready"
    SUPERSEDED = "superseded"
    UNCHECKED = "unchecked"


def is_announcement_date(publication_date: date) -> bool:
    """arXiv announces on Sunday through Thursday evenings (Eastern time)."""
    return publication_date.weekday() in {6, 0, 1, 2, 3}


def rss_listing_date(payload: bytes) -> date | None:
    """Return the Eastern-time date of an RSS channel, if it declares one."""
    channel = ET.fromstring(payload).find("channel")
    if channel is None:
        return None
    for tag in ("pubDate", "lastBuildDate"):
        value = channel.findtext(tag)
        if value and value.strip():
            try:
                parsed = parsedate_to_datetime(value.strip())
            except (TypeError, ValueError):
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.astimezone(ARXIV_TIMEZONE).date()
    return None


def classify_listing(
    listing_date: date | None, *, publication_date: date, item_count: int
) -> ListingMatch:
    """Decide whether a live RSS listing belongs to ``publication_date``."""
    if not is_announcement_date(publication_date) or listing_date is None:
        return ListingMatch.UNCHECKED
    expected = publication_date + timedelta(days=1)
    if listing_date < expected:
        return ListingMatch.NOT_READY
    if listing_date > expected:
        return ListingMatch.SUPERSEDED
    if item_count == 0:
        # An announcement day with an empty listing is a feed hiccup, not news.
        return ListingMatch.NOT_READY
    return ListingMatch.MATCH


def submission_window(publication_date: date) -> tuple[datetime, datetime] | None:
    """Return the UTC submission window announced on ``publication_date``.

    Sunday's announcement covers Thursday 14:00 to Friday 14:00 ET; Monday's
    covers Friday 14:00 to Monday 14:00; Tuesday-Thursday cover the previous
    weekday's cutoff to that day's cutoff. arXiv holidays are not modelled.
    """
    if not is_announcement_date(publication_date):
        return None
    end_day = publication_date
    if end_day.weekday() == 6:  # Sunday -> Friday cutoff
        end_day -= timedelta(days=2)
    start_day = end_day - timedelta(days=1)
    while start_day.weekday() >= 5:
        start_day -= timedelta(days=1)
    start = datetime.combine(start_day, SUBMISSION_CUTOFF, ARXIV_TIMEZONE)
    end = datetime.combine(end_day, SUBMISSION_CUTOFF, ARXIV_TIMEZONE)
    return start.astimezone(UTC), end.astimezone(UTC)


class ArxivApiListingClient:
    """Rebuild one announcement's new/cross entries from the arXiv Atom API."""

    def __init__(
        self,
        transport: HttpTransport,
        *,
        page_size: int = 500,
        pause_seconds: float = 3.1,
        sleep: Callable[[float], None] = time.sleep,
        max_pages: int = 40,
    ) -> None:
        self._transport = transport
        self._page_size = page_size
        self._pause_seconds = pause_seconds
        self._sleep = sleep
        self._max_pages = max_pages

    def fetch_publication(
        self,
        categories: Sequence[str],
        *,
        publication_date: date,
        timeout_seconds: float,
    ) -> list[RawArxivEntry]:
        window = submission_window(publication_date)
        if window is None:
            return []
        start, end = window
        query = (
            "("
            + " OR ".join(f"cat:{category}" for category in categories)
            + f") AND submittedDate:[{start:%Y%m%d%H%M} TO {end:%Y%m%d%H%M}]"
        )
        configured = set(categories)
        entries: list[RawArxivEntry] = []
        seen: set[str] = set()
        offset = 0
        total: int | None = None
        empty_retries = 0
        for page in range(self._max_pages):
            if page:
                self._sleep(self._pause_seconds)
            url = (
                "https://export.arxiv.org/api/query?search_query="
                + quote(query, safe="")
                + f"&start={offset}&max_results={self._page_size}"
                + "&sortBy=submittedDate&sortOrder=ascending"
            )
            payload = self._get_with_retry(url, timeout_seconds)
            try:
                feed = ET.fromstring(payload)
            except ET.ParseError as error:
                raise ArxivSourceError("arXiv API response was invalid") from error
            if total is None:
                total_text = feed.findtext(f"{_OPENSEARCH}totalResults")
                total = int(total_text) if total_text and total_text.isdigit() else None
            page_entries = feed.findall(f"{_ATOM}entry")
            if not page_entries and total is not None and offset < total:
                # The API intermittently returns empty pages mid-listing.
                empty_retries += 1
                if empty_retries > 3:
                    break
                continue
            for element in page_entries:
                entry = _raw_entry_from_atom(element, configured)
                if entry is not None and entry.source_arxiv_id not in seen:
                    seen.add(entry.source_arxiv_id)
                    entries.append(entry)
            offset += len(page_entries)
            if not page_entries or (total is not None and offset >= total):
                break
        else:
            raise ArxivSourceError("arXiv API listing exceeded the page limit")
        if total is not None and offset < total:
            raise ArxivSourceError(
                f"arXiv API listing incomplete: {offset} of {total} entries"
            )
        return entries

    def _get_with_retry(self, url: str, timeout_seconds: float) -> bytes:
        for attempt in range(4):
            try:
                return self._transport.get(url, timeout=timeout_seconds * 3)
            except ArxivSourceError:
                if attempt == 3:
                    raise
                self._sleep(30 * (attempt + 1))
        raise AssertionError("unreachable")


def _raw_entry_from_atom(
    element: ET.Element, configured: set[str]
) -> RawArxivEntry | None:
    source_id = (element.findtext(f"{_ATOM}id") or "").rsplit("/", 1)[-1]
    if not source_id:
        return None
    wrapper = ET.Element(f"{_ATOM}feed")
    wrapper.append(element)
    try:
        candidate = parse_arxiv_atom_candidate(
            ET.tostring(wrapper), expected_id=normalize_arxiv_id(source_id)
        )
    except ValueError:
        return None
    if candidate is None:
        return None
    primary = element.find(f"{_ARXIV}primary_category")
    primary_term = (
        primary.attrib.get("term", "").strip()
        if primary is not None
        else (candidate.categories[0] if candidate.categories else "")
    )
    announce_type = (
        AnnounceType.NEW if primary_term in configured else AnnounceType.CROSS
    )
    categories = list(candidate.categories)
    if primary_term and primary_term not in categories:
        categories.insert(0, primary_term)
    return RawArxivEntry(
        source_arxiv_id=source_id,
        title=candidate.title,
        abstract=candidate.abstract,
        authors=candidate.authors,
        categories=categories,
        announce_type=announce_type,
    )


@dataclass(frozen=True)
class GuardedArxivSource:
    """RSS source that refuses listings belonging to another publication date."""

    rss: ArxivClient
    api: ArxivApiListingClient

    def fetch_new(
        self, categories: Sequence[str], *, timeout_seconds: float
    ) -> list[RawArxivEntry]:
        return self.rss.fetch_new(categories, timeout_seconds=timeout_seconds)

    def fetch_for_publication(
        self,
        categories: Sequence[str],
        *,
        timeout_seconds: float,
        publication_date: date,
    ) -> tuple[list[RawArxivEntry], str]:
        """Return entries and the source used: ``rss`` or ``arxiv_api``."""
        listings = self.rss.fetch_listings(categories, timeout_seconds=timeout_seconds)
        decisions = {
            classify_listing(
                listing_date,
                publication_date=publication_date,
                item_count=len(entries),
            )
            for _, listing_date, entries in listings
        }
        if ListingMatch.SUPERSEDED in decisions:
            return (
                self.api.fetch_publication(
                    categories,
                    publication_date=publication_date,
                    timeout_seconds=timeout_seconds,
                ),
                "arxiv_api",
            )
        if ListingMatch.NOT_READY in decisions:
            raise ArxivListingNotReady(
                f"arXiv listing for {publication_date.isoformat()} is not published yet"
            )
        return [entry for _, _, entries in listings for entry in entries], "rss"
