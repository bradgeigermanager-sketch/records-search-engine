# Records Search Engine

A provenance-preserving search engine with versioned documents, structured metadata, typed entities, STEM artifacts, and evidence-aware relationships. This repository also serves as the **working registry for the owner-wide repository improvement program**.

## Owner repository improvement registry

The registry records repository assessments, planned work, completed changes, verification evidence, and cross-repository integration candidates. Update it whenever a repository is assessed or changed. Distinguish proposed work from completed work and never label a change verified until its relevant check has actually run.

- [Repository Improvement Registry](REPOSITORY_IMPROVEMENT_REGISTRY.md) — master status table, queue, and cross-repository map.
- [Improvement Log](IMPROVEMENT_LOG.md) — chronological log of assessments, decisions, commits, and verification outcomes.
- [Repository Review Template](docs/repository-review-template.md) — repeatable checklist for assessing each repository.

### Registry conventions

- Statuses: `NOT_STARTED`, `IN_REVIEW`, `PLANNED`, `IN_PROGRESS`, `BLOCKED`, `DONE`, `VERIFIED`, `DEFERRED`.
- Every work item should have an ID, target repository, concrete outcome, status, evidence/link, and next step.
- `DONE` means the change was made; `VERIFIED` means the relevant checks or review were completed and their evidence is linked.
- Relationship links between repositories are initially candidates until their interfaces/content have been reviewed.
- Keep source repositories authoritative for their own content. The registry coordinates work; it does not silently replace source documents.

## Features

- FastAPI JSON API and browser UI
- HTTP/HTTPS HTML ingestion with a 2 MB response limit
- GitHub repository text ingestion for public repositories or private repositories accessible to a configured `GITHUB_TOKEN`, preserving repository/ref/path/blob provenance
- Stable source registry and provenance
- Immutable document versions with content hashes, retrieval metadata, and extraction status
- Changed content creates a new version; unchanged retrievals are logged without duplicate versions
- SQLite FTS5 current-version search by default, with optional historical search
- Search query modes, field scope, weighted ranking, explainable match details, and domain/content-type/retrieval-date filters
- Typed entity registry and evidence-linked entity relationships with confidence and provenance
- Append-only source metadata revisions with explicit supersession history
- Versioned STEM Wiki and structured STEM artifacts
- Provenance-aware knowledge graph and versioned validation rules
- Automated tests, GitHub Actions CI, and Docker Compose deployment

## Run locally

```bash
python -m venv .venv
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000; API docs: http://127.0.0.1:8000/docs.

## Test

```bash
pytest -q
```

## Docker

```bash
docker compose up --build
```

## GitHub repository ingestion

For a bounded scan of an owner's accessible public repositories, the API provides `POST /github/ingest-owner`. Set `GITHUB_TOKEN` in the environment to allow access to private repositories that the token is authorized to read. This is a manually triggered ingestion flow, not an unattended crawler. Protect ingestion endpoints with authentication and appropriate network controls before exposing the service publicly.

## Search API examples

```bash
curl 'http://127.0.0.1:8000/search?q=record&mode=all&field=both'
curl 'http://127.0.0.1:8000/search?q=record%20lineage&mode=phrase&field=title'
curl 'http://127.0.0.1:8000/search?q=record&mode=any&domain=github.com&content_type=text/*'
curl 'http://127.0.0.1:8000/search?q=record&retrieved_after=2026-01-01'
curl 'http://127.0.0.1:8000/github/search?q=provenance&owner=bradgeigermanager-sketch'
curl 'http://127.0.0.1:8000/github/relationship-candidates?owner=bradgeigermanager-sketch'
```

Relevance scores are ranking aids, not probabilities, truth judgments, or source-quality ratings. Relationship candidates are suggestions; only reviewed, explicitly recorded relationships should be treated as accepted graph edges.

## Cross-repository improvement program

The initial priority is to inventory each repository, clarify its purpose and implementation status, improve its README/entry points, identify overlapping concepts, and propose shared versioned schemas and interfaces. Changes should be incremental, attributable to commits, and tested where executable code exists. See the registry and log linked above.
