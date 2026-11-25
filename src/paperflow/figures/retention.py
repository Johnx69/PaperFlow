"""Rolling retention for published figure assets.

Figures dominate PaperFlow's published size (hundreds of KB per paper), and
GitHub Pages refuses deployments above 10 GB. Papers first seen within the
retention window keep their figures. Older papers become ``archived``: their
figure files are deleted while title, authors, summary, categories and topics
stay published.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterable, Mapping
from datetime import date, timedelta
from pathlib import Path

from paperflow.models import FigureStatus, SelectedPaper


def retention_start(reference: date, retention_days: int) -> date:
    """First ``first_seen_date`` that still keeps figures (inclusive window)."""
    if retention_days < 1:
        raise ValueError("retention_days must be at least 1")
    return reference - timedelta(days=retention_days - 1)


def archive_expired_figures(
    papers: Mapping[str, SelectedPaper], *, keep_since: date
) -> tuple[dict[str, SelectedPaper], tuple[str, ...]]:
    """Mark papers older than ``keep_since`` archived; return newly archived IDs."""
    updated = dict(papers)
    archived: list[str] = []
    for paper_id, paper in sorted(papers.items()):
        if (
            paper.first_seen_date >= keep_since
            or paper.figure_status == FigureStatus.ARCHIVED
        ):
            continue
        updated[paper_id] = SelectedPaper.model_validate(
            paper.model_copy(
                update={
                    "hero_figure": None,
                    "figures": [],
                    "figure_status": FigureStatus.ARCHIVED,
                }
            ).model_dump()
        )
        archived.append(paper_id)
    return updated, tuple(archived)


def figure_directory(root: Path, paper_id: str) -> Path:
    """Return the publication directory holding one paper's figures."""
    figures_root = (root / "figures").resolve()
    directory = (figures_root / paper_id.replace("/", "_")).resolve()
    if directory.parent != figures_root:
        raise ValueError(f"unsafe figure directory for {paper_id!r}")
    return directory


def delete_archived_figure_files(
    root: Path, papers: Mapping[str, SelectedPaper]
) -> int:
    """Delete figure directories of every archived paper; return dirs removed.

    Only directories belonging to a paper whose canonical state is archived are
    touched, so READY figures can never be removed by this function.
    """
    removed = 0
    for paper_id, paper in papers.items():
        if paper.figure_status != FigureStatus.ARCHIVED:
            continue
        directory = figure_directory(root, paper_id)
        if directory.is_dir():
            shutil.rmtree(directory)
            removed += 1
    return removed


def unreferenced_figure_directories(
    root: Path, papers: Iterable[SelectedPaper]
) -> list[Path]:
    """Figure directories that no READY paper references (for reporting)."""
    figures_root = root / "figures"
    if not figures_root.is_dir():
        return []
    referenced = {
        figure_directory(root, paper.arxiv_id).name
        for paper in papers
        if paper.figure_status == FigureStatus.READY
    }
    return sorted(
        path
        for path in figures_root.iterdir()
        if path.is_dir() and path.name not in referenced
    )
