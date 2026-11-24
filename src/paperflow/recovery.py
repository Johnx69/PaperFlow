"""Validated, dated inputs for explicitly requested historical recovery."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field

from paperflow.models import DomainModel, RawArxivEntry
from paperflow.normalize import normalize_and_deduplicate


class RecoverySnapshot(DomainModel):
    schema_version: Literal[1]
    publication_date: date
    announcement_date: date
    categories: list[str] = Field(min_length=1)
    captured_at: AwareDatetime
    provenance: str = Field(min_length=1)
    candidate_count: int = Field(ge=0)
    entries: list[RawArxivEntry]


def load_recovery_snapshot(
    path: Path, *, publication_date: date, categories: Sequence[str]
) -> list[RawArxivEntry]:
    """Reject a snapshot for any date other than the next due publication."""
    snapshot = RecoverySnapshot.model_validate_json(path.read_bytes())
    if snapshot.publication_date != publication_date:
        raise ValueError("recovery snapshot does not match the next due publication")
    if snapshot.announcement_date != publication_date + timedelta(days=1):
        raise ValueError("recovery announcement date does not match publication")
    if snapshot.categories != list(categories):
        raise ValueError("recovery categories do not match configured source coverage")
    if len(normalize_and_deduplicate(snapshot.entries)) != snapshot.candidate_count:
        raise ValueError("recovery candidate count does not match snapshot contents")
    return snapshot.entries


def load_backfill_snapshot(
    path: Path, *, categories: Sequence[str], latest_published: date | None
) -> tuple[date, list[RawArxivEntry]]:
    """Load a snapshot that repairs an already-published historical date."""
    snapshot = RecoverySnapshot.model_validate_json(path.read_bytes())
    if latest_published is None or snapshot.publication_date > latest_published:
        raise ValueError(
            "backfill is only for dates already published; "
            "use the scheduled run or --source-snapshot instead"
        )
    if snapshot.announcement_date != snapshot.publication_date + timedelta(days=1):
        raise ValueError("backfill announcement date does not match publication")
    if snapshot.categories != list(categories):
        raise ValueError("backfill categories do not match configured source coverage")
    if len(normalize_and_deduplicate(snapshot.entries)) != snapshot.candidate_count:
        raise ValueError("backfill candidate count does not match snapshot contents")
    return snapshot.publication_date, snapshot.entries
