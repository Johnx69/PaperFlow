"""Apply the rolling figure-retention window to the checked-in repository.

The daily pipeline applies retention on every run; this command applies it
without an LLM or network call (first rollout, or after changing
``figures.retention_days``). Canonical papers are updated first, then their
figure directories are deleted, then all public outputs are rebuilt.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from paperflow.cli import rebuild_outputs
from paperflow.config import load_config_bundle
from paperflow.figures.retention import (
    archive_expired_figures,
    delete_archived_figure_files,
    retention_start,
    unreferenced_figure_directories,
)
from paperflow.models import FigureStatus
from paperflow.paper_store import (
    load_run_state,
    load_selected_store,
    save_run_state,
    save_selected_store,
)
from paperflow.render.validation import validate_repository
from paperflow.taxonomy import load_taxonomy, taxonomy_hash


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument(
        "--dry-run", action="store_true", help="report only; change nothing"
    )
    arguments = parser.parse_args(argv)
    root = arguments.root.resolve()
    bundle = load_config_bundle(root)
    taxonomy = load_taxonomy(root / "configs/topics.yaml")
    selected = load_selected_store(root / "data/papers.json", taxonomy)
    state = load_run_state(root / "data/state.json")
    if state.last_successful_local_date is None:
        print("No published run yet; nothing to retain.")
        return 0
    if (
        state.model_config_hash != bundle.model_hash
        or state.taxonomy_hash != taxonomy_hash(taxonomy)
    ):
        raise SystemExit(
            "Model or taxonomy changed since the last run; publish a daily run "
            "first so summaries and topics match the current configuration."
        )
    keep_since = retention_start(
        state.last_successful_local_date, bundle.runtime.figures.retention_days
    )
    papers, newly_archived = archive_expired_figures(
        selected.papers, keep_since=keep_since
    )
    ready = sum(p.figure_status == FigureStatus.READY for p in papers.values())
    archived = sum(p.figure_status == FigureStatus.ARCHIVED for p in papers.values())
    print(
        f"Keeping figures for papers first seen on or after {keep_since}: "
        f"{ready} ready, {archived} archived ({len(newly_archived)} newly)."
    )
    if arguments.dry_run:
        return 0
    if newly_archived:
        save_selected_store(root / "data/papers.json", papers, taxonomy)
    removed = delete_archived_figure_files(root, papers)
    print(f"Deleted {removed} archived figure directories.")
    leftovers = unreferenced_figure_directories(root, papers.values())
    if leftovers:
        print(
            f"Note: {len(leftovers)} figure directories are not referenced by a "
            "ready paper and were left untouched."
        )
    status = rebuild_outputs.main(["--root", str(root)])
    if status != 0:
        return status
    # Every output was just rebuilt with the current runtime configuration
    # (retention window, publishing settings), so record that attestation.
    if state.runtime_config_hash != bundle.runtime_hash:
        state_path = root / "data/state.json"
        original = state_path.read_bytes()
        save_run_state(
            state_path,
            state.model_copy(update={"runtime_config_hash": bundle.runtime_hash}),
            publication_validated=True,
        )
        try:
            validate_repository(root)
        except Exception:
            state_path.write_bytes(original)
            raise
        print("Recorded the current runtime configuration hash.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
