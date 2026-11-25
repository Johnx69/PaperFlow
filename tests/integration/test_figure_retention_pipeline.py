from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import yaml
from test_full_pipeline import (
    FixturePipelineLLM,
    FixtureRefetch,
    SequencedSource,
    project_copy,
    raw_fixture,
)

from paperflow.cli import apply_figure_retention
from paperflow.cli.sync_schedule import sync_schedule
from paperflow.models import FigureAsset, FigureStatus
from paperflow.paper_store import load_selected_store, save_selected_store
from paperflow.pipeline import PipelineDependencies, run_pipeline
from paperflow.render.validation import validate_repository
from paperflow.taxonomy import load_taxonomy

KEPT = "2608.20001"


def _project(tmp_path: Path, retention_days: int) -> Path:
    project = project_copy(tmp_path)
    runtime_path = project / "configs/runtime.yaml"
    runtime = yaml.safe_load(runtime_path.read_text())
    runtime["schedule"]["enabled"] = True
    runtime["figures"]["enabled"] = False
    runtime["figures"]["retention_days"] = retention_days
    runtime_path.write_text(yaml.safe_dump(runtime, sort_keys=False))
    sync_schedule(project, check=False)
    return project


def _publish_first_day(project: Path, instants) -> PipelineDependencies:
    entries, fixture = raw_fixture()
    entries = [e for e in entries if e.source_arxiv_id[:-2] in {KEPT, "2608.20004"}]
    by_id = {row["arxiv_id"]: row for row in fixture["primary_results"]}
    llm = FixturePipelineLLM(project, [[by_id[KEPT], by_id["2608.20004"]]])
    dependencies = PipelineDependencies(
        source_client=SequencedSource([entries, [], []]),
        refetcher=FixtureRefetch(),
        llm_client_factory=lambda: llm,
        now=lambda: next(instants),
        run_id_factory=lambda now: f"fixture-{now:%Y-%m-%dT%H}",
    )
    assert run_pipeline(project, dependencies, manual=False).ran
    return dependencies


def _give_ready_figures(project: Path) -> Path:
    taxonomy = load_taxonomy(project / "configs/topics.yaml")
    store = load_selected_store(project / "data/papers.json", taxonomy)
    directory = project / "figures" / KEPT
    directory.mkdir(parents=True)
    for name in ("hero.webp", "figure-001.webp"):
        (directory / name).write_bytes(b"RIFF....WEBP")
    papers = dict(store.papers)
    papers[KEPT] = papers[KEPT].model_copy(
        update={
            "figure_status": FigureStatus.READY,
            "hero_figure": f"figures/{KEPT}/hero.webp",
            "figures": [
                FigureAsset(
                    figure_id="figure-001",
                    kind="figure",
                    page=1,
                    image_path=f"figures/{KEPT}/figure-001.webp",
                    width=10,
                    height=10,
                )
            ],
        }
    )
    save_selected_store(project / "data/papers.json", papers, taxonomy)
    return directory


def _assert_archived(project: Path, directory: Path) -> None:
    taxonomy = load_taxonomy(project / "configs/topics.yaml")
    paper = load_selected_store(project / "data/papers.json", taxonomy).papers[KEPT]
    assert paper.figure_status == FigureStatus.ARCHIVED
    assert paper.hero_figure is None and paper.figures == []
    assert paper.tldr is not None and paper.title
    assert not directory.exists()
    day = json.loads((project / "data/daily_feeds/2026-08-23.json").read_text())
    public = next(p for p in day["papers"] if p["arxiv_id"] == KEPT)
    assert public["figure_status"] == "archived"
    assert public["title"] == paper.title and public["tldr"] == paper.tldr
    page = (project / "site/days/2026-08-23.html").read_text()
    card = page.split(f'data-arxiv-id="{KEPT}"', 1)[1].split("</article>", 1)[0]
    assert "figure-shell" not in card and paper.title in card
    validate_repository(project)


def test_daily_run_archives_figures_outside_window(tmp_path: Path) -> None:
    project = _project(tmp_path, retention_days=1)
    instants = iter(
        [datetime(2026, 8, 24, 2, tzinfo=UTC), datetime(2026, 8, 25, 2, tzinfo=UTC)]
    )
    dependencies = _publish_first_day(project, instants)
    directory = _give_ready_figures(project)

    # Next day: with a 1-day window, the 2026-08-23 paper loses its figures.
    assert run_pipeline(project, dependencies, manual=False).ran
    _assert_archived(project, directory)


def test_daily_run_keeps_figures_inside_window(tmp_path: Path) -> None:
    project = _project(tmp_path, retention_days=2)
    instants = iter(
        [datetime(2026, 8, 24, 2, tzinfo=UTC), datetime(2026, 8, 25, 2, tzinfo=UTC)]
    )
    dependencies = _publish_first_day(project, instants)
    directory = _give_ready_figures(project)

    assert run_pipeline(project, dependencies, manual=False).ran
    taxonomy = load_taxonomy(project / "configs/topics.yaml")
    paper = load_selected_store(project / "data/papers.json", taxonomy).papers[KEPT]
    assert paper.figure_status == FigureStatus.READY
    assert (directory / "hero.webp").is_file()
    validate_repository(project)


def test_retention_command_applies_window_offline(tmp_path: Path) -> None:
    project = _project(tmp_path, retention_days=30)
    instants = iter([datetime(2026, 8, 24, 2, tzinfo=UTC)])
    _publish_first_day(project, instants)
    directory = _give_ready_figures(project)
    assert apply_figure_retention.main(["--root", str(project)]) == 0
    assert directory.is_dir()  # inside the 30-day window: untouched

    runtime_path = project / "configs/runtime.yaml"
    runtime = yaml.safe_load(runtime_path.read_text())
    runtime["figures"]["retention_days"] = 1
    runtime_path.write_text(yaml.safe_dump(runtime, sort_keys=False))
    # Same day, 1-day window: the paper is still inside (inclusive).
    assert apply_figure_retention.main(["--root", str(project), "--dry-run"]) == 0
    assert directory.is_dir()
