# Records Search Engine

A provenance-preserving search engine prototype. Milestone 3 adds typed entities, explicit relationships, and append-only structured metadata with provenance.

## Features
- FastAPI JSON API and browser UI
- HTTP/HTTPS HTML ingestion with a 2 MB response limit
- Stable source registry and provenance
- Immutable document versions with content hashes, retrieval metadata, and extraction status
- Changed content creates a new version; unchanged retrievals are logged without duplicate versions
- SQLite FTS5 current-version search by default, with optional historical search
- Idempotent schema migration, including legacy v0.1 document migration
- Typed entity registry and evidence-linked entity relationships with confidence and provenance
- Append-only source metadata revisions with explicit supersession history
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

## API examples
```bash
curl -X POST http://127.0.0.1:8000/ingest -H 'Content-Type: application/json' -d '{"url":"https://example.org/"}'
curl 'http://127.0.0.1:8000/search?q=example'
curl 'http://127.0.0.1:8000/search?q=example&include_history=true'
curl 'http://127.0.0.1:8000/sources'
curl 'http://127.0.0.1:8000/documents?include_history=true'
```

## Safety and scope
Submit only sources you are authorized to retrieve; respect site terms, robots policies, rate limits, and copyright. The prototype blocks redirects, limits downloads, and only extracts HTML, but these controls do not eliminate all DNS-rebinding or network-level risks. Deploy behind egress controls for untrusted users. This is not a general crawler and does not bypass authentication or paywalls. Before public multi-user deployment, add authentication, rate limiting, stricter egress controls, and operational monitoring.

## Roadmap
1. Source registry, immutable versions, and ingestion logs — implemented in v0.2.0
2. Typed entity registry, relationships, and append-only metadata — implemented in v0.3.0
3. PostgreSQL migration and structured metadata filters
4. Archive connectors and respectful scheduled retrieval
5. Language detection, synonyms, timelines, and evidence-conflict views
