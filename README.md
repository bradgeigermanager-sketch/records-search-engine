# Records Search Engine

A provenance-preserving search engine prototype. Milestone 2 separates stable source identities from immutable document versions and records every ingestion attempt.

## Features
- FastAPI JSON API and browser UI
- HTTP/HTTPS HTML ingestion with a 2 MB response limit
- Stable source registry and provenance
- Immutable document versions with content hashes, retrieval metadata, and extraction status
- Changed content creates a new version; unchanged retrievals are logged without duplicate versions
- SQLite FTS5 current-version search by default, with optional historical search
- Idempotent schema migration, including legacy v0.1 document migration
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
2. PostgreSQL migration and structured metadata filters
3. Archive connectors and respectful scheduled retrieval
4. Language detection, synonyms, and semantic retrieval
5. Entity registry, relationship graph, timelines, and evidence conflict views
