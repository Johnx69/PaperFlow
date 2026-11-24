"""Rebuild a lost publication date's arXiv listing as a backfill snapshot."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from paperflow.arxiv_client import UrllibTransport
from paperflow.arxiv_listing import ArxivApiListingClient, submission_window
from paperflow.config import load_config_bundle
from paperflow.normalize import normalize_and_deduplicate
from paperflow.paper_store import load_run_state
from paperflow.recovery import load_backfill_snapshot


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--date", type=date.fromisoformat, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    root = arguments.root.resolve()
    bundle = load_config_bundle(root)
    categories = bundle.runtime.source.categories
    window = submission_window(arguments.date)
    if window is None:
        print(f"{arguments.date} has no arXiv announcement; nothing to backfill.")
        return 1
    entries = ArxivApiListingClient(UrllibTransport()).fetch_publication(
        categories,
        publication_date=arguments.date,
        timeout_seconds=bundle.runtime.source.request_timeout_seconds,
    )
    start, end = window
    snapshot = {
        "schema_version": 1,
        "publication_date": arguments.date.isoformat(),
        "announcement_date": (arguments.date + timedelta(days=1)).isoformat(),
        "categories": list(categories),
        "captured_at": datetime.now(UTC).isoformat(),
        "provenance": (
            "arXiv Atom API search by configured categories and v1 submittedDate "
            f"window {start.isoformat()} to {end.isoformat()}; approximates the "
            "lost RSS announcement (papers held in moderation may differ)"
        ),
        "candidate_count": len(normalize_and_deduplicate(entries)),
        "entries": [entry.model_dump(mode="json") for entry in entries],
    }
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    load_backfill_snapshot(
        arguments.output,
        categories=categories,
        latest_published=load_run_state(
            root / "data/state.json"
        ).last_successful_local_date,
    )
    print(
        f"Backfill snapshot for {arguments.date}: {len(entries)} entries, "
        f"{snapshot['candidate_count']} candidates -> {arguments.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
