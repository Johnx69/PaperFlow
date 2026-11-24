from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import yaml

from paperflow.arxiv_listing import ArxivListingNotReady
from paperflow.cli.sync_schedule import sync_schedule
from paperflow.models import FilterStatus, RawArxivEntry, RunState
from paperflow.normalize import normalize_and_deduplicate
from paperflow.paper_store import load_run_state, load_selected_store
from paperflow.pipeline import PipelineDependencies, run_pipeline
from paperflow.render.validation import validate_repository
from paperflow.screening_ledger import ScreeningLedger
from paperflow.taxonomy import load_taxonomy
from tests.integration.test_full_pipeline import (
    FixturePipelineLLM,
    FixtureRefetch,
    SequencedSource,
    project_copy,
    raw_fixture,
)


def _configured_project(tmp_path: Path) -> Path:
    project = project_copy(tmp_path)
    runtime_path = project / "configs/runtime.yaml"
    runtime = yaml.safe_load(runtime_path.read_text())
    runtime["schedule"]["enabled"] = True
    runtime["figures"]["enabled"] = False
    runtime_path.write_text(yaml.safe_dump(runtime, sort_keys=False))
    sync_schedule(project, check=False)
    return project


def _subset(entries: list[RawArxivEntry], ids: set[str]) -> list[RawArxivEntry]:
    return [entry for entry in entries if entry.source_arxiv_id[:-2] in ids]


def _results(fixture: dict, ids: list[str]) -> list[dict]:
    by_id = {row["arxiv_id"]: row for row in fixture["primary_results"]}
    return [by_id[paper_id] for paper_id in ids]


class NotReadySource:
    def fetch_new(self, categories, *, timeout_seconds):
        raise AssertionError("guarded source must be used for scheduled runs")

    def fetch_for_publication(
        self, categories, *, timeout_seconds, publication_date
    ):
        raise ArxivListingNotReady(f"{publication_date} not published")


def test_unpublished_listing_skips_without_side_effects(tmp_path: Path) -> None:
    project = _configured_project(tmp_path)
    before = {
        path: path.read_bytes()
        for path in (project / "data").rglob("*")
        if path.is_file()
    }
    result = run_pipeline(
        project,
        PipelineDependencies(
            source_client=NotReadySource(),
            refetcher=FixtureRefetch(),
            llm_client_factory=lambda: pytest.fail("no LLM call expected"),
            now=lambda: datetime(2026, 8, 25, 2, tzinfo=UTC),
        ),
        manual=False,
    )
    assert result.ran is False
    assert result.reason == "source_not_ready"
    after = {
        path: path.read_bytes()
        for path in (project / "data").rglob("*")
        if path.is_file()
    }
    assert after == before


def test_backfill_republishes_lost_date_without_moving_schedule(
    tmp_path: Path,
) -> None:
    project = _configured_project(tmp_path)
    entries, fixture = raw_fixture()
    entries = [entry for entry in entries if entry.categories == ["cs.AI"]]
    first_ids = ["2608.20004", "2608.20005", "2608.20006"]
    lost_ids = ["2608.20001", "2608.20002", "2608.20007"]
    llm = FixturePipelineLLM(
        project,
        [_results(fixture, first_ids), _results(fixture, lost_ids)],
    )
    instants = iter(
        [
            # 2026-08-23 (Sun) 22:00 EDT publishes the 2026-08-23 listing.
            datetime(2026, 8, 24, 2, tzinfo=UTC),
            # The 2026-08-24 (Mon) run fetched an empty listing: the lost day.
            datetime(2026, 8, 25, 2, tzinfo=UTC),
            datetime(2026, 8, 26, 2, tzinfo=UTC),
        ]
    )
    dependencies = PipelineDependencies(
        source_client=SequencedSource([_subset(entries, set(first_ids)), [], []]),
        refetcher=FixtureRefetch(),
        llm_client_factory=lambda: llm,
        now=lambda: next(instants),
        run_id_factory=lambda now: f"fixture-{now:%Y-%m-%dT%H}",
    )
    assert run_pipeline(project, dependencies, manual=False).ran
    assert run_pipeline(project, dependencies, manual=False).ran
    state_before = (project / "data/state.json").read_bytes()
    index_before = json.loads((project / "data/feed_index.json").read_text())
    stats_before = (project / "data/run_stats/2026-08-24.json").read_bytes()

    lost_entries = _subset(entries, set(lost_ids)) + _subset(entries, {"2608.20004"})
    snapshot = project / "backfill.json"
    snapshot.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "publication_date": "2026-08-24",
                "announcement_date": "2026-08-25",
                "categories": load_config_categories(project),
                "captured_at": "2026-08-27T00:00:00+00:00",
                "provenance": "test fixture",
                "candidate_count": len(normalize_and_deduplicate(lost_entries)),
                "entries": [entry.model_dump(mode="json") for entry in lost_entries],
            }
        )
    )
    result = run_pipeline(
        project, dependencies, manual=False, backfill_snapshot=snapshot
    )

    assert result.ran and result.reason == "backfill"
    # Already-screened 2608.20004 is not re-screened; only the lost papers are.
    assert result.stats is not None and result.stats.screened == 3
    assert (project / "data/state.json").read_bytes() == state_before
    assert (project / "data/run_stats/2026-08-24.json").read_bytes() == stats_before
    assert (project / "data/run_stats/backfill/2026-08-24.json").is_file()
    index_after = json.loads((project / "data/feed_index.json").read_text())
    assert index_after["generated_at"] == index_before["generated_at"]

    store = load_selected_store(
        project / "data/papers.json", load_taxonomy(project / "configs/topics.yaml")
    )
    for paper_id in ("2608.20001", "2608.20002"):
        assert store.papers[paper_id].first_seen_date == date(2026, 8, 24)
    day = json.loads((project / "data/daily_feeds/2026-08-24.json").read_text())
    assert {"2608.20001", "2608.20002"} <= {
        paper["arxiv_id"] for paper in day["papers"]
    }
    latest = ScreeningLedger(project / "data/screening_events").load_latest()
    assert latest["2608.20007"].filter_status == FilterStatus.DROPPED
    validate_repository(project)
    assert load_run_state(
        project / "data/state.json"
    ).last_successful_run_id == "fixture-2026-08-25T02"


def test_backfill_rejects_dates_not_yet_published(tmp_path: Path) -> None:
    project = _configured_project(tmp_path)
    (project / "data/state.json").write_text(
        RunState().model_dump_json(indent=2) + "\n"
    )
    snapshot = project / "future.json"
    snapshot.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "publication_date": "2026-08-24",
                "announcement_date": "2026-08-25",
                "categories": load_config_categories(project),
                "captured_at": "2026-08-27T00:00:00+00:00",
                "provenance": "test fixture",
                "candidate_count": 0,
                "entries": [],
            }
        )
    )
    with pytest.raises(ValueError, match="dates already published"):
        run_pipeline(
            project,
            PipelineDependencies(
                source_client=SequencedSource([]),
                refetcher=FixtureRefetch(),
                llm_client_factory=lambda: pytest.fail("no LLM call expected"),
            ),
            manual=False,
            backfill_snapshot=snapshot,
        )


def load_config_categories(project: Path) -> list[str]:
    runtime = yaml.safe_load((project / "configs/runtime.yaml").read_text())
    return runtime["source"]["categories"]
