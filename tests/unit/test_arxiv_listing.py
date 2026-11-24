from __future__ import annotations

from datetime import UTC, date, datetime
from urllib.parse import parse_qs, urlparse

import pytest

from paperflow.arxiv_client import ArxivClient, ArxivSourceError
from paperflow.arxiv_listing import (
    ArxivApiListingClient,
    ArxivListingNotReady,
    GuardedArxivSource,
    ListingMatch,
    classify_listing,
    rss_listing_date,
    submission_window,
)
from paperflow.models import AnnounceType

CATEGORIES = ["cs.AI", "cs.RO"]


def _rss(pub_date: str | None, items: int) -> bytes:
    date_tag = f"<pubDate>{pub_date}</pubDate>" if pub_date else ""
    body = "".join(
        f"""<item><title>T{index}</title>
<link>https://arxiv.org/abs/2611.0000{index}</link>
<description>Announce Type: new Abstract: A{index}.</description>
<dc:creator>Author</dc:creator><category>cs.AI</category></item>"""
        for index in range(1, items + 1)
    )
    return f"""<?xml version="1.0"?>
<rss xmlns:dc="http://purl.org/dc/elements/1.1/" version="2.0">
<channel><title>x</title>{date_tag}{body}</channel></rss>""".encode()


class MapTransport:
    def __init__(self, responder) -> None:
        self.responder = responder
        self.urls: list[str] = []

    def get(self, url: str, *, timeout: float) -> bytes:
        self.urls.append(url)
        return self.responder(url)


def test_rss_listing_date_uses_eastern_calendar_date() -> None:
    assert rss_listing_date(_rss("Tue, 03 Nov 2026 00:30:00 -0500", 0)) == date(
        2026, 11, 3
    )
    # 03:00 UTC on Nov 4 is still Nov 3 in New York.
    assert rss_listing_date(_rss("Wed, 04 Nov 2026 03:00:00 +0000", 0)) == date(
        2026, 11, 3
    )
    assert rss_listing_date(_rss(None, 0)) is None


@pytest.mark.parametrize(
    ("listing", "publication", "items", "expected"),
    [
        (date(2026, 11, 3), date(2026, 11, 2), 5, ListingMatch.MATCH),
        # Nov 5 incident: Thursday run read Wednesday's still-live listing.
        (date(2026, 11, 5), date(2026, 11, 5), 1306, ListingMatch.NOT_READY),
        # Nov 2 / Nov 11 incidents: an announcement day came back empty.
        (date(2026, 11, 3), date(2026, 11, 2), 0, ListingMatch.NOT_READY),
        (date(2026, 11, 7), date(2026, 11, 5), 9, ListingMatch.SUPERSEDED),
        # Friday/Saturday have no announcement and keep legacy behaviour.
        (date(2026, 11, 9), date(2026, 11, 6), 0, ListingMatch.UNCHECKED),
        (None, date(2026, 11, 2), 0, ListingMatch.UNCHECKED),
    ],
)
def test_classify_listing(listing, publication, items, expected) -> None:
    assert (
        classify_listing(listing, publication_date=publication, item_count=items)
        == expected
    )


def test_submission_windows_follow_arxiv_cutoffs() -> None:
    # Monday announcement: Friday 14:00 EDT -> Monday 14:00 EST (DST ended Nov 1).
    assert submission_window(date(2026, 11, 2)) == (
        datetime(2026, 10, 30, 18, tzinfo=UTC),
        datetime(2026, 11, 2, 19, tzinfo=UTC),
    )
    assert submission_window(date(2026, 11, 5)) == (
        datetime(2026, 11, 4, 19, tzinfo=UTC),
        datetime(2026, 11, 5, 19, tzinfo=UTC),
    )
    # Sunday announcement covers Thursday -> Friday submissions.
    assert submission_window(date(2026, 11, 8)) == (
        datetime(2026, 11, 5, 19, tzinfo=UTC),
        datetime(2026, 11, 6, 19, tzinfo=UTC),
    )
    assert submission_window(date(2026, 11, 6)) is None
    assert submission_window(date(2026, 11, 7)) is None


def _atom_page(ids: list[tuple[str, str]], total: int) -> bytes:
    entries = "".join(
        f"""<entry><id>http://arxiv.org/abs/{paper_id}v1</id>
<title>Title {paper_id}</title><summary>Abstract {paper_id}.</summary>
<author><name>Author</name></author>
<arxiv:primary_category term="{primary}"/>
<category term="{primary}"/><category term="cs.AI"/></entry>"""
        for paper_id, primary in ids
    )
    return f"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom"
 xmlns:arxiv="http://arxiv.org/schemas/atom"
 xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
<opensearch:totalResults>{total}</opensearch:totalResults>{entries}</feed>""".encode()


def test_api_listing_pages_and_marks_cross_lists() -> None:
    pages = {
        0: _atom_page([("2611.00001", "cs.RO"), ("2611.00002", "math.OC")], 3),
        2: _atom_page([("2611.00003", "cs.AI")], 3),
    }

    def respond(url: str) -> bytes:
        query = parse_qs(urlparse(url).query)
        window = "submittedDate:[202610301800 TO 202611021900]"
        assert window in query["search_query"][0]
        return pages[int(query["start"][0])]

    client = ArxivApiListingClient(
        MapTransport(respond), page_size=2, sleep=lambda _: None
    )
    entries = client.fetch_publication(
        CATEGORIES, publication_date=date(2026, 11, 2), timeout_seconds=1
    )
    assert [entry.source_arxiv_id for entry in entries] == [
        "2611.00001v1",
        "2611.00002v1",
        "2611.00003v1",
    ]
    assert [entry.announce_type for entry in entries] == [
        AnnounceType.NEW,
        AnnounceType.CROSS,
        AnnounceType.NEW,
    ]
    assert entries[1].categories[0] == "math.OC"


def test_api_listing_rejects_truncated_results() -> None:
    client = ArxivApiListingClient(
        MapTransport(lambda url: _atom_page([("2611.00001", "cs.AI")], 5)),
        page_size=1,
        sleep=lambda _: None,
        max_pages=2,
    )
    with pytest.raises(ArxivSourceError):
        client.fetch_publication(
            CATEGORIES, publication_date=date(2026, 11, 2), timeout_seconds=1
        )


def _guarded(rss_date: str, items: int, api_entries: bytes | None = None):
    def respond(url: str) -> bytes:
        if url.startswith("https://rss.arxiv.org"):
            return _rss(rss_date, items)
        assert api_entries is not None
        return api_entries

    transport = MapTransport(respond)
    return GuardedArxivSource(
        rss=ArxivClient(transport),
        api=ArxivApiListingClient(transport, sleep=lambda _: None),
    )


def test_guarded_source_uses_matching_rss() -> None:
    source = _guarded("Tue, 03 Nov 2026 00:30:00 -0500", 2)
    entries, used = source.fetch_for_publication(
        CATEGORIES, timeout_seconds=1, publication_date=date(2026, 11, 2)
    )
    assert used == "rss" and len(entries) == 4


def test_guarded_source_waits_for_unpublished_listing() -> None:
    source = _guarded("Thu, 05 Nov 2026 00:30:00 -0500", 3)
    with pytest.raises(ArxivListingNotReady):
        source.fetch_for_publication(
            CATEGORIES, timeout_seconds=1, publication_date=date(2026, 11, 5)
        )


def test_guarded_source_rebuilds_superseded_listing_from_api() -> None:
    source = _guarded(
        "Sat, 07 Nov 2026 00:30:00 -0500",
        3,
        api_entries=_atom_page([("2611.07001", "cs.AI")], 1),
    )
    entries, used = source.fetch_for_publication(
        CATEGORIES, timeout_seconds=1, publication_date=date(2026, 11, 5)
    )
    assert used == "arxiv_api"
    assert [entry.source_arxiv_id for entry in entries] == ["2611.07001v1"]


def test_api_listing_retries_spurious_empty_page() -> None:
    responses = iter(
        [
            _atom_page([("2611.00001", "cs.AI")], 2),
            _atom_page([], 2),
            _atom_page([("2611.00002", "cs.AI")], 2),
        ]
    )
    client = ArxivApiListingClient(
        MapTransport(lambda url: next(responses)), page_size=1, sleep=lambda _: None
    )
    entries = client.fetch_publication(
        CATEGORIES, publication_date=date(2026, 11, 2), timeout_seconds=1
    )
    assert len(entries) == 2
