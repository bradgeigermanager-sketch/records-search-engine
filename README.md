# Records Search Engine

A provenance-preserving search engine with a versioned STEM Wiki, typed STEM artifacts, structured metadata filters, typed entities, and evidence-aware relationships.

## Features
- FastAPI JSON API and browser UI
- HTTP/HTTPS HTML ingestion with a 2 MB response limit
- GitHub repository text ingestion for public repositories or private repositories accessible to a configured `GITHUB_TOKEN`, preserving repository/ref/path/blob provenance
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


## Owner-wide GitHub search and cross-repository navigation

After indexing repositories with `POST /github/ingest-owner`, use:

- `GET /github/search?q=provenance&owner=YOUR_OWNER` to search indexed GitHub files across repositories.
- Add `repository=REPO_NAME` to narrow results, or `include_history=true` to search older file versions.
- `GET /github/repositories?owner=YOUR_OWNER` to list indexed repositories, indexed-file counts, and navigation links.
- `GET /github/repositories/OWNER/REPOSITORY` to browse indexed files with GitHub file URLs and local source/version-history links.
- `POST /github/repository-relationships` to record an explicit cross-repository relationship between two indexed file sources. Body fields: `from_source_id`, `to_source_id`, `predicate`, and optional `confidence`, `status`, `provenance`.
- `GET /github/repository-relationships?owner=YOUR_OWNER` to list those relationships with both endpoints' URLs and ingestion provenance.

Cross-repository relationships are **explicit assertions**, not automatically inferred facts. They are append-only and immutable; use statuses `proposed`, `supported`, `disputed`, or `rejected` to preserve review state without silently rewriting history. Search and browse results expose local source and version-history navigation so a result can be traced back to its indexed file.

## GitHub repository ingestion
`POST /github/ingest-repository` reads a bounded set of text files from a repository tree. By default it includes Markdown, plain text, source code, JSON/YAML, SQL, HTML/XML, TOML, and CSV files; binary files, unsupported extensions, common dependency/build directories, and files over 2 MB are skipped. `max_files` is limited to 100 per request. Each indexed file is stored through the existing immutable document-version pipeline, with repository, ref, path, Git blob SHA, and source URL in provenance. Re-ingesting unchanged content does not create a duplicate document version. Set `GITHUB_TOKEN` in the deployment environment to access private repositories allowed by that token; never send the token in request JSON. This first iteration is manually triggered, not an unattended full-account crawler.

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


## GitHub repository ingestion
`POST /github/ingest-repository` reads a bounded set of text files from one repository. `POST /github/ingest-owner` scans up to 20 owned repositories per request and up to 50 files per repository, excluding forks and archived repositories. Each indexed file stores repository, ref, path, Git blob SHA, and source URL in provenance. Re-ingesting unchanged content does not create a duplicate document version. Public repositories are supported without credentials; private repositories require a deployment-configured GitHub access token with permission to read them. Do not pass credentials in request JSON. This first iteration is manually triggered, not an unattended background crawler.


## Cross-repository relationship candidate discovery

Use `GET /github/relationship-candidates?owner=YOUR_OWNER` to generate a bounded shortlist of potential links between indexed files in different repositories. The detector looks for explicit GitHub blob links, shared structured identifiers (such as ticket IDs, UUIDs, and DOI-like identifiers), import/dependency names that match file paths, and shared distinctive terms.

Optional parameters:
- `max_sources` (2–500, default 300): maximum current file sources to examine.
- `min_score` (1–100, default 10): minimum heuristic score to return.
- `limit` (1–200, default 50): maximum candidate links returned.

Every candidate includes its two file endpoints, score, evidence categories, and a suggested relationship predicate. Results are labeled `candidate_only` and require review. The detector does **not** automatically create graph edges or save candidate relationships. After reviewing a candidate, use `POST /github/repository-relationships` to record an explicit, provenance-linked assertion when justified. The current scan is bounded and examines pairs within the selected current-source set; if `scan_truncated` is true, increase `max_sources` or index/filter the repositories more narrowly. A heuristic score is a ranking aid, not a probability or proof.


## Relationship review and graph

- `POST /github/relationship-reviews` records an immutable review decision. Use `decision=accepted`, `rejected`, or `disputed`, and provide reviewer, rationale, confidence, evidence snapshot, and provenance as appropriate.
- An accepted review creates a `supported` cross-repository relationship. Rejected or disputed reviews remain in the review log and do not create a supported graph edge.
- `GET /github/relationship-reviews?owner=YOUR_OWNER` lists decisions; add `decision=accepted|rejected|disputed` to filter.
- `GET /github/relationship-graph?owner=YOUR_OWNER` returns a JSON graph with file/source nodes and explicit stored relationship edges. By default, it includes supported edges only. Add `include_proposed=true` to include proposed and disputed edges too.
- Graph nodes include source and version-history navigation. Edge provenance records the reviewed source versions and the review rationale/evidence snapshot when the edge was accepted.

Review decisions and relationship records are append-only. A later disagreement should be recorded as another review, not as an edit to an earlier decision. The graph API returns data for a visualization client; it does not itself draw an interactive graph.

## Relationship evidence change detection

When an indexed source receives a new content version, the system appends an immutable change event for each stored relationship touching that source. The event records the previous and current version IDs and content hashes, the version captured by the relationship review when available, and a recommendation for human revalidation. It does **not** automatically reject, delete, or silently rewrite the relationship.

- `GET /github/relationship-change-events?owner=YOUR_OWNER` lists change events involving the owner's repositories.
- Add `relationship_id=123` or `source_id=456` to narrow the results.
- Events are append-only and duplicate detection is guarded by a unique relationship/source/current-version key.
- An unchanged re-ingestion does not create a new document version or a relationship change event.

These events indicate that evidence may be stale; they do not establish that a relationship is false. The next review workflow can record a fresh decision against the new exact versions.
