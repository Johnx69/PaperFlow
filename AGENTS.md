# PaperFlow Codex Instructions

## Source of truth

Read before implementing:

1. docs/specification/PaperFlow_Technical_Plan_v1.md
2. docs/architecture/IMPLEMENTATION_DECISIONS_v1.md

PaperFlow is now a GitHub Pages website plus its daily pipeline. The former
iPhone app was removed; iOS/SwiftUI sections in the historical docs no longer
apply.

`docs/specification/PaperFlow_Technical_Plan_v3_2.md` is retained as historical
planning context. It is not authoritative where the V1 as-built plan or
implementation decisions describe the completed behavior.

Priority when sources conflict:

1. V1 technical plan for data models, state semantics, and pipeline behavior.
2. V1 implementation decisions for accepted architectural choices.

## Development rules

- Work on one implementation phase at a time.
- Do not implement unrelated future features.
- Do not add dependencies unless they materially simplify the implementation.
- Run the smallest relevant test after each change.
- Run the broader test suite before completing a task.
- Never commit secrets.

## Python

- Python source lives under src/paperflow.
- Use typed Pydantic models.
- Prefer deterministic pure functions where possible.
- Network clients must be injectable/mockable.
- LLM calls must use the single OpenRouter abstraction.
- All outputs must validate before canonical files are committed.

Before completing Python work run:

pytest
ruff check .
python -m paperflow.cli.validate_taxonomy

## Figure retention

- Papers first seen within `figures.retention_days` (default 30) keep
  extracted figures. Older papers are marked `archived`: their figure files
  are deleted and they keep title, summary, categories and topics only.

## Completion report

For every task report:

- files changed
- behavior implemented
- tests run
- exact result
- remaining known issues

## Secrets and credentials

Never print, log, cat, echo, serialize, commit, or include secret values
in reports.

The following variables may exist:

- OPENROUTER_API_KEY
- GH_TOKEN, if explicitly configured

Only check whether a credential exists, never display its value.

Never modify .env or secret stores unless the task explicitly requires it.

Never commit:

- .env
- credentials
- API keys
- authorization headers
- signing certificates

GitHub repository authentication should use the already-authenticated
GitHub CLI whenever available.

Production OpenRouter credentials must be stored as GitHub Actions
repository secrets.
