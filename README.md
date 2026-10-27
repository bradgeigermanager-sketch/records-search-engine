# Records Search Engine

A provenance-preserving search engine with a versioned STEM Wiki, typed STEM artifacts, structured metadata filters, typed entities, and evidence-aware relationships.

## Features
- FastAPI JSON API and browser UI
- HTTP/HTTPS HTML ingestion with a 2 MB response limit
- Stable source registry and provenance
- Immutable document versions with content hashes, retrieval metadata, and extraction status
- Changed content creates a new version; unchanged retrievals are logged without duplicate versions
- SQLite FTS5 current-version search by default, with optional historical search
- Search and document-list filters for current structured metadata keys and exact string values
- Idempotent schema migration, including legacy v0.1 document migration
- Typed entity registry and evidence-linked entity relationships with confidence and provenance
- Append-only source metadata revisions with explicit supersession history
- STEM Wiki with immutable page revisions, full-text search, discipline/language filters, tags, references, and provenance
- Structured immutable STEM artifacts for equations, constants, algorithms, and experiments, with a JSON editor in the page view
- Provenance-aware knowledge graph connecting wiki revisions and artifacts with typed, append-only relationships and bounded traversal
- STEM artifact validation reports for missing definitions, units, sources, uncertainty, experiment controls, and dimensional checks for a documented subset of compound units
- Immutable, versioned STEM validation rule-set registry with report-level validator and rule-set versions
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
curl 'http://127.0.0.1:8000/search?q=archive&metadata_key=language&metadata_value=en'
curl 'http://127.0.0.1:8000/documents?metadata_key=reviewed&metadata_value=yes'
curl -X POST http://127.0.0.1:8000/wiki/pages -H 'Content-Type: application/json' -d '{"slug":"newton-second-law","title":"Newton Second Law","body":"Net force equals mass times acceleration. F = m a.","discipline":"physics","tags":["mechanics"]}'
curl 'http://127.0.0.1:8000/wiki/search?q=force&discipline=physics'
curl 'http://127.0.0.1:8000/wiki/pages/newton-second-law?include_history=true'
curl -X POST http://127.0.0.1:8000/wiki/pages/newton-second-law/artifacts -H 'Content-Type: application/json' -d '{"artifact_type":"equation","name":"Newton second law","data":{"expression":"F = m * a","variables":[{"symbol":"F","meaning":"net force","unit":"N"},{"symbol":"m","meaning":"mass","unit":"kg"},{"symbol":"a","meaning":"acceleration","unit":"m/s^2"}]}}'
curl 'http://127.0.0.1:8000/wiki/pages/newton-second-law/artifacts?artifact_type=equation'
curl -X POST http://127.0.0.1:8000/wiki/edges -H 'Content-Type: application/json' -d '{"subject_kind":"page","subject_id":1,"predicate":"includes equation","object_kind":"artifact","object_id":1,"status":"proposed","confidence":0.9}'
curl 'http://127.0.0.1:8000/wiki/graph/page/1?depth=2'
curl 'http://127.0.0.1:8000/wiki/validation-rule-sets'
curl -X POST http://127.0.0.1:8000/wiki/artifacts/1/validate
curl 'http://127.0.0.1:8000/wiki/artifacts/1/validation-reports'
curl 'http://127.0.0.1:8000/sources'
curl 'http://127.0.0.1:8000/documents?include_history=true'
```

## Safety and scope
Submit only sources you are authorized to retrieve; respect site terms, robots policies, rate limits, and copyright. The prototype blocks redirects, limits downloads, and only extracts HTML, but these controls do not eliminate all DNS-rebinding or network-level risks. Deploy behind egress controls for untrusted users. This is not a general crawler and does not bypass authentication or paywalls. Before public multi-user deployment, add authentication, rate limiting, stricter egress controls, and operational monitoring.

## Roadmap
1. Source registry, immutable versions, and ingestion logs — implemented in v0.2.0
2. Typed entity registry, relationships, and append-only metadata — implemented in v0.3.0
3. Structured metadata filters for search and document listing — implemented in v0.4.0
4. STEM Wiki — implemented in v0.5.0
5. Structured STEM artifacts — implemented in v0.6.0
6. STEM validation reports — implemented in v0.8.0 (screening aid; not a proof of correctness)
7. Versioned validation rules and limited compound-unit dimensional checks — implemented in v0.9.0
8. PostgreSQL migration, archive connectors, language detection, timelines, and evidence-conflict views
