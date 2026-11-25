from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from paperflow.figures.extract import FigureProductionService
from paperflow.figures.retention import (
    archive_expired_figures,
    delete_archived_figure_files,
    figure_directory,
    retention_start,
    unreferenced_figure_directories,
)
from paperflow.models import (
    FigureAsset,
    FigureStatus,
    FilterStatus,
    SelectedPaper,
    SummaryStatus,
    TopicAssignment,
)

HASH = "7" * 64


def _paper(index: int, seen: date, status: FigureStatus) -> SelectedPaper:
    paper_id = f"2611.{10000 + index:05d}"
    at = datetime.fromisoformat(f"{seen.isoformat()}T21:00:00-05:00")
    ready = status == FigureStatus.READY
    return SelectedPaper(
        arxiv_id=paper_id,
        source_arxiv_id=f"{paper_id}v1",
        title=f"Paper {index}",
        abstract=f"Abstract {index}.",
        authors=["Author"],
        categories=["cs.AI"],
        arxiv_url=f"https://arxiv.org/abs/{paper_id}",
        pdf_url=f"https://arxiv.org/pdf/{paper_id}",
        first_seen_at=at,
        first_seen_date=at.date(),
        filter_status=FilterStatus.KEPT,
        relevance=8,
        novelty=7,
        topic_assignments=[
            TopicAssignment(
                topic_id="world-models", subtopic_ids=["video-world-models"]
            )
        ],
        selection_reason="Fixture.",
        summary_status=SummaryStatus.GENERATED,
        tldr=f"Summary {index}.",
        bullets=["Problem.", "Method.", "Contribution."],
        hero_figure=f"figures/{paper_id}/hero.webp" if ready else None,
        figures=(
            [
                FigureAsset(
                    figure_id="figure-001",
                    kind="figure",
                    page=1,
                    image_path=f"figures/{paper_id}/figure-001.webp",
                    width=10,
                    height=10,
                )
            ]
            if ready
            else []
        ),
        figure_status=status,
        taxonomy_version=1,
        taxonomy_hash=HASH,
        filter_prompt_version="filter-v3",
        filter_prompt_hash=HASH,
        summary_prompt_version="summary-v2",
        summary_prompt_hash=HASH,
        filter_model="filter-model",
        summary_model="summary-model",
    )


def test_retention_window_is_inclusive_of_reference_day() -> None:
    assert retention_start(date(2026, 11, 30), 30) == date(2026, 11, 1)
    assert retention_start(date(2026, 11, 30), 1) == date(2026, 11, 30)
    with pytest.raises(ValueError):
        retention_start(date(2026, 11, 30), 0)


def test_archive_expired_figures_keeps_text_and_window() -> None:
    keep_since = date(2026, 11, 1)
    papers = {
        p.arxiv_id: p
        for p in (
            _paper(1, date(2026, 10, 31), FigureStatus.READY),
            _paper(2, date(2026, 11, 1), FigureStatus.READY),
            _paper(3, date(2026, 10, 1), FigureStatus.FAILED),
            _paper(4, date(2026, 9, 1), FigureStatus.ARCHIVED),
        )
    }
    updated, newly = archive_expired_figures(papers, keep_since=keep_since)

    assert newly == ("2611.10001", "2611.10003")
    old = updated["2611.10001"]
    assert old.figure_status == FigureStatus.ARCHIVED
    assert old.hero_figure is None and old.figures == []
    assert (old.title, old.tldr, old.bullets, old.categories) == (
        "Paper 1",
        "Summary 1.",
        ["Problem.", "Method.", "Contribution."],
        ["cs.AI"],
    )
    assert old.topic_assignments == papers["2611.10001"].topic_assignments
    assert updated["2611.10002"] == papers["2611.10002"]
    assert updated["2611.10004"] == papers["2611.10004"]
    # Idempotent: a second pass archives nothing new.
    assert archive_expired_figures(updated, keep_since=keep_since)[1] == ()


def test_delete_only_touches_archived_directories(tmp_path: Path) -> None:
    ready = _paper(1, date(2026, 11, 20), FigureStatus.READY)
    archived = _paper(2, date(2026, 9, 1), FigureStatus.ARCHIVED)
    stray = tmp_path / "figures" / "9999.99999"
    for directory in (
        figure_directory(tmp_path, ready.arxiv_id),
        figure_directory(tmp_path, archived.arxiv_id),
        stray,
    ):
        directory.mkdir(parents=True)
        (directory / "hero.webp").write_bytes(b"x")

    papers = {ready.arxiv_id: ready, archived.arxiv_id: archived}
    assert delete_archived_figure_files(tmp_path, papers) == 1
    assert figure_directory(tmp_path, ready.arxiv_id).is_dir()
    assert not figure_directory(tmp_path, archived.arxiv_id).exists()
    assert stray.is_dir()
    assert unreferenced_figure_directories(tmp_path, papers.values()) == [stray]
    assert delete_archived_figure_files(tmp_path, papers) == 0


def test_figure_directory_rejects_escapes(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        figure_directory(tmp_path, "..")


def test_extractor_never_reprocesses_archived_papers(tmp_path: Path) -> None:
    archived = _paper(1, date(2026, 9, 1), FigureStatus.ARCHIVED)

    class ExplodingAdapter:
        def extract(self, *args, **kwargs):
            raise AssertionError("archived papers must not be extracted")

    processor = FigureProductionService(
        adapter=ExplodingAdapter(), cache_root=tmp_path
    )
    result = processor.process(
        {archived.arxiv_id: archived}, publication_root=tmp_path, force=True
    )
    assert result[archived.arxiv_id] == archived
