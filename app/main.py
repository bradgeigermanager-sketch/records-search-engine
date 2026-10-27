"""Records Search Engine — source registry and immutable content versions."""
from __future__ import annotations
import base64, difflib, hashlib, ipaddress, json, math, os, re, socket, sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.parse import urlparse, urldefrag, urlunparse, quote
import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, HttpUrl

APP_NAME = "Records Search Engine"
DB_PATH = os.getenv("SEARCH_DB_PATH", "search.db")
MAX_BYTES = 2_000_000
TIMEOUT_SECONDS = 12
USER_AGENT = "RecordsSearchEngine/0.11 (+responsible research; contact site operator before bulk use)"
app = FastAPI(title=APP_NAME, version="0.16.0", description="Provenance-preserving web-record search prototype")

class IngestRequest(BaseModel):
    url: HttpUrl

class GitHubRepositoryIngestRequest(BaseModel):
    owner: str
    repository: str
    ref: str | None = None
    max_files: int = 50
    include_extensions: list[str] = [".md", ".txt", ".rst", ".py", ".json", ".yaml", ".yml", ".toml", ".sql", ".html", ".xml", ".csv"]

class GitHubOwnerIngestRequest(BaseModel):
    owner: str
    max_repositories: int = 10
    max_files_per_repository: int = 20
class EntityCreate(BaseModel):
    canonical_name: str
    entity_type: str
    description: str = ""
    aliases: list[str] = []
    provenance: dict = {}

class MetadataCreate(BaseModel):
    key: str
    value: object
    provenance: dict = {}

class RelationshipCreate(BaseModel):
    subject_entity_id: int
    predicate: str
    object_entity_id: int | None = None
    object_literal: object | None = None
    source_id: int | None = None
    evidence_version_id: int | None = None
    confidence: float | None = None
    provenance: dict = {}

class RepositoryRelationshipReviewCreate(BaseModel):
    from_source_id: int
    to_source_id: int
    decision: str
    predicate: str = "possibly_related_to"
    reviewer: str = "unspecified"
    rationale: str = ""
    confidence: float | None = None
    evidence_snapshot: list[dict] = []
    provenance: dict = {}

class RepositoryRelationshipRevalidationCreate(BaseModel):
    event_id: int
    decision: str
    reviewer: str = "unspecified"
    rationale: str
    predicate: str | None = None
    confidence: float | None = None
    evidence_snapshot: list[dict] = []
    provenance: dict = {}

class RepositoryRelationshipCreate(BaseModel):
    from_source_id: int
    to_source_id: int
    predicate: str
    confidence: float | None = None
    status: str = "proposed"
    provenance: dict = {}

class WikiPageCreate(BaseModel):
    slug: str
    title: str
    body: str
    discipline: str
    summary: str = ""
    language: str = "en"
    tags: list[str] = []
    references: list[dict] = []
    author: str = ""
    change_note: str = "Initial page"
    provenance: dict = {}

class WikiRevisionCreate(BaseModel):
    title: str
    body: str
    discipline: str
    summary: str = ""
    language: str = "en"
    tags: list[str] = []
    references: list[dict] = []
    author: str = ""
    change_note: str = ""
    provenance: dict = {}

class STEMArtifactCreate(BaseModel):
    artifact_type: str
    name: str
    data: dict
    provenance: dict = {}

class KnowledgeEdgeCreate(BaseModel):
    subject_kind: str
    subject_id: int
    predicate: str
    object_kind: str
    object_id: int
    evidence_page_revision_id: int | None = None
    confidence: float | None = None
    status: str = "proposed"
    provenance: dict = {}

class SearchResult(BaseModel):
    document_id: int
    version_id: int
    source_id: int
    version_number: int
    title: str
    url: str
    excerpt: str
    retrieved_at: str
    content_hash: str
    is_current: bool

def utc_now():
    return datetime.now(timezone.utc).isoformat()

def canonicalize_url(raw_url: str) -> str:
    parts = urlparse(raw_url)
    if parts.scheme not in {"http", "https"}:
        raise ValueError("Only http and https URLs are supported")
    if not parts.hostname or parts.username or parts.password:
        raise ValueError("URL must have a hostname and must not contain credentials")
    host = parts.hostname.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host + (f":{parts.port}" if parts.port else "")
    return urldefrag(urlunparse((parts.scheme.lower(), netloc, parts.path or "/", parts.params, parts.query, "")))[0]

def validate_public_host(url: str) -> None:
    parsed = urlparse(url)
    host = parsed.hostname
    if not host or host.lower() in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise ValueError("Local hosts are not permitted")
    try:
        answers = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError("Hostname could not be resolved") from exc
    if not answers or any(not ipaddress.ip_address(a[4][0]).is_global for a in answers):
        raise ValueError("Only publicly routable hosts are permitted")

@contextmanager
def connection():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def _table_exists(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None

def init_db():
    with connection() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
        applied = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
        if 1 not in applied:
            legacy = _table_exists(conn, "documents")
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS sources(id INTEGER PRIMARY KEY, canonical_url TEXT NOT NULL UNIQUE, source_type TEXT NOT NULL DEFAULT 'web_page', first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, provenance_json TEXT NOT NULL DEFAULT '{}', current_version_id INTEGER, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS document_versions(id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL REFERENCES sources(id), version_number INTEGER NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL, content_hash TEXT NOT NULL, retrieved_at TEXT NOT NULL, http_status INTEGER NOT NULL, content_type TEXT NOT NULL, extraction_status TEXT NOT NULL, provenance_json TEXT NOT NULL DEFAULT '{}', UNIQUE(source_id,version_number));
            CREATE TABLE IF NOT EXISTS ingestion_events(id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL REFERENCES sources(id), occurred_at TEXT NOT NULL, outcome TEXT NOT NULL, content_hash TEXT, version_id INTEGER REFERENCES document_versions(id), http_status INTEGER, content_type TEXT, extraction_status TEXT NOT NULL, details_json TEXT NOT NULL DEFAULT '{}');
            CREATE INDEX IF NOT EXISTS idx_versions_source_time ON document_versions(source_id,retrieved_at DESC);
            CREATE INDEX IF NOT EXISTS idx_events_source_time ON ingestion_events(source_id,occurred_at DESC);
            """)
            if legacy:
                cols = {r[1] for r in conn.execute("PRAGMA table_info(documents)")}
                needed = {"canonical_url","title","body","content_hash","retrieved_at","http_status","content_type"}
                if needed <= cols:
                    for row in conn.execute("SELECT canonical_url,title,body,content_hash,retrieved_at,http_status,content_type FROM documents").fetchall():
                        at = row["retrieved_at"] or utc_now()
                        conn.execute("INSERT OR IGNORE INTO sources(canonical_url,first_seen_at,last_seen_at,created_at,provenance_json) VALUES(?,?,?,?,?)", (row["canonical_url"],at,at,at,'{"migration":"legacy_documents"}'))
                        src = conn.execute("SELECT id FROM sources WHERE canonical_url=?", (row["canonical_url"],)).fetchone()
                        if src and not conn.execute("SELECT 1 FROM document_versions WHERE source_id=?", (src["id"],)).fetchone():
                            cur = conn.execute("INSERT INTO document_versions(source_id,version_number,title,body,content_hash,retrieved_at,http_status,content_type,extraction_status,provenance_json) VALUES(?,?,?,?,?,?,?,?,?,?)", (src["id"],1,row["title"],row["body"],row["content_hash"],at,row["http_status"],row["content_type"],"success",'{"migration":"legacy_documents"}'))
                            conn.execute("UPDATE sources SET current_version_id=? WHERE id=?", (cur.lastrowid,src["id"]))
                            conn.execute("INSERT INTO ingestion_events(source_id,occurred_at,outcome,content_hash,version_id,http_status,content_type,extraction_status,details_json) VALUES(?,?,?,?,?,?,?,?,?)", (src["id"],at,"migrated",row["content_hash"],cur.lastrowid,row["http_status"],row["content_type"],"success",'{"migration":"legacy_documents"}'))
            conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS versions_fts USING fts5(title,body,content='document_versions',content_rowid='id',tokenize='unicode61')")
            conn.executescript("""
            CREATE TRIGGER IF NOT EXISTS versions_ai AFTER INSERT ON document_versions BEGIN INSERT INTO versions_fts(rowid,title,body) VALUES(new.id,new.title,new.body); END;
            CREATE TRIGGER IF NOT EXISTS versions_ad AFTER DELETE ON document_versions BEGIN INSERT INTO versions_fts(versions_fts,rowid,title,body) VALUES('delete',old.id,old.title,old.body); END;
            CREATE TRIGGER IF NOT EXISTS document_versions_no_update BEFORE UPDATE ON document_versions BEGIN SELECT RAISE(ABORT,'document versions are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS document_versions_no_delete BEFORE DELETE ON document_versions BEGIN SELECT RAISE(ABORT,'document versions are immutable'); END;
            """)
            conn.execute("INSERT INTO versions_fts(versions_fts) VALUES('rebuild')")
            conn.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(1,?)", (utc_now(),))
        else:
            conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS versions_fts USING fts5(title,body,content='document_versions',content_rowid='id',tokenize='unicode61')")
            conn.executescript("""
            CREATE TRIGGER IF NOT EXISTS versions_ai AFTER INSERT ON document_versions BEGIN INSERT INTO versions_fts(rowid,title,body) VALUES(new.id,new.title,new.body); END;
            CREATE TRIGGER IF NOT EXISTS versions_ad AFTER DELETE ON document_versions BEGIN INSERT INTO versions_fts(versions_fts,rowid,title,body) VALUES('delete',old.id,old.title,old.body); END;
            CREATE TRIGGER IF NOT EXISTS document_versions_no_update BEFORE UPDATE ON document_versions BEGIN SELECT RAISE(ABORT,'document versions are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS document_versions_no_delete BEFORE DELETE ON document_versions BEGIN SELECT RAISE(ABORT,'document versions are immutable'); END;
            """)
        if 2 not in applied:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS entities (
                id INTEGER PRIMARY KEY,
                canonical_name TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                aliases_json TEXT NOT NULL DEFAULT '[]',
                provenance_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(canonical_name);
            CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(entity_type);
            CREATE TABLE IF NOT EXISTS source_metadata (
                id INTEGER PRIMARY KEY,
                source_id INTEGER NOT NULL REFERENCES sources(id),
                metadata_key TEXT NOT NULL,
                value_json TEXT NOT NULL,
                provenance_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                supersedes_id INTEGER REFERENCES source_metadata(id)
            );
            CREATE INDEX IF NOT EXISTS idx_metadata_source_key_time
                ON source_metadata(source_id, metadata_key, created_at DESC, id DESC);
            CREATE TABLE IF NOT EXISTS entity_relationships (
                id INTEGER PRIMARY KEY,
                subject_entity_id INTEGER NOT NULL REFERENCES entities(id),
                predicate TEXT NOT NULL,
                object_entity_id INTEGER REFERENCES entities(id),
                object_literal_json TEXT,
                source_id INTEGER REFERENCES sources(id),
                evidence_version_id INTEGER REFERENCES document_versions(id),
                confidence REAL,
                provenance_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                CHECK ((object_entity_id IS NOT NULL AND object_literal_json IS NULL)
                    OR (object_entity_id IS NULL AND object_literal_json IS NOT NULL)),
                CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1))
            );
            CREATE INDEX IF NOT EXISTS idx_relationship_subject ON entity_relationships(subject_entity_id);
            CREATE INDEX IF NOT EXISTS idx_relationship_object ON entity_relationships(object_entity_id);
            """)
            conn.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(2,?)", (utc_now(),))
        if 3 not in applied:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS wiki_pages (
                id INTEGER PRIMARY KEY,
                slug TEXT NOT NULL UNIQUE,
                current_revision_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS wiki_page_revisions (
                id INTEGER PRIMARY KEY,
                page_id INTEGER NOT NULL REFERENCES wiki_pages(id),
                revision_number INTEGER NOT NULL,
                title TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                body TEXT NOT NULL,
                language TEXT NOT NULL DEFAULT 'en',
                discipline TEXT NOT NULL,
                tags_json TEXT NOT NULL DEFAULT '[]',
                references_json TEXT NOT NULL DEFAULT '[]',
                author TEXT NOT NULL DEFAULT '',
                change_note TEXT NOT NULL DEFAULT '',
                provenance_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                UNIQUE(page_id, revision_number)
            );
            CREATE INDEX IF NOT EXISTS idx_wiki_revisions_page ON wiki_page_revisions(page_id, revision_number DESC);
            CREATE INDEX IF NOT EXISTS idx_wiki_pages_updated ON wiki_pages(updated_at DESC);
            CREATE VIRTUAL TABLE IF NOT EXISTS wiki_fts USING fts5(slug, title, summary, body, tags, tokenize='unicode61');
            CREATE TRIGGER IF NOT EXISTS wiki_revisions_no_update BEFORE UPDATE ON wiki_page_revisions BEGIN SELECT RAISE(ABORT,'wiki revisions are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS wiki_revisions_no_delete BEFORE DELETE ON wiki_page_revisions BEGIN SELECT RAISE(ABORT,'wiki revisions are immutable'); END;
            """)
            conn.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(3,?)", (utc_now(),))
        if 4 not in applied:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS stem_artifacts (
                id INTEGER PRIMARY KEY,
                page_id INTEGER NOT NULL REFERENCES wiki_pages(id),
                page_revision_id INTEGER NOT NULL REFERENCES wiki_page_revisions(id),
                artifact_type TEXT NOT NULL,
                name TEXT NOT NULL,
                data_json TEXT NOT NULL,
                provenance_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_stem_artifacts_page ON stem_artifacts(page_id, artifact_type, created_at DESC);
            CREATE TRIGGER IF NOT EXISTS stem_artifacts_no_update BEFORE UPDATE ON stem_artifacts BEGIN SELECT RAISE(ABORT,'STEM artifacts are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS stem_artifacts_no_delete BEFORE DELETE ON stem_artifacts BEGIN SELECT RAISE(ABORT,'STEM artifacts are immutable'); END;
            """)
            conn.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(4,?)", (utc_now(),))
        if 5 not in applied:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS knowledge_edges (
                id INTEGER PRIMARY KEY,
                subject_kind TEXT NOT NULL CHECK(subject_kind IN ('page','artifact')),
                subject_id INTEGER NOT NULL,
                predicate TEXT NOT NULL,
                object_kind TEXT NOT NULL CHECK(object_kind IN ('page','artifact')),
                object_id INTEGER NOT NULL,
                evidence_page_revision_id INTEGER REFERENCES wiki_page_revisions(id),
                confidence REAL CHECK(confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
                status TEXT NOT NULL DEFAULT 'proposed' CHECK(status IN ('proposed','supported','disputed','rejected')),
                provenance_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_knowledge_edges_subject ON knowledge_edges(subject_kind,subject_id,predicate);
            CREATE INDEX IF NOT EXISTS idx_knowledge_edges_object ON knowledge_edges(object_kind,object_id,predicate);
            CREATE TRIGGER IF NOT EXISTS knowledge_edges_no_update BEFORE UPDATE ON knowledge_edges BEGIN SELECT RAISE(ABORT,'knowledge edges are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS knowledge_edges_no_delete BEFORE DELETE ON knowledge_edges BEGIN SELECT RAISE(ABORT,'knowledge edges are immutable'); END;
            """)
            conn.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(5,?)", (utc_now(),))
        if 6 not in applied:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS stem_validation_reports (
                id INTEGER PRIMARY KEY,
                artifact_id INTEGER NOT NULL REFERENCES stem_artifacts(id),
                validator_version TEXT NOT NULL,
                outcome TEXT NOT NULL CHECK(outcome IN ('pass','review','fail')),
                findings_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_stem_validation_artifact ON stem_validation_reports(artifact_id,created_at DESC);
            CREATE TRIGGER IF NOT EXISTS stem_validation_no_update BEFORE UPDATE ON stem_validation_reports BEGIN SELECT RAISE(ABORT,'validation reports are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS stem_validation_no_delete BEFORE DELETE ON stem_validation_reports BEGIN SELECT RAISE(ABORT,'validation reports are immutable'); END;
            """)
            conn.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(6,?)", (utc_now(),))
        if 7 not in applied:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS stem_validation_rule_sets (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, version TEXT NOT NULL,
                rules_json TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(name,version)
            );
            CREATE TRIGGER IF NOT EXISTS stem_validation_rule_sets_no_update BEFORE UPDATE ON stem_validation_rule_sets BEGIN SELECT RAISE(ABORT,'validation rule sets are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS stem_validation_rule_sets_no_delete BEFORE DELETE ON stem_validation_rule_sets BEGIN SELECT RAISE(ABORT,'validation rule sets are immutable'); END;
            """)
            conn.execute("""INSERT OR IGNORE INTO stem_validation_rule_sets(name,version,rules_json,created_at) VALUES(?,?,?,?)""",
                ("stem-core","0.2",json.dumps({"equation":["expression_required","variables_defined","coarse_dimensional_check"],"constant":["value_finite","unit_required","source_review","uncertainty_review"],"algorithm":["pseudocode_required","interface_review","preconditions_review","complexity_review"],"experiment":["procedure_complete","variables_defined","controls_review","replication_review","uncertainty_review","safety_review"]}, sort_keys=True),utc_now()))
            conn.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(7,?)", (utc_now(),))

        if 9 not in applied:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS repository_relationship_reviews (
                id INTEGER PRIMARY KEY,
                from_source_id INTEGER NOT NULL REFERENCES sources(id),
                to_source_id INTEGER NOT NULL REFERENCES sources(id),
                decision TEXT NOT NULL CHECK(decision IN ('accepted','rejected','disputed')),
                predicate TEXT NOT NULL,
                reviewer TEXT NOT NULL,
                rationale TEXT NOT NULL DEFAULT '',
                confidence REAL CHECK(confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
                evidence_snapshot_json TEXT NOT NULL DEFAULT '[]',
                provenance_json TEXT NOT NULL DEFAULT '{}',
                resulting_relationship_id INTEGER REFERENCES repository_relationships(id),
                created_at TEXT NOT NULL,
                CHECK(from_source_id <> to_source_id)
            );
            CREATE INDEX IF NOT EXISTS idx_repo_reviews_pair ON repository_relationship_reviews(from_source_id,to_source_id,created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_repo_reviews_decision ON repository_relationship_reviews(decision,created_at DESC);
            CREATE TRIGGER IF NOT EXISTS repository_relationship_reviews_no_update BEFORE UPDATE ON repository_relationship_reviews BEGIN SELECT RAISE(ABORT,'relationship reviews are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS repository_relationship_reviews_no_delete BEFORE DELETE ON repository_relationship_reviews BEGIN SELECT RAISE(ABORT,'relationship reviews are immutable'); END;
            """)
            conn.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(9,?)", (utc_now(),))

        if 8 not in applied:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS repository_relationships (
                id INTEGER PRIMARY KEY,
                from_source_id INTEGER NOT NULL REFERENCES sources(id),
                to_source_id INTEGER NOT NULL REFERENCES sources(id),
                predicate TEXT NOT NULL,
                confidence REAL CHECK(confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
                status TEXT NOT NULL DEFAULT 'proposed' CHECK(status IN ('proposed','supported','disputed','rejected')),
                provenance_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                CHECK(from_source_id <> to_source_id)
            );
            CREATE INDEX IF NOT EXISTS idx_repo_relationship_from ON repository_relationships(from_source_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_repo_relationship_to ON repository_relationships(to_source_id, created_at DESC);
            CREATE TRIGGER IF NOT EXISTS repository_relationships_no_update BEFORE UPDATE ON repository_relationships BEGIN SELECT RAISE(ABORT,'repository relationships are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS repository_relationships_no_delete BEFORE DELETE ON repository_relationships BEGIN SELECT RAISE(ABORT,'repository relationships are immutable'); END;
            """)
            conn.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(8,?)", (utc_now(),))

        if 11 not in applied:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS relationship_revalidations (
                id INTEGER PRIMARY KEY,
                event_id INTEGER NOT NULL REFERENCES relationship_change_events(id),
                relationship_id INTEGER NOT NULL REFERENCES repository_relationships(id),
                decision TEXT NOT NULL CHECK(decision IN ('reaffirmed','revised','disputed','no_longer_supported')),
                reviewer TEXT NOT NULL,
                rationale TEXT NOT NULL,
                replacement_relationship_id INTEGER REFERENCES repository_relationships(id),
                evidence_snapshot_json TEXT NOT NULL DEFAULT '[]',
                provenance_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                UNIQUE(event_id)
            );
            CREATE INDEX IF NOT EXISTS idx_relationship_revalidations_relationship ON relationship_revalidations(relationship_id,created_at DESC);
            CREATE TRIGGER IF NOT EXISTS relationship_revalidations_no_update BEFORE UPDATE ON relationship_revalidations BEGIN SELECT RAISE(ABORT,'relationship revalidations are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS relationship_revalidations_no_delete BEFORE DELETE ON relationship_revalidations BEGIN SELECT RAISE(ABORT,'relationship revalidations are immutable'); END;
            """)
            conn.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(11,?)", (utc_now(),))

        if 10 not in applied:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS relationship_change_events (
                id INTEGER PRIMARY KEY,
                relationship_id INTEGER NOT NULL REFERENCES repository_relationships(id),
                source_id INTEGER NOT NULL REFERENCES sources(id),
                previous_version_id INTEGER NOT NULL REFERENCES document_versions(id),
                current_version_id INTEGER NOT NULL REFERENCES document_versions(id),
                previous_content_hash TEXT NOT NULL,
                current_content_hash TEXT NOT NULL,
                reviewed_version_id INTEGER,
                relationship_status TEXT NOT NULL,
                predicate TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(relationship_id,source_id,current_version_id)
            );
            CREATE INDEX IF NOT EXISTS idx_relationship_change_events_rel ON relationship_change_events(relationship_id,created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_relationship_change_events_source ON relationship_change_events(source_id,created_at DESC);
            CREATE TRIGGER IF NOT EXISTS relationship_change_events_no_update BEFORE UPDATE ON relationship_change_events BEGIN SELECT RAISE(ABORT,'relationship change events are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS relationship_change_events_no_delete BEFORE DELETE ON relationship_change_events BEGIN SELECT RAISE(ABORT,'relationship change events are immutable'); END;
            """)
            conn.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(10,?)", (utc_now(),))

@app.on_event("startup")
def startup():
    init_db()

def extract_page(html, fallback_title):
    soup = BeautifulSoup(html, "html.parser")
    for node in soup(["script","style","noscript","svg","nav","footer"]): node.decompose()
    title = soup.title.get_text(" ",strip=True) if soup.title else fallback_title
    main = soup.find("main") or soup.find("article") or soup.body or soup
    body = " ".join(main.get_text(" ",strip=True).split())
    return title[:500] or fallback_title, body[:1_000_000]

def retrieve_page(raw_url):
    url = canonicalize_url(str(raw_url)); validate_public_host(url)
    with httpx.Client(timeout=TIMEOUT_SECONDS,follow_redirects=False,headers={"User-Agent":USER_AGENT}) as client:
        with client.stream("GET",url) as response:
            if response.status_code in {301,302,303,307,308}: raise ValueError("Redirect encountered; submit the destination URL directly")
            response.raise_for_status()
            ctype = response.headers.get("content-type","").split(";",1)[0].strip().lower()
            if ctype not in {"text/html","application/xhtml+xml"}: raise ValueError(f"Unsupported content type: {ctype or 'unknown'}; HTML is required")
            chunks=[]; total=0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > MAX_BYTES: raise ValueError("Page exceeds the 2 MB retrieval limit")
                chunks.append(chunk)
            title,body = extract_page(b"".join(chunks).decode(response.encoding or "utf-8",errors="replace"),url)
            if not body: raise ValueError("No searchable text could be extracted from this page")
            return title,body,response.status_code,ctype

def save_document(url,title,body,status,content_type,provenance=None,extraction_status="success"):
    url=canonicalize_url(url); now=utc_now(); digest=hashlib.sha256(body.encode()).hexdigest()
    provenance=provenance or {"method":"direct_url_ingestion","submitted_url":url}
    pjson=json.dumps(provenance,ensure_ascii=False,sort_keys=True)
    with connection() as conn:
        conn.execute("INSERT OR IGNORE INTO sources(canonical_url,first_seen_at,last_seen_at,created_at,provenance_json) VALUES(?,?,?,?,?)",(url,now,now,now,pjson))
        src=conn.execute("SELECT * FROM sources WHERE canonical_url=?",(url,)).fetchone(); sid=src["id"]
        cur=conn.execute("SELECT * FROM document_versions WHERE id=?",(src["current_version_id"],)).fetchone() if src["current_version_id"] else None
        if cur and cur["content_hash"]==digest:
            vid=cur["id"]; vnum=cur["version_number"]; outcome="unchanged"
        else:
            vnum=cur["version_number"]+1 if cur else 1
            ins=conn.execute("INSERT INTO document_versions(source_id,version_number,title,body,content_hash,retrieved_at,http_status,content_type,extraction_status,provenance_json) VALUES(?,?,?,?,?,?,?,?,?,?)",(sid,vnum,title,body,digest,now,status,content_type,extraction_status,pjson))
            vid=ins.lastrowid; conn.execute("UPDATE sources SET current_version_id=? WHERE id=?",(vid,sid)); outcome="created" if cur is None else "changed"
        if cur and outcome == "changed":
            # Preserve an audit event for every relationship touching a source whose current version changed.
            related = conn.execute("""SELECT id,from_source_id,to_source_id,predicate,status,provenance_json
                FROM repository_relationships WHERE from_source_id=? OR to_source_id=? ORDER BY id""", (sid, sid)).fetchall()
            for rel in related:
                rel_provenance = json.loads(rel["provenance_json"] or "{}")
                version_snapshot = rel_provenance.get("source_versions") or {}
                side = "from" if rel["from_source_id"] == sid else "to"
                expected_version_id = version_snapshot.get(side)
                conn.execute("""INSERT OR IGNORE INTO relationship_change_events
                    (relationship_id,source_id,previous_version_id,current_version_id,previous_content_hash,current_content_hash,
                     reviewed_version_id,relationship_status,predicate,reason,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (rel["id"], sid, cur["id"], vid, cur["content_hash"], digest, expected_version_id,
                     rel["status"], rel["predicate"],
                     "Source content changed after relationship creation/review; human revalidation recommended.", now))
        conn.execute("UPDATE sources SET last_seen_at=?,provenance_json=? WHERE id=?",(now,pjson,sid))
        conn.execute("INSERT INTO ingestion_events(source_id,occurred_at,outcome,content_hash,version_id,http_status,content_type,extraction_status,details_json) VALUES(?,?,?,?,?,?,?,?,?)",(sid,now,outcome,digest,vid,status,content_type,extraction_status,pjson))
        return {"source_id":sid,"document_id":vid,"version_id":vid,"version_number":vnum,"title":title,"url":url,"retrieved_at":now,"content_hash":digest,"outcome":outcome,"is_current":True,"extraction_status":extraction_status}

@app.get("/health")
def health(): return {"status":"ok","service":APP_NAME,"version":app.version}

GITHUB_API = "https://api.github.com"
GITHUB_FILE_LIMIT = 2_000_000

def _github_headers():
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "RecordsSearchEngine/0.9"}
    token = os.getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers

def _github_get_json(url: str):
    response = httpx.get(url, headers=_github_headers(), timeout=TIMEOUT_SECONDS, follow_redirects=False)
    if response.status_code in (401, 403):
        raise HTTPException(status_code=502, detail="GitHub denied access. Check repository visibility, token permissions, or API rate limits.")
    if response.status_code == 404:
        raise HTTPException(status_code=404, detail="GitHub repository, ref, or file was not found or is not accessible to the configured token.")
    response.raise_for_status()
    return response.json()

def _github_text_blob(blob_url: str):
    payload = _github_get_json(blob_url)
    if payload.get("encoding") != "base64" or not isinstance(payload.get("content"), str):
        return None
    try:
        content = base64.b64decode(payload["content"], validate=False)
        return content.decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None

@app.post("/github/ingest-owner")
def ingest_github_owner(request: GitHubOwnerIngestRequest):
    owner = request.owner.strip()
    if not owner or any(not (c.isalnum() or c in "-_") for c in owner):
        raise HTTPException(status_code=422, detail="owner must be a GitHub username or organization name")
    if request.max_repositories < 1 or request.max_repositories > 20:
        raise HTTPException(status_code=422, detail="max_repositories must be between 1 and 20")
    if request.max_files_per_repository < 1 or request.max_files_per_repository > 50:
        raise HTTPException(status_code=422, detail="max_files_per_repository must be between 1 and 50")
    if os.getenv("GITHUB_TOKEN"):
        repositories = _github_get_json(f"{GITHUB_API}/user/repos?affiliation=owner&per_page=100&sort=updated")
        repositories = [item for item in repositories if (item.get("owner") or {}).get("login", "").lower() == owner.lower()]
    else:
        repositories = _github_get_json(f"{GITHUB_API}/users/{quote(owner, safe='')}/repos?type=owner&per_page=100&sort=updated")
    if not isinstance(repositories, list):
        raise HTTPException(status_code=502, detail="GitHub repository listing returned an unexpected response")
    repositories = [item for item in repositories if not item.get("fork") and not item.get("archived") and item.get("permissions", {}).get("pull", True)]
    repositories = repositories[:request.max_repositories]
    results, failures = [], []
    for item in repositories:
        try:
            result = ingest_github_repository(GitHubRepositoryIngestRequest(owner=owner, repository=item["name"], max_files=request.max_files_per_repository))
            results.append({"repository": item["name"], "private": bool(item.get("private")), "result": result})
        except HTTPException as exc:
            failures.append({"repository": item.get("name", "unknown"), "status_code": exc.status_code, "reason": str(exc.detail)})
    return {"status": "completed", "owner": owner, "repositories_considered": len(repositories), "repositories_indexed": len(results), "repositories_failed": failures, "results": results, "note": "This bounded scan excludes forks and archived repositories. Private repositories are visible only when GITHUB_TOKEN grants access. Re-run this endpoint to detect updated files."}

@app.post("/github/ingest-repository")
def ingest_github_repository(request: GitHubRepositoryIngestRequest):
    owner = request.owner.strip()
    repository = request.repository.strip()
    if not owner or not repository or any(x in owner + repository for x in ("/", "..", " ")) or not all(c.isalnum() or c in "-_" for c in owner + repository):
        raise HTTPException(status_code=422, detail="owner and repository must be simple GitHub names, not paths or URLs")
    if request.max_files < 1 or request.max_files > 100:
        raise HTTPException(status_code=422, detail="max_files must be between 1 and 100")
    extensions = {("." + x.lstrip(".")).lower() for x in request.include_extensions if x.strip()}
    if not extensions or any(len(x) > 16 for x in extensions):
        raise HTTPException(status_code=422, detail="include_extensions must contain valid short file extensions")
    base = f"{GITHUB_API}/repos/{quote(owner, safe='')}/{quote(repository, safe='')}"
    repo_info = _github_get_json(base)
    ref = (request.ref or repo_info.get("default_branch") or "main").strip()
    if not ref or len(ref) > 250:
        raise HTTPException(status_code=422, detail="ref must be a non-empty branch, tag, or commit reference")
    tree = _github_get_json(f"{base}/git/trees/{quote(ref, safe='')}" + "?recursive=1")
    if tree.get("truncated"):
        raise HTTPException(status_code=413, detail="GitHub returned a truncated repository tree; narrow the ref or use smaller repositories")
    candidates = []
    skipped = {"binary_or_unsupported_extension": 0, "too_large": 0}
    for item in tree.get("tree", []):
        if item.get("type") != "blob":
            continue
        path = item.get("path", "")
        if not path or path.startswith("/") or any(part in (".git", "node_modules", "vendor", "__pycache__", ".venv") for part in path.split("/")):
            continue
        if os.path.splitext(path)[1].lower() not in extensions:
            skipped["binary_or_unsupported_extension"] += 1
            continue
        if item.get("size", 0) > GITHUB_FILE_LIMIT:
            skipped["too_large"] += 1
            continue
        candidates.append(item)
    candidates.sort(key=lambda item: item["path"].lower())
    selected = candidates[:request.max_files]
    indexed, failed = [], []
    for item in selected:
        try:
            body = _github_text_blob(f"{base}/git/blobs/{quote(item['sha'], safe='')}")
            if body is None:
                skipped["binary_or_unsupported_extension"] += 1
                continue
            if len(body.encode("utf-8")) > GITHUB_FILE_LIMIT:
                skipped["too_large"] += 1
                continue
            path = item["path"]
            file_url = f"https://github.com/{owner}/{repository}/blob/{quote(ref, safe='/')}/{quote(path, safe='/')}"
            provenance = {"method": "github_repository_ingestion", "owner": owner, "repository": repository, "ref": ref, "git_blob_sha": item["sha"], "path": path, "repository_url": repo_info.get("html_url"), "private_repository": bool(repo_info.get("private")), "api_endpoint": f"{base}/git/blobs/{item['sha']}"}
            saved = save_document(file_url, f"{owner}/{repository}: {path}", body, 200, "text/plain; charset=utf-8", provenance)
            indexed.append({**saved, "path": path, "characters_indexed": len(body)})
        except (httpx.HTTPError, ValueError, UnicodeDecodeError) as exc:
            failed.append({"path": item.get("path"), "reason": type(exc).__name__})
    return {"status": "completed", "owner": owner, "repository": repository, "ref": ref, "private_repository": bool(repo_info.get("private")), "files_considered": len(selected), "files_available": len(candidates), "files_indexed": len(indexed), "files_failed": failed, "skipped": skipped, "results": indexed, "note": "Files are indexed as immutable document versions; unchanged file content does not create a duplicate version. Configure GITHUB_TOKEN through the deployment environment for private repositories."}


@app.get("/github/search")
def github_search(q: str = Query(min_length=1, max_length=300),
                  owner: str | None = None, repository: str | None = None,
                  limit: int = Query(default=20, ge=1, le=100),
                  include_history: bool = False):
    """Search indexed GitHub files with repository and source-navigation metadata."""
    terms = [term.replace('"', ' ').strip() for term in q.split() if term.strip()]
    if not terms:
        return []
    match = " AND ".join('"' + term.replace('"', '') + '"' for term in terms)
    clauses, params = ["versions_fts MATCH ?", "json_extract(v.provenance_json,'$.method')='github_repository_ingestion'"], [match]
    if not include_history:
        clauses.append("s.current_version_id=v.id")
    if owner:
        clauses.append("lower(json_extract(v.provenance_json,'$.owner'))=lower(?)")
        params.append(owner.strip())
    if repository:
        clauses.append("lower(json_extract(v.provenance_json,'$.repository'))=lower(?)")
        params.append(repository.strip())
    params.append(limit)
    with connection() as conn:
        rows = conn.execute("""SELECT v.id version_id,v.source_id,v.version_number,v.title,s.canonical_url url,
            v.body,v.retrieved_at,v.content_hash,s.current_version_id,
            json_extract(v.provenance_json,'$.owner') owner,
            json_extract(v.provenance_json,'$.repository') repository,
            json_extract(v.provenance_json,'$.ref') ref,
            json_extract(v.provenance_json,'$.path') path,
            json_extract(v.provenance_json,'$.repository_url') repository_url,
            snippet(versions_fts,1,'<mark>','</mark>',' … ',24) excerpt
            FROM versions_fts JOIN document_versions v ON v.id=versions_fts.rowid
            JOIN sources s ON s.id=v.source_id WHERE """ + " AND ".join(clauses) +
            " ORDER BY bm25(versions_fts),v.retrieved_at DESC LIMIT ?", params).fetchall()
    return [{"source_id": r["source_id"], "version_id": r["version_id"], "version_number": r["version_number"],
             "title": r["title"], "url": r["url"], "excerpt": r["excerpt"] or r["body"][:240],
             "retrieved_at": r["retrieved_at"], "content_hash": r["content_hash"],
             "is_current": r["current_version_id"] == r["version_id"], "owner": r["owner"],
             "repository": r["repository"], "ref": r["ref"], "path": r["path"],
             "repository_url": r["repository_url"], "file_url": r["url"],
             "source_navigation": f"/sources/{r['source_id']}",
             "history_navigation": f"/sources/{r['source_id']}/versions"} for r in rows]


@app.get("/github/repositories")
def github_repositories(owner: str | None = None, limit: int = Query(default=100, ge=1, le=500)):
    """List indexed GitHub repositories with file counts and last-indexed times."""
    clauses = ["json_extract(s.provenance_json,'$.method')='github_repository_ingestion'"]
    params = []
    if owner:
        clauses.append("lower(json_extract(s.provenance_json,'$.owner'))=lower(?)")
        params.append(owner.strip())
    with connection() as conn:
        rows = conn.execute("""SELECT json_extract(s.provenance_json,'$.owner') owner,
            json_extract(s.provenance_json,'$.repository') repository,
            json_extract(s.provenance_json,'$.repository_url') repository_url,
            COUNT(DISTINCT s.id) file_count, MAX(s.last_seen_at) last_indexed_at,
            SUM(CASE WHEN s.current_version_id IS NOT NULL THEN 1 ELSE 0 END) current_file_count
            FROM sources s WHERE """ + " AND ".join(clauses) +
            " GROUP BY owner,repository,repository_url ORDER BY last_indexed_at DESC LIMIT ?", (*params, limit)).fetchall()
    return [{"owner": r["owner"], "repository": r["repository"], "repository_url": r["repository_url"],
             "file_count": r["file_count"], "current_file_count": r["current_file_count"],
             "last_indexed_at": r["last_indexed_at"],
             "files_navigation": f"/github/repositories/{quote(str(r['owner']), safe='')}/{quote(str(r['repository']), safe='')}"} for r in rows]


@app.get("/github/repositories/{owner}/{repository}")
def github_repository_files(owner: str, repository: str,
                            limit: int = Query(default=100, ge=1, le=500),
                            include_history: bool = False):
    """Browse indexed files in one repository, with source/version navigation."""
    clauses = ["json_extract(s.provenance_json,'$.method')='github_repository_ingestion'",
               "lower(json_extract(s.provenance_json,'$.owner'))=lower(?)",
               "lower(json_extract(s.provenance_json,'$.repository'))=lower(?)"]
    params = [owner, repository]
    if not include_history:
        clauses.append("s.current_version_id=v.id")
    params.append(limit)
    with connection() as conn:
        rows = conn.execute("""SELECT s.id source_id,v.id version_id,v.version_number,v.title,
            s.canonical_url file_url,json_extract(v.provenance_json,'$.ref') ref,
            json_extract(v.provenance_json,'$.path') path,v.content_hash,v.retrieved_at,
            (s.current_version_id=v.id) is_current
            FROM sources s JOIN document_versions v ON v.source_id=s.id
            WHERE """ + " AND ".join(clauses) + " ORDER BY path, v.version_number DESC LIMIT ?", params).fetchall()
    return [{"source_id": r["source_id"], "version_id": r["version_id"], "version_number": r["version_number"],
             "title": r["title"], "file_url": r["file_url"], "ref": r["ref"], "path": r["path"],
             "content_hash": r["content_hash"], "retrieved_at": r["retrieved_at"], "is_current": bool(r["is_current"]),
             "source_navigation": f"/sources/{r['source_id']}",
             "history_navigation": f"/sources/{r['source_id']}/versions"} for r in rows]



@app.get("/github/relationship-candidates")
def github_relationship_candidates(
    owner: str = Query(min_length=1, max_length=100),
    max_sources: int = Query(default=300, ge=2, le=500),
    min_score: int = Query(default=10, ge=1, le=100),
    limit: int = Query(default=50, ge=1, le=200),
):
    """Propose explainable cross-repository file links; never auto-assert or persist them."""
    owner = owner.strip()
    if not owner or any(c.isspace() for c in owner) or "/" in owner:
        raise HTTPException(status_code=422, detail="owner must be a GitHub owner name")
    with connection() as conn:
        rows = conn.execute("""SELECT s.id source_id,s.canonical_url file_url,s.provenance_json source_provenance,
            v.id version_id,v.title,v.body,v.content_hash,v.provenance_json version_provenance
            FROM sources s JOIN document_versions v ON v.id=s.current_version_id
            WHERE lower(json_extract(s.provenance_json,'$.method'))='github_repository_ingestion'
              AND lower(json_extract(s.provenance_json,'$.owner'))=lower(?)
            ORDER BY s.id LIMIT ?""", (owner, max_sources)).fetchall()
        total = conn.execute("""SELECT COUNT(*) FROM sources s WHERE
            lower(json_extract(s.provenance_json,'$.method'))='github_repository_ingestion'
            AND lower(json_extract(s.provenance_json,'$.owner'))=lower(?)""", (owner,)).fetchone()[0]
    documents = []
    for row in rows:
        source_meta = json.loads(row["source_provenance"] or "{}")
        version_meta = json.loads(row["version_provenance"] or "{}")
        meta = {**source_meta, **version_meta}
        body = row["body"] or ""
        repo_name = meta.get("repository", "")
        path = meta.get("path", "")
        # Normalize common identifiers, import/dependency names, and explicit GitHub file references.
        identifiers = set(re.findall(r"(?i)\b(?:[0-9a-f]{8}-[0-9a-f-]{27,}|[A-Z]{2,12}-\d{2,}|(?:doi:)?10\.\d{4,9}/[-._;()/:A-Z0-9]+)\b", body))
        identifiers = {x.lower().rstrip(".,;:)") for x in identifiers if len(x) >= 8}
        dependencies = set()
        for pattern in (
            r"(?m)^\s*(?:from|import)\s+([A-Za-z_][A-Za-z0-9_.-]*)",
            r"(?m)^\s*([A-Za-z0-9][A-Za-z0-9_.-]*)\s*(?:==|>=|<=|~=|!=|>|<)",
            r'(?m)^\s*["\']([A-Za-z_][A-Za-z0-9_.-]*)["\']\s*=\s*["\']',
        ):
            dependencies.update(x.lower().split(".")[0] for x in re.findall(pattern, body))
        explicit = set()
        for match in re.finditer(r"https?://github\.com/([^/\s?#]+)/([^/\s?#]+)/blob/[^/\s?#]+/([^\s?#)\"'<>]+)", body, re.I):
            link_owner, link_repo, link_path = match.groups()
            explicit.add((link_owner.lower(), link_repo.lower(), link_path.rstrip(".,;").lower()))
        terms = set(re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{4,}", (row["title"] or "") + " " + path + " " + body[:100_000].lower()))
        terms -= {"https", "httpsgithubcom", "false", "true", "return", "class", "function", "import", "from", "this", "that", "with", "which", "should", "their", "about", "these", "those", "using", "source", "file", "readme", "github", "owner", "repository"}
        documents.append({
            "source_id": row["source_id"], "version_id": row["version_id"], "file_url": row["file_url"],
            "repository": repo_name, "path": path, "title": row["title"], "content_hash": row["content_hash"],
            "ref": meta.get("ref"), "identifiers": identifiers, "dependencies": dependencies,
            "explicit": explicit, "terms": terms,
        })
    # Build exact cross-repository lookup for explicit GitHub blob links.
    path_index = {}
    for doc in documents:
        path_index[(owner.lower(), str(doc["repository"]).lower(), str(doc["path"]).lower())] = doc
    proposals = {}
    for i, left in enumerate(documents):
        for right in documents[i + 1:]:
            if str(left["repository"]).lower() == str(right["repository"]).lower():
                continue
            evidence = []
            score = 0
            left_key = (owner.lower(), str(left["repository"]).lower(), str(left["path"]).lower())
            right_key = (owner.lower(), str(right["repository"]).lower(), str(right["path"]).lower())
            if right_key in left["explicit"] or left_key in right["explicit"]:
                evidence.append({"type": "explicit_github_file_reference", "detail": "One file contains a direct GitHub blob URL to the other"})
                score += 100
            shared_ids = sorted(left["identifiers"] & right["identifiers"])
            if shared_ids:
                evidence.append({"type": "shared_identifier", "values": shared_ids[:10]})
                score += min(80, 35 + 15 * (len(shared_ids) - 1))
            shared_dependencies = sorted(left["dependencies"] & right["dependencies"])
            # Dependency signals only count when the shared token is distinctive and present in a file path/name.
            path_tokens = set(re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{3,}", left["path"] + " " + right["path"]))
            dependency_hits = sorted(set(shared_dependencies) & {x.lower() for x in path_tokens})
            if dependency_hits:
                evidence.append({"type": "dependency_or_import_name_matches_file_path", "values": dependency_hits[:10]})
                score += min(60, 30 + 10 * (len(dependency_hits) - 1))
            shared_terms = sorted(left["terms"] & right["terms"])
            # Avoid generic similarity claims: only report rare-ish terms with concrete names, not every shared word.
            if shared_terms:
                evidence.append({"type": "shared_distinctive_terms", "values": shared_terms[:12]})
                score += min(25, 5 * min(5, len(shared_terms)))
            if score < min_score:
                continue
            key = (left["source_id"], right["source_id"])
            proposals[key] = {
                "from_source_id": left["source_id"], "to_source_id": right["source_id"],
                "from": {"repository": left["repository"], "path": left["path"], "title": left["title"],
                         "file_url": left["file_url"], "version_id": left["version_id"]},
                "to": {"repository": right["repository"], "path": right["path"], "title": right["title"],
                       "file_url": right["file_url"], "version_id": right["version_id"]},
                "score": min(100, score), "status": "candidate_only",
                "suggested_predicate": "references" if any(e["type"] == "explicit_github_file_reference" for e in evidence) else "possibly_related_to",
                "evidence": evidence,
                "review_required": True,
            }
    results = sorted(proposals.values(), key=lambda item: (-item["score"], item["from_source_id"], item["to_source_id"]))[:limit]
    return {"owner": owner, "sources_scanned": len(documents), "sources_available": total,
            "scan_truncated": total > len(documents), "candidate_count_returned": len(results),
            "min_score": min_score, "candidates": results,
            "note": "Heuristic candidates are not verified facts and are not saved as relationships. Review evidence and create an explicit relationship only when justified."}



@app.post("/github/relationship-reviews")
def review_github_relationship_candidate(request: RepositoryRelationshipReviewCreate):
    """Append an immutable review decision; accepted candidates create a supported edge."""
    decision = request.decision.strip().lower()
    predicate = request.predicate.strip()
    reviewer = request.reviewer.strip()[:200]
    rationale = request.rationale.strip()[:5000]
    if decision not in {"accepted", "rejected", "disputed"}:
        raise HTTPException(status_code=422, detail="decision must be accepted, rejected, or disputed")
    if request.from_source_id == request.to_source_id:
        raise HTTPException(status_code=422, detail="A review must connect two different file sources")
    if not predicate or len(predicate) > 200:
        raise HTTPException(status_code=422, detail="predicate must contain 1 to 200 characters")
    if request.confidence is not None and not 0 <= request.confidence <= 1:
        raise HTTPException(status_code=422, detail="confidence must be between 0 and 1")
    now = utc_now()
    with connection() as conn:
        rows = conn.execute("""SELECT id,canonical_url,provenance_json FROM sources WHERE id IN (?,?)""",
                            (request.from_source_id, request.to_source_id)).fetchall()
        if len(rows) != 2:
            raise HTTPException(status_code=404, detail="Both source IDs must exist and be different")
        by_id = {r["id"]: r for r in rows}
        left, right = by_id[request.from_source_id], by_id[request.to_source_id]
        left_meta, right_meta = json.loads(left["provenance_json"] or "{}"), json.loads(right["provenance_json"] or "{}")
        if left_meta.get("method") != "github_repository_ingestion" or right_meta.get("method") != "github_repository_ingestion":
            raise HTTPException(status_code=422, detail="Both sources must be indexed GitHub repository files")
        if (left_meta.get("owner", "").lower(), left_meta.get("repository", "").lower()) == (right_meta.get("owner", "").lower(), right_meta.get("repository", "").lower()):
            raise HTTPException(status_code=422, detail="Relationship reviews must connect different repositories")
        relationship_id = None
        if decision == "accepted":
            relation_provenance = {
                "review_id": None, "reviewer": reviewer, "rationale": rationale,
                "evidence_snapshot": request.evidence_snapshot,
                "review_provenance": request.provenance,
                "source_versions": {"from": None, "to": None},
            }
            # Capture current version identifiers so later updates cannot obscure what was reviewed.
            current = conn.execute("SELECT id,current_version_id FROM sources WHERE id IN (?,?)",
                                   (request.from_source_id, request.to_source_id)).fetchall()
            current_by_id = {r["id"]: r["current_version_id"] for r in current}
            relation_provenance["source_versions"] = {
                "from": current_by_id.get(request.from_source_id), "to": current_by_id.get(request.to_source_id)}
            cur = conn.execute("""INSERT INTO repository_relationships(from_source_id,to_source_id,predicate,confidence,status,provenance_json,created_at)
                VALUES(?,?,?,?,?,?,?)""", (request.from_source_id, request.to_source_id, predicate,
                request.confidence, "supported", json.dumps(relation_provenance, ensure_ascii=False, sort_keys=True), now))
            relationship_id = cur.lastrowid
        cur = conn.execute("""INSERT INTO repository_relationship_reviews
            (from_source_id,to_source_id,decision,predicate,reviewer,rationale,confidence,evidence_snapshot_json,provenance_json,resulting_relationship_id,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (request.from_source_id, request.to_source_id, decision, predicate,
            reviewer, rationale, request.confidence, json.dumps(request.evidence_snapshot, ensure_ascii=False, sort_keys=True),
            json.dumps(request.provenance, ensure_ascii=False, sort_keys=True), relationship_id, now))
        review_id = cur.lastrowid
        if relationship_id is not None:
            conn.execute("UPDATE repository_relationships SET provenance_json=? WHERE id=?", ("{}", relationship_id)) if False else None
            # Relationships are immutable; review_id is therefore included in the edge provenance on a follow-up schema.
    return {"review_id": review_id, "decision": decision, "from_source_id": request.from_source_id,
            "to_source_id": request.to_source_id, "predicate": predicate, "reviewer": reviewer,
            "rationale": rationale, "confidence": request.confidence, "resulting_relationship_id": relationship_id,
            "created_at": now}


@app.get("/github/relationship-reviews")
def list_github_relationship_reviews(owner: str | None = None, decision: str | None = None,
                                     limit: int = Query(default=100, ge=1, le=500)):
    clauses = ["1=1"]
    params = []
    if owner:
        clauses.append("(lower(json_extract(fs.provenance_json,'$.owner'))=lower(?) OR lower(json_extract(ts.provenance_json,'$.owner'))=lower(?))")
        params.extend([owner.strip(), owner.strip()])
    if decision:
        if decision not in {"accepted", "rejected", "disputed"}:
            raise HTTPException(status_code=422, detail="decision must be accepted, rejected, or disputed")
        clauses.append("rr.decision=?")
        params.append(decision)
    params.append(limit)
    with connection() as conn:
        rows = conn.execute("""SELECT rr.*,fs.canonical_url from_url,ts.canonical_url to_url,
            fs.provenance_json from_provenance_json,ts.provenance_json to_provenance_json
            FROM repository_relationship_reviews rr JOIN sources fs ON fs.id=rr.from_source_id
            JOIN sources ts ON ts.id=rr.to_source_id WHERE """ + " AND ".join(clauses) +
            " ORDER BY rr.created_at DESC,rr.id DESC LIMIT ?", params).fetchall()
    return [{"review_id": r["id"], "from_source_id": r["from_source_id"], "to_source_id": r["to_source_id"],
             "from_url": r["from_url"], "to_url": r["to_url"],
             "from_provenance": json.loads(r["from_provenance_json"]), "to_provenance": json.loads(r["to_provenance_json"]),
             "decision": r["decision"], "predicate": r["predicate"], "reviewer": r["reviewer"],
             "rationale": r["rationale"], "confidence": r["confidence"],
             "evidence_snapshot": json.loads(r["evidence_snapshot_json"]),
             "provenance": json.loads(r["provenance_json"]),
             "resulting_relationship_id": r["resulting_relationship_id"], "created_at": r["created_at"]} for r in rows]


@app.get("/github/relationship-graph")
def github_relationship_graph(owner: str = Query(min_length=1, max_length=100),
                              include_proposed: bool = False,
                              limit: int = Query(default=500, ge=1, le=2000)):
    """Return a graph payload with source nodes and explicit relationship edges."""
    owner = owner.strip()
    if not owner or any(c.isspace() for c in owner) or "/" in owner:
        raise HTTPException(status_code=422, detail="owner must be a GitHub owner name")
    with connection() as conn:
        sources = conn.execute("""SELECT s.id,s.canonical_url,s.provenance_json,v.id version_id,v.title,
            json_extract(v.provenance_json,'$.path') path
            FROM sources s LEFT JOIN document_versions v ON v.id=s.current_version_id
            WHERE lower(json_extract(s.provenance_json,'$.method'))='github_repository_ingestion'
              AND lower(json_extract(s.provenance_json,'$.owner'))=lower(?)
            ORDER BY s.id LIMIT ?""", (owner, limit)).fetchall()
        statuses = ("supported", "proposed", "disputed") if include_proposed else ("supported",)
        placeholders = ",".join("?" for _ in statuses)
        edge_rows = conn.execute("""SELECT rr.*,fs.provenance_json from_meta,ts.provenance_json to_meta
            FROM repository_relationships rr JOIN sources fs ON fs.id=rr.from_source_id
            JOIN sources ts ON ts.id=rr.to_source_id
            WHERE lower(json_extract(fs.provenance_json,'$.owner'))=lower(?)
              AND lower(json_extract(ts.provenance_json,'$.owner'))=lower(?)
              AND NOT EXISTS (SELECT 1 FROM relationship_revalidations rv WHERE rv.relationship_id=rr.id)
              AND rr.status IN (""" + placeholders + ") ORDER BY rr.id LIMIT ?",
              (owner, owner, *statuses, limit)).fetchall()
    nodes = [{"id": r["id"], "label": r["title"] or r["path"] or r["canonical_url"],
              "repository": json.loads(r["provenance_json"] or "{}").get("repository"),
              "path": r["path"], "url": r["canonical_url"], "version_id": r["version_id"],
              "source_navigation": f"/sources/{r['id']}", "history_navigation": f"/sources/{r['id']}/versions"} for r in sources]
    edges = [{"id": r["id"], "source": r["from_source_id"], "target": r["to_source_id"],
              "predicate": r["predicate"], "status": r["status"], "confidence": r["confidence"],
              "provenance": json.loads(r["provenance_json"] or "{}"), "created_at": r["created_at"]}
             for r in edge_rows]
    return {"owner": owner, "nodes": nodes, "edges": edges, "node_count": len(nodes), "edge_count": len(edges),
            "edge_filter": list(statuses), "nodes_truncated": len(nodes) >= limit,
            "note": "Only explicit stored relationships are rendered as edges; heuristic candidates are not graph edges."}


@app.post("/github/repository-relationships")
def create_github_repository_relationship(request: RepositoryRelationshipCreate):
    """Record an explicit, provenance-linked relation between files in different repositories."""
    predicate = request.predicate.strip()
    if request.from_source_id == request.to_source_id:
        raise HTTPException(status_code=422, detail="Cross-repository relationships must connect two different file sources")
    if not predicate or len(predicate) > 200:
        raise HTTPException(status_code=422, detail="predicate must contain 1 to 200 characters")
    if request.status not in {"proposed", "supported", "disputed", "rejected"}:
        raise HTTPException(status_code=422, detail="status must be proposed, supported, disputed, or rejected")
    if request.confidence is not None and not 0 <= request.confidence <= 1:
        raise HTTPException(status_code=422, detail="confidence must be between 0 and 1")
    now = utc_now()
    with connection() as conn:
        rows = conn.execute("SELECT id,provenance_json FROM sources WHERE id IN (?,?)",
                            (request.from_source_id, request.to_source_id)).fetchall()
        if len(rows) != 2:
            raise HTTPException(status_code=404, detail="Both source IDs must exist and be different")
        provenance = {r["id"]: json.loads(r["provenance_json"]) for r in rows}
        left, right = provenance[request.from_source_id], provenance[request.to_source_id]
        if left.get("method") != "github_repository_ingestion" or right.get("method") != "github_repository_ingestion":
            raise HTTPException(status_code=422, detail="Both sources must be indexed GitHub repository files")
        if (left.get("owner", "").lower(), left.get("repository", "").lower()) == (right.get("owner", "").lower(), right.get("repository", "").lower()):
            raise HTTPException(status_code=422, detail="Cross-repository relationships must connect different repositories")
        cur = conn.execute("""INSERT INTO repository_relationships(from_source_id,to_source_id,predicate,confidence,status,provenance_json,created_at)
            VALUES(?,?,?,?,?,?,?)""", (request.from_source_id, request.to_source_id, predicate, request.confidence,
            request.status, json.dumps(request.provenance, ensure_ascii=False, sort_keys=True), now))
        relationship_id = cur.lastrowid
    return {"relationship_id": relationship_id, "from_source_id": request.from_source_id,
            "to_source_id": request.to_source_id, "predicate": predicate, "confidence": request.confidence,
            "status": request.status, "provenance": request.provenance, "created_at": now}


@app.get("/github/repository-relationships")
def list_github_repository_relationships(owner: str | None = None, repository: str | None = None,
                                         limit: int = Query(default=100, ge=1, le=500)):
    """List explicit cross-repository links with both file endpoints and evidence provenance."""
    clauses = ["1=1"]
    params = []
    if owner:
        clauses.append("(lower(json_extract(fs.provenance_json,'$.owner'))=lower(?) OR lower(json_extract(ts.provenance_json,'$.owner'))=lower(?))")
        params.extend([owner.strip(), owner.strip()])
    if repository:
        clauses.append("(lower(json_extract(fs.provenance_json,'$.repository'))=lower(?) OR lower(json_extract(ts.provenance_json,'$.repository'))=lower(?))")
        params.extend([repository.strip(), repository.strip()])
    params.append(limit)
    with connection() as conn:
        rows = conn.execute("""SELECT rr.*,fs.canonical_url from_url,ts.canonical_url to_url,
            fs.provenance_json from_provenance_json,ts.provenance_json to_provenance_json
            FROM repository_relationships rr JOIN sources fs ON fs.id=rr.from_source_id
            JOIN sources ts ON ts.id=rr.to_source_id WHERE """ + " AND ".join(clauses) +
            " ORDER BY rr.created_at DESC,rr.id DESC LIMIT ?", params).fetchall()
    return [{"relationship_id": r["id"], "from_source_id": r["from_source_id"], "to_source_id": r["to_source_id"],
             "from_url": r["from_url"], "to_url": r["to_url"],
             "from_provenance": json.loads(r["from_provenance_json"]), "to_provenance": json.loads(r["to_provenance_json"]),
             "predicate": r["predicate"], "confidence": r["confidence"], "status": r["status"],
             "provenance": json.loads(r["provenance_json"]), "created_at": r["created_at"]} for r in rows]


@app.post("/ingest")
def ingest(request:IngestRequest):
    try:
        url=canonicalize_url(str(request.url)); title,body,status,ctype=retrieve_page(url)
        saved=save_document(url,title,body,status,ctype,{"method":"direct_url_ingestion","submitted_url":str(request.url),"canonical_url":url})
        return {"status":"indexed",**saved,"characters_indexed":len(body)}
    except (ValueError,httpx.HTTPError) as exc: raise HTTPException(status_code=400,detail=str(exc)) from exc

def _metadata_filter_sql(metadata_key: str | None, metadata_value: str | None, alias: str = "s"):
    """Return a parameterized EXISTS clause for the current value of source metadata."""
    if metadata_value is not None and not metadata_key:
        raise HTTPException(status_code=422, detail="metadata_key is required when metadata_value is supplied")
    if not metadata_key:
        return "", []
    clauses = [
        f"""EXISTS (
            SELECT 1 FROM source_metadata sm
            WHERE sm.source_id={alias}.id AND sm.metadata_key=?
              AND sm.id=(SELECT sm2.id FROM source_metadata sm2
                         WHERE sm2.source_id={alias}.id AND sm2.metadata_key=sm.metadata_key
                         ORDER BY sm2.created_at DESC, sm2.id DESC LIMIT 1)"""
    ]
    params = [metadata_key.strip()]
    if metadata_value is not None:
        clauses[0] += " AND sm.value_json=?"
        params.append(json.dumps(metadata_value, ensure_ascii=False))
    clauses[0] += ")"
    return " AND " + clauses[0], params

@app.get("/search",response_model=list[SearchResult])
def search(q:str=Query(min_length=1,max_length=300),limit:int=Query(default=10,ge=1,le=50),
           include_history:bool=False,metadata_key:str|None=None,metadata_value:str|None=None):
    terms=[t.replace('"',' ').strip() for t in q.split() if t.strip()]
    if not terms: return []
    match=' AND '.join('"'+t.replace('"','')+'"' for t in terms)
    filter_sql='' if include_history else 'AND s.current_version_id=v.id'
    metadata_sql, metadata_params = _metadata_filter_sql(metadata_key, metadata_value)
    with connection() as conn:
        rows=conn.execute(f"""SELECT v.id,v.source_id,v.version_number,v.title,s.canonical_url,v.body,v.retrieved_at,v.content_hash,s.current_version_id,snippet(versions_fts,1,'<mark>','</mark>',' … ',24) excerpt FROM versions_fts JOIN document_versions v ON v.id=versions_fts.rowid JOIN sources s ON s.id=v.source_id WHERE versions_fts MATCH ? {filter_sql} {metadata_sql} ORDER BY bm25(versions_fts),v.retrieved_at DESC LIMIT ?""",(match,*metadata_params,limit)).fetchall()
    return [SearchResult(document_id=r["id"],version_id=r["id"],source_id=r["source_id"],version_number=r["version_number"],title=r["title"],url=r["canonical_url"],excerpt=r["excerpt"] or r["body"][:240],retrieved_at=r["retrieved_at"],content_hash=r["content_hash"],is_current=r["current_version_id"]==r["id"]) for r in rows]

@app.get("/documents")
def documents(limit:int=Query(default=20,ge=1,le=100),include_history:bool=False,
              metadata_key:str|None=None,metadata_value:str|None=None):
    filt='' if include_history else 'WHERE s.current_version_id=v.id'
    metadata_sql, metadata_params = _metadata_filter_sql(metadata_key, metadata_value)
    if filt:
        filt += metadata_sql
    else:
        filt = "WHERE 1=1" + metadata_sql
    with connection() as conn:
        rows=conn.execute(f"SELECT v.id version_id,s.id source_id,v.version_number,s.canonical_url url,v.title,v.retrieved_at,v.content_hash,v.extraction_status,v.http_status,v.content_type,(s.current_version_id=v.id) is_current FROM document_versions v JOIN sources s ON s.id=v.source_id {filt} ORDER BY v.retrieved_at DESC LIMIT ?",(*metadata_params,limit)).fetchall()
    return [dict(r) for r in rows]

@app.get("/sources")
def list_sources(limit:int=Query(default=50,ge=1,le=200)):
    with connection() as conn:
        rows=conn.execute("SELECT s.id source_id,s.canonical_url,s.source_type,s.first_seen_at,s.last_seen_at,s.provenance_json,v.id current_version_id,v.version_number current_version_number,v.title current_title,v.content_hash current_content_hash FROM sources s LEFT JOIN document_versions v ON v.id=s.current_version_id ORDER BY s.last_seen_at DESC LIMIT ?",(limit,)).fetchall()
    return [dict(r)|{"provenance":json.loads(r["provenance_json"])} for r in rows]

@app.get("/sources/{source_id}")
def get_source(source_id:int):
    with connection() as conn:
        row=conn.execute("SELECT * FROM sources WHERE id=?",(source_id,)).fetchone()
        if not row: raise HTTPException(status_code=404,detail="Source not found")
        versions=conn.execute("SELECT id version_id,version_number,title,content_hash,retrieved_at,http_status,content_type,extraction_status,provenance_json,(id=?) is_current FROM document_versions WHERE source_id=? ORDER BY version_number DESC",(row["current_version_id"],source_id)).fetchall()
        events=conn.execute("SELECT id,occurred_at event_occurred FROM ingestion_events WHERE source_id=? ORDER BY occurred_at DESC LIMIT 50",(source_id,)).fetchall()
    return {"source_id":row["id"],"canonical_url":row["canonical_url"],"source_type":row["source_type"],"first_seen_at":row["first_seen_at"],"last_seen_at":row["last_seen_at"],"provenance":json.loads(row["provenance_json"]),"versions":[dict(v)|{"provenance":json.loads(v["provenance_json"])} for v in versions],"recent_ingestion_events":[dict(e) for e in events]}

@app.get("/sources/{source_id}/versions")
def source_versions(source_id:int):
    with connection() as conn:
        src=conn.execute("SELECT id,current_version_id FROM sources WHERE id=?",(source_id,)).fetchone()
        if not src: raise HTTPException(status_code=404,detail="Source not found")
        rows=conn.execute("SELECT id version_id,source_id,version_number,title,body,content_hash,retrieved_at,http_status,content_type,extraction_status,provenance_json FROM document_versions WHERE source_id=? ORDER BY version_number DESC",(source_id,)).fetchall()
    return [dict(r)|{"is_current":r["version_id"]==src["current_version_id"],"provenance":json.loads(r["provenance_json"])} for r in rows]

@app.get("/sources/{source_id}/events")
def source_events(source_id:int,limit:int=Query(default=50,ge=1,le=200)):
    with connection() as conn:
        if not conn.execute("SELECT 1 FROM sources WHERE id=?",(source_id,)).fetchone(): raise HTTPException(status_code=404,detail="Source not found")
        rows=conn.execute("SELECT * FROM ingestion_events WHERE source_id=? ORDER BY occurred_at DESC LIMIT ?",(source_id,limit)).fetchall()
    return [dict(r)|{"details":json.loads(r["details_json"])} for r in rows]

@app.post("/entities")
def create_entity(request: EntityCreate):
    name = request.canonical_name.strip()
    kind = request.entity_type.strip()
    if not name or not kind:
        raise HTTPException(status_code=422, detail="canonical_name and entity_type must not be blank")
    now = utc_now()
    with connection() as conn:
        cur = conn.execute(
            "INSERT INTO entities(canonical_name,entity_type,description,aliases_json,provenance_json,created_at) VALUES(?,?,?,?,?,?)",
            (name, kind, request.description, json.dumps(request.aliases, ensure_ascii=False), json.dumps(request.provenance, ensure_ascii=False, sort_keys=True), now))
        entity_id = cur.lastrowid
    return {"entity_id": entity_id, "canonical_name": name, "entity_type": kind,
            "description": request.description, "aliases": request.aliases,
            "provenance": request.provenance, "created_at": now}

@app.get("/entities")
def list_entities(q: str | None = None, entity_type: str | None = None,
                  limit: int = Query(default=50, ge=1, le=200)):
    clauses, params = [], []
    if q:
        clauses.append("(canonical_name LIKE ? OR aliases_json LIKE ?)")
        params.extend([f"%{q}%", f"%{q}%"])
    if entity_type:
        clauses.append("entity_type=?")
        params.append(entity_type)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(limit)
    with connection() as conn:
        rows = conn.execute("SELECT * FROM entities" + where + " ORDER BY canonical_name LIMIT ?", params).fetchall()
    return [{"entity_id": r["id"], "canonical_name": r["canonical_name"], "entity_type": r["entity_type"],
             "description": r["description"], "aliases": json.loads(r["aliases_json"]),
             "provenance": json.loads(r["provenance_json"]), "created_at": r["created_at"]} for r in rows]

@app.get("/entities/{entity_id}")
def get_entity(entity_id: int):
    with connection() as conn:
        row = conn.execute("SELECT * FROM entities WHERE id=?", (entity_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Entity not found")
        rels = conn.execute("""SELECT r.*, e.canonical_name object_entity_name
            FROM entity_relationships r LEFT JOIN entities e ON e.id=r.object_entity_id
            WHERE r.subject_entity_id=? OR r.object_entity_id=? ORDER BY r.created_at, r.id""",
            (entity_id, entity_id)).fetchall()
    return {"entity_id": row["id"], "canonical_name": row["canonical_name"], "entity_type": row["entity_type"],
            "description": row["description"], "aliases": json.loads(row["aliases_json"]),
            "provenance": json.loads(row["provenance_json"]), "created_at": row["created_at"],
            "relationships": [{"relationship_id": r["id"], "subject_entity_id": r["subject_entity_id"],
                "predicate": r["predicate"], "object_entity_id": r["object_entity_id"],
                "object_entity_name": r["object_entity_name"],
                "object_literal": json.loads(r["object_literal_json"]) if r["object_literal_json"] is not None else None,
                "source_id": r["source_id"], "evidence_version_id": r["evidence_version_id"],
                "confidence": r["confidence"], "provenance": json.loads(r["provenance_json"]),
                "created_at": r["created_at"]} for r in rels]}

@app.post("/sources/{source_id}/metadata")
def add_source_metadata(source_id: int, request: MetadataCreate):
    key = request.key.strip()
    if not key:
        raise HTTPException(status_code=422, detail="Metadata key must not be blank")
    now = utc_now()
    with connection() as conn:
        if not conn.execute("SELECT 1 FROM sources WHERE id=?", (source_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Source not found")
        previous = conn.execute("SELECT id FROM source_metadata WHERE source_id=? AND metadata_key=? ORDER BY created_at DESC,id DESC LIMIT 1", (source_id, key)).fetchone()
        cur = conn.execute("INSERT INTO source_metadata(source_id,metadata_key,value_json,provenance_json,created_at,supersedes_id) VALUES(?,?,?,?,?,?)",
            (source_id, key, json.dumps(request.value, ensure_ascii=False, sort_keys=True),
             json.dumps(request.provenance, ensure_ascii=False, sort_keys=True), now, previous["id"] if previous else None))
        metadata_id = cur.lastrowid
    return {"metadata_id": metadata_id, "source_id": source_id, "key": key, "value": request.value,
            "supersedes_id": previous["id"] if previous else None, "provenance": request.provenance, "created_at": now}

@app.get("/sources/{source_id}/metadata")
def get_source_metadata(source_id: int, include_history: bool = False):
    with connection() as conn:
        if not conn.execute("SELECT 1 FROM sources WHERE id=?", (source_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Source not found")
        rows = conn.execute("SELECT * FROM source_metadata WHERE source_id=? ORDER BY metadata_key,created_at DESC,id DESC", (source_id,)).fetchall()
    latest_ids = {}
    for r in rows:
        latest_ids.setdefault(r["metadata_key"], r["id"])
    return [{"metadata_id": r["id"], "source_id": r["source_id"], "key": r["metadata_key"],
             "value": json.loads(r["value_json"]), "provenance": json.loads(r["provenance_json"]),
             "created_at": r["created_at"], "supersedes_id": r["supersedes_id"],
             "is_current": latest_ids[r["metadata_key"]] == r["id"]}
            for r in rows if include_history or latest_ids[r["metadata_key"]] == r["id"]]

@app.post("/relationships")
def create_relationship(request: RelationshipCreate):
    predicate = request.predicate.strip()
    if not predicate:
        raise HTTPException(status_code=422, detail="predicate must not be blank")
    if (request.object_entity_id is None) == (request.object_literal is None):
        raise HTTPException(status_code=422, detail="Provide exactly one of object_entity_id or object_literal")
    if request.confidence is not None and not 0 <= request.confidence <= 1:
        raise HTTPException(status_code=422, detail="confidence must be between 0 and 1")
    now = utc_now()
    with connection() as conn:
        if not conn.execute("SELECT 1 FROM entities WHERE id=?", (request.subject_entity_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Subject entity not found")
        if request.object_entity_id is not None and not conn.execute("SELECT 1 FROM entities WHERE id=?", (request.object_entity_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Object entity not found")
        if request.source_id is not None and not conn.execute("SELECT 1 FROM sources WHERE id=?", (request.source_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Source not found")
        if request.evidence_version_id is not None and not conn.execute("SELECT 1 FROM document_versions WHERE id=?", (request.evidence_version_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Evidence version not found")
        cur = conn.execute("""INSERT INTO entity_relationships(subject_entity_id,predicate,object_entity_id,object_literal_json,
            source_id,evidence_version_id,confidence,provenance_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
            (request.subject_entity_id, predicate, request.object_entity_id,
             json.dumps(request.object_literal, ensure_ascii=False, sort_keys=True) if request.object_entity_id is None else None,
             request.source_id, request.evidence_version_id, request.confidence,
             json.dumps(request.provenance, ensure_ascii=False, sort_keys=True), now))
        relationship_id = cur.lastrowid
    return {"relationship_id": relationship_id, "subject_entity_id": request.subject_entity_id,
            "predicate": predicate, "object_entity_id": request.object_entity_id,
            "object_literal": request.object_literal, "source_id": request.source_id,
            "evidence_version_id": request.evidence_version_id, "confidence": request.confidence,
            "provenance": request.provenance, "created_at": now}

def _knowledge_node_exists(conn, kind, node_id):
    if kind == "page":
        return conn.execute("SELECT 1 FROM wiki_page_revisions WHERE id=?", (node_id,)).fetchone() is not None
    if kind == "artifact":
        return conn.execute("SELECT 1 FROM stem_artifacts WHERE id=?", (node_id,)).fetchone() is not None
    return False

@app.post("/wiki/edges")
def create_knowledge_edge(request: KnowledgeEdgeCreate):
    sk, ok = request.subject_kind.strip().lower(), request.object_kind.strip().lower()
    predicate = request.predicate.strip().lower().replace(" ", "_")
    if sk not in {"page", "artifact"} or ok not in {"page", "artifact"}:
        raise HTTPException(status_code=422, detail="Node kinds must be page or artifact")
    if not predicate or len(predicate) > 120:
        raise HTTPException(status_code=422, detail="Predicate must contain 1-120 characters")
    if request.status not in {"proposed", "supported", "disputed", "rejected"}:
        raise HTTPException(status_code=422, detail="Invalid relationship status")
    if request.confidence is not None and not 0 <= request.confidence <= 1:
        raise HTTPException(status_code=422, detail="Confidence must be between 0 and 1")
    with connection() as conn:
        if not _knowledge_node_exists(conn, sk, request.subject_id) or not _knowledge_node_exists(conn, ok, request.object_id):
            raise HTTPException(status_code=404, detail="Subject or object node not found")
        evidence = request.evidence_page_revision_id
        if evidence is not None and not conn.execute("SELECT 1 FROM wiki_page_revisions WHERE id=?", (evidence,)).fetchone():
            raise HTTPException(status_code=404, detail="Evidence page revision not found")
        now = utc_now()
        cur = conn.execute("""INSERT INTO knowledge_edges
            (subject_kind,subject_id,predicate,object_kind,object_id,evidence_page_revision_id,confidence,status,provenance_json,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (sk, request.subject_id, predicate, ok, request.object_id, evidence, request.confidence,
             request.status, json.dumps(request.provenance, ensure_ascii=False, sort_keys=True), now))
        edge_id = cur.lastrowid
    return {"edge_id": edge_id, "subject_kind": sk, "subject_id": request.subject_id, "predicate": predicate,
            "object_kind": ok, "object_id": request.object_id, "evidence_page_revision_id": evidence,
            "confidence": request.confidence, "status": request.status, "provenance": request.provenance, "created_at": now}

@app.get("/wiki/graph/{kind}/{node_id}")
def get_knowledge_graph(kind: str, node_id: int, depth: int = Query(default=1, ge=1, le=3),
                        include_rejected: bool = False, limit: int = Query(default=100, ge=1, le=500)):
    if kind not in {"page", "artifact"}:
        raise HTTPException(status_code=422, detail="kind must be page or artifact")
    with connection() as conn:
        if not _knowledge_node_exists(conn, kind, node_id):
            raise HTTPException(status_code=404, detail="Graph starting node not found")
        visited, frontier, edges_by_id = {(kind, node_id)}, [(kind, node_id, 0)], {}
        while frontier and len(visited) < limit:
            current_kind, current_id, level = frontier.pop(0)
            if level >= depth:
                continue
            sql = "SELECT * FROM knowledge_edges WHERE ((subject_kind=? AND subject_id=?) OR (object_kind=? AND object_id=?))"
            params = [current_kind, current_id, current_kind, current_id]
            if not include_rejected:
                sql += " AND status!='rejected'"
            sql += " ORDER BY created_at DESC,id DESC LIMIT ?"
            params.append(limit)
            for edge in conn.execute(sql, params).fetchall():
                edges_by_id[edge["id"]] = edge
                other = (edge["object_kind"], edge["object_id"]) if (edge["subject_kind"], edge["subject_id"]) == (current_kind, current_id) else (edge["subject_kind"], edge["subject_id"])
                if other not in visited and len(visited) < limit:
                    visited.add(other)
                    frontier.append((other[0], other[1], level + 1))
        nodes = []
        for node_kind, object_id in visited:
            if node_kind == "page":
                row = conn.execute("""SELECT p.slug,r.title,r.discipline,r.revision_number FROM wiki_page_revisions r
                    JOIN wiki_pages p ON p.id=r.page_id WHERE r.id=?""", (object_id,)).fetchone()
                if row:
                    nodes.append({"kind":"page","id":object_id,"slug":row["slug"],"title":row["title"],"discipline":row["discipline"],"revision_number":row["revision_number"]})
            else:
                row = conn.execute("SELECT id,name,artifact_type,page_revision_id FROM stem_artifacts WHERE id=?", (object_id,)).fetchone()
                if row:
                    nodes.append({"kind":"artifact","id":object_id,"name":row["name"],"artifact_type":row["artifact_type"],"page_revision_id":row["page_revision_id"]})
        edges = [{"edge_id":r["id"],"subject_kind":r["subject_kind"],"subject_id":r["subject_id"],"predicate":r["predicate"],
                  "object_kind":r["object_kind"],"object_id":r["object_id"],"evidence_page_revision_id":r["evidence_page_revision_id"],
                  "confidence":r["confidence"],"status":r["status"],"provenance":json.loads(r["provenance_json"]),"created_at":r["created_at"]}
                 for r in edges_by_id.values()]
    return {"root":{"kind":kind,"id":node_id},"depth":depth,"nodes":nodes,"edges":edges,
            "note":"Relationships retain status, confidence, and provenance; a link is not by itself proof of truth."}

@app.get("/wiki/edges")
def list_knowledge_edges(subject_kind: str | None = None, subject_id: int | None = None,
                         object_kind: str | None = None, object_id: int | None = None,
                         predicate: str | None = None, status: str | None = None,
                         limit: int = Query(default=100, ge=1, le=500)):
    clauses, params = [], []
    for field, value in (("subject_kind", subject_kind), ("subject_id", subject_id),
                         ("object_kind", object_kind), ("object_id", object_id),
                         ("predicate", predicate), ("status", status)):
        if value is not None:
            clauses.append(field + "=?")
            params.append(value.strip().lower().replace(" ", "_") if field == "predicate" else value)
    if status is not None and status not in {"proposed", "supported", "disputed", "rejected"}:
        raise HTTPException(status_code=422, detail="Invalid relationship status")
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(limit)
    with connection() as conn:
        rows = conn.execute("SELECT * FROM knowledge_edges" + where + " ORDER BY created_at DESC,id DESC LIMIT ?", params).fetchall()
    return [{"edge_id": r["id"], "subject_kind": r["subject_kind"], "subject_id": r["subject_id"],
             "predicate": r["predicate"], "object_kind": r["object_kind"], "object_id": r["object_id"],
             "evidence_page_revision_id": r["evidence_page_revision_id"], "confidence": r["confidence"],
             "status": r["status"], "provenance": json.loads(r["provenance_json"]), "created_at": r["created_at"]}
            for r in rows]

def _unit_dimension(unit: str):
    """Return coarse dimensions for a supported unit expression, or None if unsupported."""
    base = {
        "m": {"L": 1}, "cm": {"L": 1}, "km": {"L": 1}, "s": {"T": 1}, "min": {"T": 1}, "h": {"T": 1},
        "kg": {"M": 1}, "g": {"M": 1}, "N": {"M": 1, "L": 1, "T": -2},
        "J": {"M": 1, "L": 2, "T": -2}, "W": {"M": 1, "L": 2, "T": -3},
        "Hz": {"T": -1}, "A": {"I": 1}, "K": {"Theta": 1}, "mol": {"Amount": 1},
        "rad": {}, "dimensionless": {}, "1": {},
    }
    text_unit = unit.replace(" ", "")
    if text_unit in base:
        return base[text_unit]
    import re
    tokens = re.findall(r"([*/]?)([A-Za-z]+|1)(?:\^(-?\d+))?", text_unit)
    rebuilt = "".join((op or "") + name + (("^" + power) if power else "") for op, name, power in tokens)
    if not tokens or rebuilt != text_unit:
        return None
    dims = {}
    for index, (op, name, power) in enumerate(tokens):
        if name not in base or (op == "" and index > 0):
            return None
        exponent = int(power or "1") * (-1 if op == "/" else 1)
        for dimension, dimension_power in base[name].items():
            dims[dimension] = dims.get(dimension, 0) + dimension_power * exponent
    return {key: value for key, value in dims.items() if value}

def _validate_stem_data(artifact_type: str, data: dict) -> dict:
    errors, warnings, checks = [], [], []
    if artifact_type == "equation":
        expression = data.get("expression")
        variables = data.get("variables")
        if not isinstance(expression, str) or not expression.strip():
            errors.append("Equation expression must be a non-empty string.")
        if not isinstance(variables, list) or not variables:
            errors.append("Equation should define at least one variable.")
        else:
            symbols = set()
            for index, variable in enumerate(variables):
                if not isinstance(variable, dict):
                    errors.append(f"Variable at index {index} must be an object.")
                    continue
                symbol = variable.get("symbol")
                if not isinstance(symbol, str) or not symbol.strip():
                    errors.append(f"Variable at index {index} is missing a symbol.")
                elif symbol in symbols:
                    warnings.append(f"Variable symbol '{symbol}' is defined more than once.")
                else:
                    symbols.add(symbol)
                if not isinstance(variable.get("unit"), str) or not variable.get("unit", "").strip():
                    warnings.append(f"Variable '{symbol or index}' has no unit recorded.")
                if not variable.get("meaning"):
                    warnings.append(f"Variable '{symbol or index}' has no meaning/definition.")
        if "lhs_unit" in data and "rhs_unit" in data:
            lhs, rhs = data["lhs_unit"], data["rhs_unit"]
            lhs_dimensions, rhs_dimensions = _unit_dimension(lhs), _unit_dimension(rhs)
            if lhs_dimensions is not None and rhs_dimensions is not None:
                if lhs_dimensions != rhs_dimensions:
                    errors.append(f"Declared equation side units are dimensionally incompatible: {lhs} vs {rhs}.")
                else:
                    checks.append(f"Declared side units are dimensionally compatible: {lhs} and {rhs}.")
            else:
                warnings.append("Dimensional comparison was skipped because one or both units use unsupported unit syntax or symbols.")
        else:
            warnings.append("No lhs_unit/rhs_unit pair supplied; equation dimensional consistency was not evaluated.")
    elif artifact_type == "constant":
        value, unit = data.get("value"), data.get("unit")
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            errors.append("Constant value must be numeric or a documented string.")
        elif isinstance(value, (int, float)) and not math.isfinite(value):
            errors.append("Numeric constant must be finite.")
        elif isinstance(value, str):
            try:
                number = float(value)
                if not math.isfinite(number):
                    errors.append("Numeric constant must be finite.")
            except ValueError:
                warnings.append("Constant value is textual; verify whether it is a symbolic value or a measured number.")
        if not isinstance(unit, str) or not unit.strip():
            errors.append("Constant must include a non-empty unit or explicitly use 'dimensionless'.")
        if not data.get("source") and not data.get("reference"):
            warnings.append("No source/reference is attached to the constant.")
        if "uncertainty" not in data:
            warnings.append("Uncertainty or a note explaining why uncertainty is not applicable is not recorded.")
    elif artifact_type == "algorithm":
        if not isinstance(data.get("pseudocode"), str) or not data.get("pseudocode", "").strip():
            errors.append("Algorithm pseudocode must be non-empty.")
        for field in ("inputs", "outputs"):
            if not isinstance(data.get(field), list) or not data[field]:
                warnings.append(f"Algorithm {field} should be a non-empty list.")
        if not data.get("preconditions"):
            warnings.append("Algorithm preconditions are not recorded.")
        if not data.get("complexity"):
            warnings.append("Algorithm complexity or an explicit not-assessed note is not recorded.")
    elif artifact_type == "experiment":
        procedure = data.get("procedure")
        if not isinstance(procedure, list) or not procedure or not all(isinstance(step, str) and step.strip() for step in procedure):
            errors.append("Experiment procedure must be a non-empty list of non-empty steps.")
        for field in ("independent_variables", "dependent_variables"):
            if not isinstance(data.get(field), list) or not data[field]:
                errors.append(f"Experiment {field} must be a non-empty list.")
        for field, label in (("controls","control variables"),("replicates","replicate plan"),("uncertainty","measurement uncertainty"),("safety","safety considerations")):
            if not data.get(field):
                warnings.append(f"Experiment {label} are not recorded.")
        if not data.get("analysis_method"):
            warnings.append("Experiment analysis method is not recorded.")
    else:
        errors.append("Unsupported STEM artifact type.")
    outcome = "fail" if errors else ("review" if warnings else "pass")
    return {"outcome": outcome, "errors": errors, "warnings": warnings, "checks": checks,
            "note": "Automated validation is a screening aid, not proof of scientific correctness."}

@app.get("/wiki/validation-rule-sets")
def list_stem_validation_rule_sets():
    with connection() as conn:
        rows = conn.execute("SELECT id,name,version,rules_json,created_at FROM stem_validation_rule_sets ORDER BY name,version").fetchall()
    return [{"rule_set_id": r["id"], "name": r["name"], "version": r["version"], "rules": json.loads(r["rules_json"]), "created_at": r["created_at"]} for r in rows]

@app.post("/wiki/artifacts/{artifact_id}/validate")
def validate_stem_artifact(artifact_id: int):
    with connection() as conn:
        artifact = conn.execute("SELECT id,artifact_type,data_json FROM stem_artifacts WHERE id=?", (artifact_id,)).fetchone()
        if not artifact:
            raise HTTPException(status_code=404, detail="STEM artifact not found")
        result = _validate_stem_data(artifact["artifact_type"], json.loads(artifact["data_json"]))
        rule_set = conn.execute("SELECT name,version FROM stem_validation_rule_sets WHERE name='stem-core' ORDER BY id DESC LIMIT 1").fetchone()
        validator_version = "0.2"
        rule_set_name, rule_set_version = (rule_set["name"], rule_set["version"]) if rule_set else ("stem-core", "0.2")
        now = utc_now()
        cur = conn.execute("""INSERT INTO stem_validation_reports(artifact_id,validator_version,outcome,findings_json,created_at) VALUES(?,?,?,?,?)""", (artifact_id, validator_version, result["outcome"], json.dumps(result, ensure_ascii=False, sort_keys=True), now))
    return {"report_id": cur.lastrowid, "artifact_id": artifact_id, "validator_version": validator_version, "rule_set": {"name": rule_set_name, "version": rule_set_version},
            **result, "created_at": now}

@app.get("/wiki/artifacts/{artifact_id}/validation-reports")
def list_stem_validation_reports(artifact_id: int, limit: int = Query(default=20, ge=1, le=100)):
    with connection() as conn:
        if not conn.execute("SELECT 1 FROM stem_artifacts WHERE id=?", (artifact_id,)).fetchone():
            raise HTTPException(status_code=404, detail="STEM artifact not found")
        rows = conn.execute("SELECT * FROM stem_validation_reports WHERE artifact_id=? ORDER BY created_at DESC,id DESC LIMIT ?", (artifact_id, limit)).fetchall()
    return [{"report_id": r["id"], "artifact_id": r["artifact_id"], "validator_version": r["validator_version"],
             "outcome": r["outcome"], **json.loads(r["findings_json"]), "created_at": r["created_at"]} for r in rows]

def _validate_stem_artifact(artifact_type: str, data: dict):
    required = {
        "equation": {"expression", "variables"},
        "constant": {"value", "unit"},
        "algorithm": {"pseudocode", "inputs", "outputs"},
        "experiment": {"procedure", "independent_variables", "dependent_variables"},
    }
    if artifact_type not in required:
        raise HTTPException(status_code=422, detail="artifact_type must be equation, constant, algorithm, or experiment")
    missing = sorted(required[artifact_type] - set(data))
    if missing:
        raise HTTPException(status_code=422, detail=f"Missing required data fields: {', '.join(missing)}")
    if artifact_type == "equation" and not isinstance(data.get("variables"), list):
        raise HTTPException(status_code=422, detail="equation variables must be a list of variable definitions")
    if artifact_type == "constant":
        if not isinstance(data.get("value"), (int, float, str)) or not str(data.get("unit", "")).strip():
            raise HTTPException(status_code=422, detail="constant requires a numeric/text value and non-empty unit")
    if artifact_type == "algorithm" and not isinstance(data.get("pseudocode"), str):
        raise HTTPException(status_code=422, detail="algorithm pseudocode must be a string")
    if artifact_type == "experiment" and not isinstance(data.get("procedure"), list):
        raise HTTPException(status_code=422, detail="experiment procedure must be a list of steps")

@app.post("/wiki/pages/{slug}/artifacts")
def create_stem_artifact(slug: str, request: STEMArtifactCreate):
    slug = _wiki_slug(slug)
    name = request.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="artifact name must not be blank")
    _validate_stem_artifact(request.artifact_type, request.data)
    with connection() as conn:
        page = conn.execute("SELECT id,current_revision_id FROM wiki_pages WHERE slug=?", (slug,)).fetchone()
        if not page:
            raise HTTPException(status_code=404, detail="Wiki page not found")
        now = utc_now()
        cur = conn.execute("""INSERT INTO stem_artifacts(page_id,page_revision_id,artifact_type,name,data_json,provenance_json,created_at)
            VALUES(?,?,?,?,?,?,?)""", (page["id"], page["current_revision_id"], request.artifact_type, name,
            json.dumps(request.data, ensure_ascii=False, sort_keys=True),
            json.dumps(request.provenance, ensure_ascii=False, sort_keys=True), now))
        artifact_id = cur.lastrowid
    return {"artifact_id": artifact_id, "page_slug": slug, "page_revision_id": page["current_revision_id"],
            "artifact_type": request.artifact_type, "name": name, "data": request.data,
            "provenance": request.provenance, "created_at": now}

@app.get("/wiki/pages/{slug}/artifacts")
def list_stem_artifacts(slug: str, artifact_type: str | None = None, include_history: bool = False):
    slug = _wiki_slug(slug)
    clauses, params = ["p.slug=?"], [slug]
    if artifact_type:
        clauses.append("a.artifact_type=?"); params.append(artifact_type)
    if not include_history:
        clauses.append("a.page_revision_id=p.current_revision_id")
    with connection() as conn:
        if not conn.execute("SELECT 1 FROM wiki_pages WHERE slug=?", (slug,)).fetchone():
            raise HTTPException(status_code=404, detail="Wiki page not found")
        rows = conn.execute("""SELECT a.id artifact_id,p.slug page_slug,a.page_revision_id,a.artifact_type,a.name,a.data_json,a.provenance_json,a.created_at
            FROM stem_artifacts a JOIN wiki_pages p ON p.id=a.page_id WHERE """ + " AND ".join(clauses) + " ORDER BY a.created_at DESC,a.id DESC", params).fetchall()
    return [{"artifact_id": r["artifact_id"], "page_slug": r["page_slug"], "page_revision_id": r["page_revision_id"],
             "artifact_type": r["artifact_type"], "name": r["name"], "data": json.loads(r["data_json"]),
             "provenance": json.loads(r["provenance_json"]), "created_at": r["created_at"]} for r in rows]

def _wiki_slug(value: str) -> str:
    import re
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    if not slug or len(slug) > 160:
        raise HTTPException(status_code=422, detail="slug must contain 1-160 URL-safe characters")
    return slug

def _wiki_revision_dict(row):
    return {
        "revision_id": row["revision_id"], "page_id": row["page_id"],
        "slug": row["slug"], "revision_number": row["revision_number"],
        "title": row["title"], "summary": row["summary"], "body": row["body"],
        "language": row["language"], "discipline": row["discipline"],
        "tags": json.loads(row["tags_json"]), "references": json.loads(row["references_json"]),
        "author": row["author"], "change_note": row["change_note"],
        "provenance": json.loads(row["provenance_json"]), "created_at": row["created_at"],
        "is_current": row["current_revision_id"] == row["revision_id"]
    }

def _insert_wiki_revision(conn, page_id, slug, revision_number, request):
    title, body, discipline = request.title.strip(), request.body.strip(), request.discipline.strip()
    if not title or not body or not discipline:
        raise HTTPException(status_code=422, detail="title, body, and discipline must not be blank")
    now = utc_now()
    tags = [str(tag).strip() for tag in request.tags if str(tag).strip()]
    cur = conn.execute("""INSERT INTO wiki_page_revisions
        (page_id,revision_number,title,summary,body,language,discipline,tags_json,references_json,author,change_note,provenance_json,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (page_id, revision_number, title, request.summary.strip(), body,
         request.language.strip() or "en", discipline,
         json.dumps(tags, ensure_ascii=False), json.dumps(request.references, ensure_ascii=False, sort_keys=True),
         request.author.strip(), request.change_note.strip(),
         json.dumps(request.provenance, ensure_ascii=False, sort_keys=True), now))
    revision_id = cur.lastrowid
    conn.execute("INSERT INTO wiki_fts(rowid,slug,title,summary,body,tags) VALUES(?,?,?,?,?,?)",
        (revision_id, slug, title, request.summary, body, " ".join(tags)))
    conn.execute("UPDATE wiki_pages SET current_revision_id=?,updated_at=? WHERE id=?", (revision_id, now, page_id))
    return revision_id

@app.post("/wiki/pages")
def create_wiki_page(request: WikiPageCreate):
    slug, now = _wiki_slug(request.slug), utc_now()
    with connection() as conn:
        if conn.execute("SELECT 1 FROM wiki_pages WHERE slug=?", (slug,)).fetchone():
            raise HTTPException(status_code=409, detail="Wiki slug already exists; append a revision instead")
        page_id = conn.execute("INSERT INTO wiki_pages(slug,created_at,updated_at) VALUES(?,?,?)", (slug, now, now)).lastrowid
        revision_id = _insert_wiki_revision(conn, page_id, slug, 1, request)
        row = conn.execute("""SELECT r.id revision_id,r.page_id,p.slug,r.revision_number,r.title,r.summary,r.body,r.language,r.discipline,r.tags_json,r.references_json,r.author,r.change_note,r.provenance_json,r.created_at,p.current_revision_id
            FROM wiki_page_revisions r JOIN wiki_pages p ON p.id=r.page_id WHERE r.id=?""", (revision_id,)).fetchone()
    return _wiki_revision_dict(row)

@app.get("/wiki/pages")
def list_wiki_pages(q: str | None = None, discipline: str | None = None,
                    language: str | None = None, tag: str | None = None,
                    limit: int = Query(default=50, ge=1, le=200)):
    clauses, params = [], []
    if q:
        terms = [term.replace('"', ' ').strip() for term in q.split() if term.strip()]
        if terms:
            match = " AND ".join('"' + term.replace('"', '') + '"' for term in terms)
            clauses.append("p.current_revision_id IN (SELECT rowid FROM wiki_fts WHERE wiki_fts MATCH ?)")
            params.append(match)
    if discipline:
        clauses.append("r.discipline=?"); params.append(discipline)
    if language:
        clauses.append("r.language=?"); params.append(language)
    if tag:
        clauses.append("EXISTS (SELECT 1 FROM json_each(r.tags_json) WHERE value=?)"); params.append(tag)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(limit)
    with connection() as conn:
        rows = conn.execute("""SELECT r.id revision_id,r.page_id,p.slug,r.revision_number,r.title,r.summary,r.body,r.language,r.discipline,r.tags_json,r.references_json,r.author,r.change_note,r.provenance_json,r.created_at,p.current_revision_id
            FROM wiki_pages p JOIN wiki_page_revisions r ON r.id=p.current_revision_id""" + where + " ORDER BY p.updated_at DESC LIMIT ?", params).fetchall()
    return [_wiki_revision_dict(row) for row in rows]

@app.get("/wiki/pages/{slug}")
def get_wiki_page(slug: str, include_history: bool = False):
    slug = _wiki_slug(slug)
    with connection() as conn:
        page = conn.execute("SELECT id,slug,current_revision_id FROM wiki_pages WHERE slug=?", (slug,)).fetchone()
        if not page:
            raise HTTPException(status_code=404, detail="Wiki page not found")
        if include_history:
            rows = conn.execute("""SELECT r.id revision_id,r.page_id,p.slug,r.revision_number,r.title,r.summary,r.body,r.language,r.discipline,r.tags_json,r.references_json,r.author,r.change_note,r.provenance_json,r.created_at,p.current_revision_id
                FROM wiki_page_revisions r JOIN wiki_pages p ON p.id=r.page_id WHERE p.id=? ORDER BY r.revision_number DESC""", (page["id"],)).fetchall()
        else:
            rows = conn.execute("""SELECT r.id revision_id,r.page_id,p.slug,r.revision_number,r.title,r.summary,r.body,r.language,r.discipline,r.tags_json,r.references_json,r.author,r.change_note,r.provenance_json,r.created_at,p.current_revision_id
                FROM wiki_page_revisions r JOIN wiki_pages p ON p.id=r.page_id WHERE r.id=?""", (page["current_revision_id"],)).fetchall()
    values = [_wiki_revision_dict(row) for row in rows]
    return {"slug": slug, "current_revision_id": page["current_revision_id"], "revisions": values} if include_history else values[0]

@app.post("/wiki/pages/{slug}/revisions")
def add_wiki_revision(slug: str, request: WikiRevisionCreate):
    slug = _wiki_slug(slug)
    with connection() as conn:
        page = conn.execute("SELECT id FROM wiki_pages WHERE slug=?", (slug,)).fetchone()
        if not page:
            raise HTTPException(status_code=404, detail="Wiki page not found")
        number = conn.execute("SELECT COALESCE(MAX(revision_number),0)+1 FROM wiki_page_revisions WHERE page_id=?", (page["id"],)).fetchone()[0]
        revision_id = _insert_wiki_revision(conn, page["id"], slug, number, request)
        row = conn.execute("""SELECT r.id revision_id,r.page_id,p.slug,r.revision_number,r.title,r.summary,r.body,r.language,r.discipline,r.tags_json,r.references_json,r.author,r.change_note,r.provenance_json,r.created_at,p.current_revision_id
            FROM wiki_page_revisions r JOIN wiki_pages p ON p.id=r.page_id WHERE r.id=?""", (revision_id,)).fetchone()
    return _wiki_revision_dict(row)

@app.get("/wiki/search")
def search_wiki(q: str = Query(min_length=1, max_length=300),
                discipline: str | None = None, language: str | None = None,
                include_history: bool = False, limit: int = Query(default=20, ge=1, le=100)):
    terms = [term.replace('"', ' ').strip() for term in q.split() if term.strip()]
    if not terms:
        return []
    match = " AND ".join('"' + term.replace('"', '') + '"' for term in terms)
    clauses, params = ["wiki_fts MATCH ?"], [match]
    if not include_history:
        clauses.append("p.current_revision_id=r.id")
    if discipline:
        clauses.append("r.discipline=?"); params.append(discipline)
    if language:
        clauses.append("r.language=?"); params.append(language)
    params.append(limit)
    with connection() as conn:
        rows = conn.execute("""SELECT r.id revision_id,r.page_id,p.slug,r.revision_number,r.title,r.summary,r.body,r.language,r.discipline,r.tags_json,r.references_json,r.author,r.change_note,r.provenance_json,r.created_at,p.current_revision_id
            FROM wiki_fts JOIN wiki_page_revisions r ON r.id=wiki_fts.rowid JOIN wiki_pages p ON p.id=r.page_id
            WHERE """ + " AND ".join(clauses) + " ORDER BY bm25(wiki_fts),r.created_at DESC LIMIT ?", params).fetchall()
    return [_wiki_revision_dict(row) for row in rows]

@app.get("/wiki/view/{slug}", response_class=HTMLResponse)
def wiki_page_view(slug: str):
    safe_slug = _wiki_slug(slug)
    return HTMLResponse("""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>STEM Wiki Page</title><style>body{font:16px system-ui;max-width:900px;margin:2rem auto;padding:0 1rem;color:#202124}.muted{color:#666}pre{white-space:pre-wrap;overflow-wrap:anywhere}li{margin:.5rem 0}</style></head><body><p><a href="/wiki">← STEM Wiki</a></p><p id="status" class="muted">Loading page…</p><article id="page"></article><script>const slug=decodeURIComponent(location.pathname.split('/').pop()),status=document.querySelector('#status'),root=document.querySelector('#page');fetch('/wiki/pages/'+encodeURIComponent(slug)).then(async r=>{const data=await r.json();if(!r.ok)throw new Error(data.detail||'Page not found');document.title=data.title+' · STEM Wiki';status.textContent=data.discipline+' · '+data.language+' · revision '+data.revision_number;const h=document.createElement('h1');h.textContent=data.title;const summary=document.createElement('p');summary.textContent=data.summary;const body=document.createElement('pre');body.textContent=data.body;const tags=document.createElement('p');tags.className='muted';tags.textContent='Tags: '+data.tags.join(', ');const history=document.createElement('a');history.href='/wiki/pages/'+encodeURIComponent(slug)+'?include_history=true';history.textContent='View revision history (JSON)';root.append(h,summary,body,tags);const refs=document.createElement('section'),rh=document.createElement('h2'),list=document.createElement('ul');rh.textContent='References';for(const ref of data.references){const li=document.createElement('li');if(ref.url&&/^https?:\\/\\//i.test(ref.url)){const a=document.createElement('a');a.href=ref.url;a.textContent=ref.title||ref.url;a.rel='noopener noreferrer';a.target='_blank';li.append(a)}else li.textContent=ref.title||JSON.stringify(ref);list.append(li)}refs.append(rh,list);root.append(refs,history);const artifactSection=document.createElement('section'),ah=document.createElement('h2'),artifactList=document.createElement('div');ah.textContent='Structured STEM artifacts';artifactSection.append(ah,artifactList);root.append(artifactSection);async function loadArtifacts(){const ar=await fetch('/wiki/pages/'+encodeURIComponent(slug)+'/artifacts');const items=await ar.json();artifactList.replaceChildren();for(const item of items){const card=document.createElement('section'),h=document.createElement('h3'),meta=document.createElement('p'),pre=document.createElement('pre');h.textContent=item.name;meta.className='muted';meta.textContent=item.artifact_type+' · artifact #'+item.artifact_id+' · page revision '+item.page_revision_id;pre.textContent=JSON.stringify(item.data,null,2);card.append(h,meta,pre);artifactList.append(card)}}loadArtifacts();const artifactForm=document.createElement('form'),formTitle=document.createElement('h3'),typeLabel=document.createElement('label'),typeSelect=document.createElement('select'),nameLabel=document.createElement('label'),nameInput=document.createElement('input'),dataLabel=document.createElement('label'),dataInput=document.createElement('textarea'),submit=document.createElement('button');formTitle.textContent='Add structured artifact';typeLabel.textContent='Artifact type';for(const t of ['equation','constant','algorithm','experiment']){const o=document.createElement('option');o.value=t;o.textContent=t;typeSelect.append(o)}nameLabel.textContent='Artifact name';nameInput.required=true;nameInput.placeholder='e.g. Newton second law';dataLabel.textContent='Data (JSON)';dataInput.required=true;dataInput.rows=8;dataInput.placeholder='Equation: {"expression":"F = m * a","variables":[{"symbol":"F","meaning":"net force","unit":"N"}]}';submit.type='submit';submit.textContent='Save artifact';artifactForm.append(formTitle,typeLabel,typeSelect,nameLabel,nameInput,dataLabel,dataInput,submit);artifactSection.append(artifactForm);typeSelect.addEventListener('change',()=>{const examples={equation:'{"expression":"F = m * a","variables":[{"symbol":"F","meaning":"net force","unit":"N"}]}',constant:'{"value":9.80665,"unit":"m/s^2","uncertainty":"context-dependent"}',algorithm:'{"pseudocode":"total <- sum(values)","inputs":["values"],"outputs":["total"]}',experiment:'{"procedure":["Record distance","Measure elapsed time"],"independent_variables":["distance"],"dependent_variables":["elapsed time"]}'};dataInput.placeholder=examples[typeSelect.value]});artifactForm.addEventListener('submit',async e=>{e.preventDefault();let data;try{data=JSON.parse(dataInput.value)}catch(err){status.textContent='Artifact data must be valid JSON';return}status.textContent='Saving artifact…';const ar=await fetch('/wiki/pages/'+encodeURIComponent(slug)+'/artifacts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({artifact_type:typeSelect.value,name:nameInput.value,data})});const result=await ar.json();if(!ar.ok){status.textContent=result.detail||'Could not save artifact';return}status.textContent='Saved artifact: '+result.name;nameInput.value='';dataInput.value='';await loadArtifacts()})}).catch(e=>{status.textContent=e.message});</script></body></html>""")

@app.get("/wiki", response_class=HTMLResponse)
def wiki_home():
    return HTMLResponse("""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>STEM Wiki</title><style>body{font:16px system-ui;max-width:1000px;margin:2rem auto;padding:0 1rem;color:#202124}input,textarea,select{box-sizing:border-box;padding:.65rem;width:100%;margin:.25rem 0 1rem}button{padding:.7rem 1rem;cursor:pointer}.page{border-top:1px solid #ddd;padding:1rem 0}.muted{color:#666}pre{white-space:pre-wrap;overflow-wrap:anywhere}.layout{display:grid;grid-template-columns:1fr 1fr;gap:2rem}@media(max-width:700px){.layout{grid-template-columns:1fr}}</style></head><body><p><a href="/">← Records Search Engine</a></p><h1>STEM Wiki</h1><p class="muted">Versioned STEM knowledge pages with references, tags, and provenance. New revisions are preserved rather than overwritten.</p><form id="search"><label for="q">Search pages</label><input id="q" placeholder="e.g. force acceleration" required><label for="discipline">Discipline (optional)</label><input id="discipline" placeholder="physics, mathematics, engineering…"><button>Search wiki</button></form><p id="status" class="muted"></p><section id="results"></section><hr><h2>Create a page</h2><form id="create"><label>URL slug</label><input id="slug" placeholder="newton-second-law" required><label>Title</label><input id="title" required><label>Discipline</label><input id="kind" placeholder="physics" required><label>Summary</label><input id="summary"><label>Tags (comma separated)</label><input id="tags" placeholder="mechanics, classical physics"><label>Body / explanation</label><textarea id="body" rows="7" required></textarea><label>Reference URLs (one per line)</label><textarea id="refs" rows="3" placeholder="https://example.org/reference"></textarea><label>Author / contributor</label><input id="author"><button>Create wiki page</button></form><script>const status=document.querySelector('#status'),results=document.querySelector('#results');function renderPages(items){results.replaceChildren();for(const x of items){const article=document.createElement('article');article.className='page';const h=document.createElement('h2'),a=document.createElement('a');a.href='/wiki/view/'+encodeURIComponent(x.slug);a.textContent=x.title;h.append(a);const meta=document.createElement('p');meta.className='muted';meta.textContent=x.discipline+' · '+x.language+' · revision '+x.revision_number+' · '+x.tags.join(', ');const p=document.createElement('p');p.textContent=x.summary||x.body.slice(0,280);article.append(h,meta,p);results.append(article)}}document.querySelector('#search').addEventListener('submit',async e=>{e.preventDefault();status.textContent='Searching…';const params=new URLSearchParams({q:document.querySelector('#q').value});const d=document.querySelector('#discipline').value.trim();if(d)params.set('discipline',d);const r=await fetch('/wiki/search?'+params);const data=await r.json();status.textContent=data.length+' result(s)';renderPages(data)});document.querySelector('#create').addEventListener('submit',async e=>{e.preventDefault();status.textContent='Creating page…';const refs=document.querySelector('#refs').value.split(/\\r?\\n/).map(x=>x.trim()).filter(Boolean).map(url=>({url}));const payload={slug:document.querySelector('#slug').value,title:document.querySelector('#title').value,discipline:document.querySelector('#kind').value,summary:document.querySelector('#summary').value,body:document.querySelector('#body').value,tags:document.querySelector('#tags').value.split(',').map(x=>x.trim()).filter(Boolean),references:refs,author:document.querySelector('#author').value};const r=await fetch('/wiki/pages',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});const data=await r.json();if(!r.ok){status.textContent=data.detail||'Could not create page';return}status.textContent='Created '+data.title+' (revision '+data.revision_number+')';renderPages([data])});</script></body></html>""")

@app.get("/",response_class=HTMLResponse)
def home():
    return HTMLResponse("""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Records Search Engine</title><style>body{font:16px system-ui;max-width:900px;margin:3rem auto;padding:0 1rem;color:#202124}input{padding:.7rem;width:min(65%,520px)}button{padding:.75rem;cursor:pointer}.muted{color:#666}.result{border-top:1px solid #ddd;padding:1rem 0}</style></head><body><h1>Records Search Engine</h1><p><a href="/wiki">Open STEM Wiki</a> · <a href="/github/explorer">Open GitHub Relationship Explorer</a></p><p class="muted">Search indexed web records or add a public HTML page. Source versions are preserved.</p><form id="search"><input id="q" placeholder="Search indexed records" required><label><input id="history" type="checkbox" style="width:auto"> Include historical versions</label><button>Search</button></form><h2>Add a source</h2><form id="ingest"><input id="url" type="url" placeholder="https://example.org/page" required><button>Index URL</button></form><p id="status" class="muted"></p><section id="results"></section><script>const status=document.querySelector('#status'),results=document.querySelector('#results');document.querySelector('#search').addEventListener('submit',async e=>{e.preventDefault();status.textContent='Searching…';results.replaceChildren();const q=document.querySelector('#q').value,history=document.querySelector('#history').checked,r=await fetch('/search?q='+encodeURIComponent(q)+'&include_history='+history),data=await r.json();status.textContent=data.length+' result(s)';for(const x of data){const el=document.createElement('article');el.className='result';const h=document.createElement('h3'),a=document.createElement('a');a.href=x.url;a.target='_blank';a.rel='noopener noreferrer';a.textContent=x.title;h.append(a);const p=document.createElement('p');p.textContent=x.excerpt.replace(/<\/?mark>/g,'');const s=document.createElement('small');s.textContent='Source #'+x.source_id+' · Version '+x.version_number+(x.is_current?' (current)':' (historical)');el.append(h,p,s);results.append(el)}});document.querySelector('#ingest').addEventListener('submit',async e=>{e.preventDefault();status.textContent='Retrieving and indexing…';const r=await fetch('/ingest',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:document.querySelector('#url').value})}),data=await r.json();status.textContent=r.ok?'Indexed: '+data.title+' ('+data.outcome+')':(data.detail||'Ingestion failed')});</script></body></html>""")




@app.post("/github/relationship-revalidations")
def revalidate_github_relationship(request: RepositoryRelationshipRevalidationCreate):
    """Append a revalidation decision tied to a recorded source-change event."""
    decision = request.decision.strip().lower()
    reviewer = request.reviewer.strip()[:200] or "unspecified"
    rationale = request.rationale.strip()[:5000]
    if decision not in {"reaffirmed", "revised", "disputed", "no_longer_supported"}:
        raise HTTPException(status_code=422, detail="decision must be reaffirmed, revised, disputed, or no_longer_supported")
    if not rationale:
        raise HTTPException(status_code=422, detail="rationale is required")
    if request.confidence is not None and not 0 <= request.confidence <= 1:
        raise HTTPException(status_code=422, detail="confidence must be between 0 and 1")
    now = utc_now()
    with connection() as conn:
        event = conn.execute("""SELECT e.*,rr.from_source_id,rr.to_source_id,rr.predicate old_predicate,
            rr.confidence old_confidence,rr.status old_status,rr.provenance_json old_provenance
            FROM relationship_change_events e JOIN repository_relationships rr ON rr.id=e.relationship_id
            WHERE e.id=?""", (request.event_id,)).fetchone()
        if event is None:
            raise HTTPException(status_code=404, detail="Relationship change event not found")
        if conn.execute("SELECT 1 FROM relationship_revalidations WHERE event_id=?", (request.event_id,)).fetchone():
            raise HTTPException(status_code=409, detail="This change event already has a revalidation decision; create a new source-change event for another review")
        left = conn.execute("SELECT current_version_id FROM sources WHERE id=?", (event["from_source_id"],)).fetchone()
        right = conn.execute("SELECT current_version_id FROM sources WHERE id=?", (event["to_source_id"],)).fetchone()
        if left is None or right is None or left["current_version_id"] is None or right["current_version_id"] is None:
            raise HTTPException(status_code=409, detail="Both relationship sources must have current versions before revalidation")
        predicate = (request.predicate or event["old_predicate"]).strip()
        if decision in {"reaffirmed", "revised"} and (not predicate or len(predicate) > 200):
            raise HTTPException(status_code=422, detail="predicate must contain 1 to 200 characters")
        replacement_id = None
        if decision in {"reaffirmed", "revised"}:
            old_provenance = json.loads(event["old_provenance"] or "{}")
            new_provenance = {
                "revalidation_of_relationship_id": event["relationship_id"],
                "revalidation_event_id": request.event_id,
                "decision": decision, "reviewer": reviewer, "rationale": rationale,
                "evidence_snapshot": request.evidence_snapshot,
                "review_provenance": request.provenance,
                "source_versions": {"from": left["current_version_id"], "to": right["current_version_id"]},
                "previous_source_versions": old_provenance.get("source_versions", {}),
            }
            cur = conn.execute("""INSERT INTO repository_relationships
                (from_source_id,to_source_id,predicate,confidence,status,provenance_json,created_at)
                VALUES(?,?,?,?,?,?,?)""", (event["from_source_id"], event["to_source_id"], predicate,
                request.confidence if request.confidence is not None else event["old_confidence"],
                "supported", json.dumps(new_provenance, ensure_ascii=False, sort_keys=True), now))
            replacement_id = cur.lastrowid
        cur = conn.execute("""INSERT INTO relationship_revalidations
            (event_id,relationship_id,decision,reviewer,rationale,replacement_relationship_id,evidence_snapshot_json,provenance_json,created_at)
            VALUES(?,?,?,?,?,?,?,?,?)""", (request.event_id, event["relationship_id"], decision, reviewer, rationale,
            replacement_id, json.dumps(request.evidence_snapshot, ensure_ascii=False, sort_keys=True),
            json.dumps(request.provenance, ensure_ascii=False, sort_keys=True), now))
        return {"revalidation_id": cur.lastrowid, "event_id": request.event_id,
            "relationship_id": event["relationship_id"], "decision": decision,
            "replacement_relationship_id": replacement_id, "reviewer": reviewer,
            "rationale": rationale, "created_at": now,
            "note": "Historical relationship and prior review records are retained; current graph omits superseded or no-longer-supported edges."}


@app.get("/github/relationship-revalidations")
def list_github_relationship_revalidations(owner: str | None = None,
                                           relationship_id: int | None = Query(default=None, ge=1),
                                           limit: int = Query(default=100, ge=1, le=500)):
    clauses = ["1=1"]
    params = []
    if owner:
        clauses.append("""(lower(json_extract(fs.provenance_json,'$.owner'))=lower(?)
            OR lower(json_extract(ts.provenance_json,'$.owner'))=lower(?))""")
        params.extend([owner.strip(), owner.strip()])
    if relationship_id is not None:
        clauses.append("rv.relationship_id=?")
        params.append(relationship_id)
    params.append(limit)
    with connection() as conn:
        rows = conn.execute("""SELECT rv.*,e.source_id changed_source_id,e.previous_version_id,e.current_version_id,
            fs.canonical_url from_url,ts.canonical_url to_url
            FROM relationship_revalidations rv
            JOIN relationship_change_events e ON e.id=rv.event_id
            JOIN repository_relationships rr ON rr.id=rv.relationship_id
            JOIN sources fs ON fs.id=rr.from_source_id JOIN sources ts ON ts.id=rr.to_source_id
            WHERE """ + " AND ".join(clauses) + " ORDER BY rv.created_at DESC,rv.id DESC LIMIT ?", params).fetchall()
    return [{"revalidation_id": r["id"], "event_id": r["event_id"], "relationship_id": r["relationship_id"],
        "decision": r["decision"], "reviewer": r["reviewer"], "rationale": r["rationale"],
        "replacement_relationship_id": r["replacement_relationship_id"], "changed_source_id": r["changed_source_id"],
        "previous_version_id": r["previous_version_id"], "current_version_id": r["current_version_id"],
        "from_url": r["from_url"], "to_url": r["to_url"],
        "evidence_snapshot": json.loads(r["evidence_snapshot_json"]), "provenance": json.loads(r["provenance_json"]),
        "created_at": r["created_at"]} for r in rows]


@app.get("/github/relationship-change-events")
def list_github_relationship_change_events(owner: str | None = None,
                                           relationship_id: int | None = Query(default=None, ge=1),
                                           source_id: int | None = Query(default=None, ge=1),
                                           limit: int = Query(default=100, ge=1, le=500)):
    """List immutable source-change events that may invalidate relationship evidence."""
    clauses = ["1=1"]
    params = []
    if owner:
        clauses.append("""(lower(json_extract(fs.provenance_json,'$.owner'))=lower(?)
            OR lower(json_extract(ts.provenance_json,'$.owner'))=lower(?))""")
        params.extend([owner.strip(), owner.strip()])
    if relationship_id is not None:
        clauses.append("e.relationship_id=?")
        params.append(relationship_id)
    if source_id is not None:
        clauses.append("e.source_id=?")
        params.append(source_id)
    params.append(limit)
    with connection() as conn:
        rows = conn.execute("""SELECT e.*,fs.canonical_url from_url,ts.canonical_url to_url,
            sv.title source_title,json_extract(sv.provenance_json,'$.path') source_path,
            json_extract(s.provenance_json,'$.repository') source_repository
            FROM relationship_change_events e
            JOIN repository_relationships rr ON rr.id=e.relationship_id
            JOIN sources fs ON fs.id=rr.from_source_id JOIN sources ts ON ts.id=rr.to_source_id
            JOIN sources s ON s.id=e.source_id
            JOIN document_versions sv ON sv.id=e.current_version_id
            WHERE """ + " AND ".join(clauses) +
            " ORDER BY e.created_at DESC,e.id DESC LIMIT ?", params).fetchall()
    return [{"event_id": r["id"], "relationship_id": r["relationship_id"], "source_id": r["source_id"],
        "source_repository": r["source_repository"], "source_path": r["source_path"],
        "source_title": r["source_title"], "from_url": r["from_url"], "to_url": r["to_url"],
        "previous_version_id": r["previous_version_id"], "current_version_id": r["current_version_id"],
        "reviewed_version_id": r["reviewed_version_id"],
        "previous_content_hash": r["previous_content_hash"], "current_content_hash": r["current_content_hash"],
        "relationship_status_at_change": r["relationship_status"], "predicate": r["predicate"],
        "reason": r["reason"], "created_at": r["created_at"],
        "revalidation_recommended": True} for r in rows]



@app.get("/github/relationship-change-comparisons")
def compare_github_relationship_change(event_id: int = Query(ge=1),
                                       context_chars: int = Query(default=12000, ge=1000, le=30000),
                                       max_diff_lines: int = Query(default=1500, ge=100, le=5000)):
    """Return a bounded unified diff for the exact old/current versions named by a change event."""
    with connection() as conn:
        row = conn.execute("""SELECT e.*,rr.from_source_id,rr.to_source_id,rr.predicate,
            old.title previous_title,old.body previous_body,old.content_hash actual_previous_hash,
            current.title current_title,current.body current_body,current.content_hash actual_current_hash,
            s.canonical_url changed_source_url,json_extract(s.provenance_json,'$.repository') changed_repository,
            json_extract(s.provenance_json,'$.path') changed_path,
            other.current_version_id other_current_version_id
            FROM relationship_change_events e
            JOIN repository_relationships rr ON rr.id=e.relationship_id
            JOIN document_versions old ON old.id=e.previous_version_id
            JOIN document_versions current ON current.id=e.current_version_id
            JOIN sources s ON s.id=e.source_id
            JOIN sources other ON other.id=CASE WHEN e.source_id=rr.from_source_id THEN rr.to_source_id ELSE rr.from_source_id END
            WHERE e.id=?""", (event_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Relationship change event or its source versions were not found")
        other_version = conn.execute("SELECT id,title,body,content_hash FROM document_versions WHERE id=?",
                                     (row["other_current_version_id"],)).fetchone() if row["other_current_version_id"] else None
    old_lines = (row["previous_body"] or "").splitlines()
    new_lines = (row["current_body"] or "").splitlines()
    diff_lines = list(difflib.unified_diff(old_lines, new_lines,
        fromfile=f"version-{row['previous_version_id']} (previous)",
        tofile=f"version-{row['current_version_id']} (current)", lineterm=""))
    truncated = len(diff_lines) > max_diff_lines
    added_line_count = sum(1 for line in diff_lines if line.startswith("+") and not line.startswith("+++"))
    removed_line_count = sum(1 for line in diff_lines if line.startswith("-") and not line.startswith("---"))
    diff_lines = diff_lines[:max_diff_lines]
    old_hash_matches = row["actual_previous_hash"] == row["previous_content_hash"]
    new_hash_matches = row["actual_current_hash"] == row["current_content_hash"]
    return {
        "event_id": event_id, "relationship_id": row["relationship_id"], "predicate": row["predicate"],
        "source_id": row["source_id"], "source_url": row["changed_source_url"],
        "source_repository": row["changed_repository"], "source_path": row["changed_path"],
        "previous_version_id": row["previous_version_id"], "current_version_id": row["current_version_id"],
        "previous_title": row["previous_title"], "current_title": row["current_title"],
        "previous_content_hash": row["previous_content_hash"], "current_content_hash": row["current_content_hash"],
        "stored_hashes_match_versions": old_hash_matches and new_hash_matches,
        "previous_line_count": len(old_lines), "current_line_count": len(new_lines),
        "added_lines": added_line_count,
        "removed_lines": removed_line_count,
        "diff_lines": diff_lines, "diff_truncated": truncated, "diff_line_limit": max_diff_lines,
        "other_endpoint_current_version": ({
            "version_id": other_version["id"], "title": other_version["title"],
            "content_hash": other_version["content_hash"],
            "body_excerpt": (other_version["body"] or "")[:context_chars]
        } if other_version else None),
        "note": "This is a textual line diff, not a semantic assessment. Confirm both versions and the other endpoint before deciding."
    }


@app.get("/github/explorer", response_class=HTMLResponse)
def github_relationship_explorer():
    """Small browser interface for repository search, candidate review, and graph inspection."""
    return HTMLResponse("""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>GitHub Relationship Explorer</title><style>
body{font:16px system-ui;max-width:1200px;margin:2rem auto;padding:0 1rem;color:#202124}input,select,textarea{box-sizing:border-box;padding:.65rem;width:100%;margin:.25rem 0 .7rem}button{padding:.6rem .85rem;cursor:pointer;margin:.2rem;border:1px solid #bbb;border-radius:6px;background:#f8f8f8}button.primary{background:#202124;color:#fff}button.reject{border-color:#b33}button.accept{border-color:#287b48}.muted{color:#666}.grid{display:grid;grid-template-columns:minmax(250px,1fr) minmax(300px,2fr);gap:1.5rem}.panel,.item{border:1px solid #ddd;border-radius:8px;padding:1rem;margin:.75rem 0}.item h3{margin:.2rem 0 .5rem}.pill{font-size:.8rem;border:1px solid #ccc;border-radius:999px;padding:.1rem .5rem}.row{display:flex;gap:.5rem;flex-wrap:wrap;align-items:center}.row>*{flex:1}.item pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f7f7f7;padding:.6rem}.small{font-size:.9rem}@media(max-width:750px){.grid{grid-template-columns:1fr}}
</style></head><body><p><a href="/">← Records Search Engine</a> · <a href="/docs">API docs</a></p><h1>GitHub Relationship Explorer</h1><p class="muted">Browse indexed repositories, inspect heuristic candidates, record review decisions, and inspect explicitly stored graph edges. Candidate scores rank evidence; they are not probabilities or proof.</p>
<section class="panel"><div class="row"><div><label for="owner">GitHub owner</label><input id="owner" placeholder="your-github-owner" required></div><div><label for="reviewer">Reviewer label</label><input id="reviewer" value="manual-review"></div></div><div class="row"><div><label for="max">Max sources to scan (2–500)</label><input id="max" type="number" min="2" max="500" value="100"></div><div><label for="minscore">Minimum candidate score</label><input id="minscore" type="number" min="1" max="100" value="10"></div></div><button id="load" class="primary">Load repositories and candidates</button><button id="refresh">Refresh graph and review log</button><p id="status" class="muted" role="status">Enter the GitHub owner used when indexing repositories.</p></section>
<div class="grid"><section><h2>Indexed repositories</h2><div id="repos"></div><h2>Review history</h2><div id="reviews"></div></section><section><h2>Relationship candidates</h2><div id="candidates"></div><h2>Supported graph edges</h2><div id="graph"></div><h2>Stale evidence awaiting review</h2><div id="stale"></div></section></div>
<script>
const $=id=>document.getElementById(id),status=$('status');
function esc(value){return String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
async function api(path,options){const r=await fetch(path,options),data=await r.json().catch(()=>({detail:'Invalid server response'}));if(!r.ok)throw new Error(data.detail||('Request failed: '+r.status));return data}
function owner(){const v=$('owner').value.trim();if(!v)throw new Error('Enter a GitHub owner first.');return v}
function endpoint(path,params){return path+'?'+new URLSearchParams(params)}
async function loadAll(){try{const o=owner();status.textContent='Loading indexed repositories and relationship candidates…';const [repos,cands]=await Promise.all([api(endpoint('/github/repositories',{owner:o,limit:'200'})),api(endpoint('/github/relationship-candidates',{owner:o,max_sources:$('max').value,min_score:$('minscore').value,limit:'100'}))]);renderRepos(repos);renderCandidates(cands);status.textContent='Loaded '+(repos.repositories?.length??repos.length??0)+' repository records; scanned '+cands.sources_scanned+' of '+cands.sources_available+' sources; '+cands.candidate_count_returned+' candidates returned.'+(cands.scan_truncated?' The source scan was truncated.':'');await refreshReviewAndGraph()}catch(e){status.textContent=e.message}}
function renderRepos(data){const items=Array.isArray(data)?data:(data.repositories||[]),root=$('repos');root.replaceChildren();if(!items.length){root.textContent='No indexed repositories found for this owner yet. Ingest repositories through the API first.';return}for(const x of items){const d=document.createElement('article');d.className='item';d.innerHTML='<h3>'+esc(x.repository||x.name||'Repository')+'</h3><p class="muted small">'+esc(x.file_count??x.indexed_files??'')+' indexed files</p><p><a href="'+esc(x.browse_url||('/github/repositories/'+encodeURIComponent(owner())+'/'+encodeURIComponent(x.repository||x.name)))+'">Browse files</a></p>';root.append(d)}}
function renderCandidates(data){const root=$('candidates');root.replaceChildren();if(!data.candidates.length){root.textContent='No candidates met this threshold in the scanned source set.';return}for(const c of data.candidates){const d=document.createElement('article');d.className='item';d.innerHTML='<div class="row"><h3>'+esc(c.from.repository)+' / '+esc(c.from.path)+'</h3><span class="pill">score '+esc(c.score)+'</span></div><p>↔ <b>'+esc(c.to.repository)+' / '+esc(c.to.path)+'</b></p><p class="small"><a target="_blank" rel="noopener noreferrer" href="'+esc(c.from.file_url)+'">Open source file</a> · <a target="_blank" rel="noopener noreferrer" href="'+esc(c.to.file_url)+'">Open related file</a></p><p class="muted small">Source IDs '+esc(c.from_source_id)+' → '+esc(c.to_source_id)+' · versions '+esc(c.from.version_id)+' / '+esc(c.to.version_id)+'</p><details><summary>Evidence details</summary><pre>'+esc(JSON.stringify(c.evidence,null,2))+'</pre></details><label>Relationship predicate</label><input class="predicate" value="'+esc(c.suggested_predicate)+'"><label>Rationale (required for this decision)</label><textarea class="rationale" rows="2" placeholder="Explain what you checked and why"></textarea><div class="row"><button class="accept">Accept and add supported edge</button><button class="reject">Reject candidate</button><button class="dispute">Mark disputed</button></div>';const predicate=d.querySelector('.predicate'),rationale=d.querySelector('.rationale');async function decide(decision){const why=rationale.value.trim();if(!why){alert('Add a short rationale before recording a decision.');rationale.focus();return}try{const result=await api('/github/relationship-reviews',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({from_source_id:c.from_source_id,to_source_id:c.to_source_id,decision,predicate:predicate.value.trim()||c.suggested_predicate,reviewer:$('reviewer').value.trim()||'unspecified',rationale:why,confidence:null,evidence_snapshot:c.evidence,provenance:{method:'github_relationship_explorer',heuristic_score:c.score,source_versions:{from:c.from.version_id,to:c.to.version_id},content_hashes:{from:c.from.content_hash||null,to:c.to.content_hash||null}}})});d.querySelectorAll('button').forEach(b=>b.disabled=true);d.querySelector('.pill').textContent=decision;status.textContent='Recorded review #'+result.review_id+' ('+decision+').';await refreshReviewAndGraph()}catch(e){status.textContent=e.message}}d.querySelector('.accept').addEventListener('click',()=>decide('accepted'));d.querySelector('.reject').addEventListener('click',()=>decide('rejected'));d.querySelector('.dispute').addEventListener('click',()=>decide('disputed'));root.append(d)}}
async function refreshReviewAndGraph(){try{const o=owner();const [reviews,graph,events,revalidations]=await Promise.all([api(endpoint('/github/relationship-reviews',{owner:o,limit:'50'})),api(endpoint('/github/relationship-graph',{owner:o,limit:'500'})),api(endpoint('/github/relationship-change-events',{owner:o,limit:'200'})),api(endpoint('/github/relationship-revalidations',{owner:o,limit:'200'}))]);renderReviews(reviews);renderGraph(graph);renderStale(events,revalidations)}catch(e){status.textContent=e.message}}
function renderStale(events,revalidations){const root=$('stale');root.replaceChildren();const resolved=new Set(revalidations.map(r=>r.event_id));const pending=events.filter(e=>!resolved.has(e.event_id));if(!pending.length){root.textContent='No unresolved relationship-change events found for this owner.';return}for(const e of pending){const d=document.createElement('article');d.className='item';d.innerHTML='<h3>Relationship #'+esc(e.relationship_id)+' · '+esc(e.predicate)+'</h3><p class="muted small">Event #'+esc(e.event_id)+' · source '+esc(e.source_id)+' · '+esc(e.source_repository||'')+' / '+esc(e.source_path||'')+'</p><p>Version '+esc(e.previous_version_id)+' → '+esc(e.current_version_id)+'</p><p class="small">Previous hash: '+esc(e.previous_content_hash)+'<br>Current hash: '+esc(e.current_content_hash)+'</p><p><a target="_blank" rel="noopener noreferrer" href="'+esc(e.from_url)+'">Open relationship source A</a> · <a target="_blank" rel="noopener noreferrer" href="'+esc(e.to_url)+'">Open source B</a></p><button class="show-diff">Compare exact source versions</button><p class="diff-summary muted small"></p><pre class="version-diff" hidden></pre><details class="other-context" hidden><summary>Other endpoint current-version excerpt</summary><pre class="other-version"></pre></details><label>Revalidation rationale</label><textarea class="reval-rationale" rows="2" placeholder="Describe the comparison of current versions"></textarea><label>Predicate (optional; used for revised decision)</label><input class="reval-predicate" value="'+esc(e.predicate)+'"><div class="row"><button data-decision="reaffirmed" class="accept">Reaffirm</button><button data-decision="revised">Revise</button><button data-decision="disputed">Dispute</button><button data-decision="no_longer_supported" class="reject">No longer supported</button></div>';d.querySelector('.show-diff').addEventListener('click',async()=>{const b=d.querySelector('.show-diff'),pre=d.querySelector('.version-diff');b.disabled=true;b.textContent='Loading comparison…';try{const comparison=await api(endpoint('/github/relationship-change-comparisons',{event_id:String(e.event_id)}));pre.hidden=false;pre.textContent=comparison.diff_lines.join('\n')+(comparison.diff_truncated?'\n… diff truncated at '+comparison.diff_line_limit+' lines.':'');d.querySelector('.diff-summary').textContent='Version '+comparison.previous_version_id+' → '+comparison.current_version_id+' · '+comparison.added_lines+' added · '+comparison.removed_lines+' removed'+(comparison.stored_hashes_match_versions?' · stored hashes verified':' · WARNING: stored hash mismatch');if(comparison.other_endpoint_current_version){d.querySelector('.other-context').hidden=false;d.querySelector('.other-version').textContent='Current version '+comparison.other_endpoint_current_version.version_id+' · '+comparison.other_endpoint_current_version.title+'\nSHA-256 '+comparison.other_endpoint_current_version.content_hash+'\n\n'+comparison.other_endpoint_current_version.body_excerpt}b.textContent='Refresh version comparison'}catch(err){d.querySelector('.diff-summary').textContent=err.message;b.textContent='Retry comparison'}finally{b.disabled=false}});for(const button of d.querySelectorAll('button[data-decision]'))button.addEventListener('click',async()=>{const rationale=d.querySelector('.reval-rationale').value.trim();if(!rationale){alert('Add a rationale before recording the revalidation.');return}const decision=button.dataset.decision;try{const payload={event_id:e.event_id,decision,reviewer:$('reviewer').value.trim()||'unspecified',rationale,evidence_snapshot:[{previous_version_id:e.previous_version_id,current_version_id:e.current_version_id,previous_content_hash:e.previous_content_hash,current_content_hash:e.current_content_hash}],provenance:{method:'github_relationship_explorer_revalidation'}};if(decision==='revised')payload.predicate=d.querySelector('.reval-predicate').value.trim();const result=await api('/github/relationship-revalidations',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});status.textContent='Recorded revalidation #'+result.revalidation_id+' ('+decision+').';await refreshReviewAndGraph()}catch(err){status.textContent=err.message}});root.append(d)}}
function renderReviews(items){const root=$('reviews');root.replaceChildren();if(!items.length){root.textContent='No review decisions recorded yet.';return}for(const r of items){const d=document.createElement('article');d.className='item';d.innerHTML='<h3>'+esc(r.decision)+' · '+esc(r.predicate)+'</h3><p class="muted small">Review #'+esc(r.review_id)+' · '+esc(r.reviewer)+' · '+esc(r.created_at)+'</p><p>'+esc(r.rationale)+'</p><p class="small"><a target="_blank" rel="noopener noreferrer" href="'+esc(r.from_url)+'">Source '+esc(r.from_source_id)+'</a> → <a target="_blank" rel="noopener noreferrer" href="'+esc(r.to_url)+'">Source '+esc(r.to_source_id)+'</a></p>';root.append(d)}}
function renderGraph(g){const root=$('graph');root.replaceChildren();const summary=document.createElement('p');summary.className='muted small';summary.textContent=g.node_count+' nodes · '+g.edge_count+' supported edges'+(g.nodes_truncated?' · node list may be truncated':'');root.append(summary);if(!g.edges.length){root.append(document.createTextNode('No supported edges yet. Accept a candidate to create one.'));return}for(const e of g.edges){const d=document.createElement('article');d.className='item';const from=g.nodes.find(n=>n.id===e.source),to=g.nodes.find(n=>n.id===e.target);d.innerHTML='<h3>'+esc(e.predicate)+'</h3><p><a href="'+esc(from?.url||'#')+'" target="_blank" rel="noopener noreferrer">'+esc(from?.repository||'source')+' / '+esc(from?.path||e.source)+'</a></p><p>↓</p><p><a href="'+esc(to?.url||'#')+'" target="_blank" rel="noopener noreferrer">'+esc(to?.repository||'target')+' / '+esc(to?.path||e.target)+'</a></p><p class="muted small">Edge #'+esc(e.id)+' · '+esc(e.status)+' · '+esc(e.created_at)+'</p><details><summary>Provenance</summary><pre>'+esc(JSON.stringify(e.provenance,null,2))+'</pre></details>';root.append(d)}}
$('load').addEventListener('click',loadAll);$('refresh').addEventListener('click',refreshReviewAndGraph);
</script></body></html>""")

