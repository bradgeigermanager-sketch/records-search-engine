"""Records Search Engine — source registry and immutable content versions."""
from __future__ import annotations
import hashlib, ipaddress, json, os, socket, sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.parse import urlparse, urldefrag, urlunparse
import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, HttpUrl

APP_NAME = "Records Search Engine"
DB_PATH = os.getenv("SEARCH_DB_PATH", "search.db")
MAX_BYTES = 2_000_000
TIMEOUT_SECONDS = 12
USER_AGENT = "RecordsSearchEngine/0.6 (+responsible research; contact site operator before bulk use)"
app = FastAPI(title=APP_NAME, version="0.6.0", description="Provenance-preserving web-record search prototype")

class IngestRequest(BaseModel):
    url: HttpUrl
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
        conn.execute("UPDATE sources SET last_seen_at=?,provenance_json=? WHERE id=?",(now,pjson,sid))
        conn.execute("INSERT INTO ingestion_events(source_id,occurred_at,outcome,content_hash,version_id,http_status,content_type,extraction_status,details_json) VALUES(?,?,?,?,?,?,?,?,?)",(sid,now,outcome,digest,vid,status,content_type,extraction_status,pjson))
        return {"source_id":sid,"document_id":vid,"version_id":vid,"version_number":vnum,"title":title,"url":url,"retrieved_at":now,"content_hash":digest,"outcome":outcome,"is_current":True,"extraction_status":extraction_status}

@app.get("/health")
def health(): return {"status":"ok","service":APP_NAME,"version":app.version}

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
    return HTMLResponse("""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>STEM Wiki Page</title><style>body{font:16px system-ui;max-width:900px;margin:2rem auto;padding:0 1rem;color:#202124}.muted{color:#666}pre{white-space:pre-wrap;overflow-wrap:anywhere}li{margin:.5rem 0}</style></head><body><p><a href="/wiki">← STEM Wiki</a></p><p id="status" class="muted">Loading page…</p><article id="page"></article><script>const slug=decodeURIComponent(location.pathname.split('/').pop()),status=document.querySelector('#status'),root=document.querySelector('#page');fetch('/wiki/pages/'+encodeURIComponent(slug)).then(async r=>{const data=await r.json();if(!r.ok)throw new Error(data.detail||'Page not found');document.title=data.title+' · STEM Wiki';status.textContent=data.discipline+' · '+data.language+' · revision '+data.revision_number;const h=document.createElement('h1');h.textContent=data.title;const summary=document.createElement('p');summary.textContent=data.summary;const body=document.createElement('pre');body.textContent=data.body;const tags=document.createElement('p');tags.className='muted';tags.textContent='Tags: '+data.tags.join(', ');const history=document.createElement('a');history.href='/wiki/pages/'+encodeURIComponent(slug)+'?include_history=true';history.textContent='View revision history (JSON)';root.append(h,summary,body,tags);const refs=document.createElement('section'),rh=document.createElement('h2'),list=document.createElement('ul');rh.textContent='References';for(const ref of data.references){const li=document.createElement('li');if(ref.url&&/^https?:\\/\\//i.test(ref.url)){const a=document.createElement('a');a.href=ref.url;a.textContent=ref.title||ref.url;a.rel='noopener noreferrer';a.target='_blank';li.append(a)}else li.textContent=ref.title||JSON.stringify(ref);list.append(li)}refs.append(rh,list);root.append(refs,history)}).catch(e=>{status.textContent=e.message});</script></body></html>""")

@app.get("/wiki", response_class=HTMLResponse)
def wiki_home():
    return HTMLResponse("""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>STEM Wiki</title><style>body{font:16px system-ui;max-width:1000px;margin:2rem auto;padding:0 1rem;color:#202124}input,textarea,select{box-sizing:border-box;padding:.65rem;width:100%;margin:.25rem 0 1rem}button{padding:.7rem 1rem;cursor:pointer}.page{border-top:1px solid #ddd;padding:1rem 0}.muted{color:#666}pre{white-space:pre-wrap;overflow-wrap:anywhere}.layout{display:grid;grid-template-columns:1fr 1fr;gap:2rem}@media(max-width:700px){.layout{grid-template-columns:1fr}}</style></head><body><p><a href="/">← Records Search Engine</a></p><h1>STEM Wiki</h1><p class="muted">Versioned STEM knowledge pages with references, tags, and provenance. New revisions are preserved rather than overwritten.</p><form id="search"><label for="q">Search pages</label><input id="q" placeholder="e.g. force acceleration" required><label for="discipline">Discipline (optional)</label><input id="discipline" placeholder="physics, mathematics, engineering…"><button>Search wiki</button></form><p id="status" class="muted"></p><section id="results"></section><hr><h2>Create a page</h2><form id="create"><label>URL slug</label><input id="slug" placeholder="newton-second-law" required><label>Title</label><input id="title" required><label>Discipline</label><input id="kind" placeholder="physics" required><label>Summary</label><input id="summary"><label>Tags (comma separated)</label><input id="tags" placeholder="mechanics, classical physics"><label>Body / explanation</label><textarea id="body" rows="7" required></textarea><label>Reference URLs (one per line)</label><textarea id="refs" rows="3" placeholder="https://example.org/reference"></textarea><label>Author / contributor</label><input id="author"><button>Create wiki page</button></form><script>const status=document.querySelector('#status'),results=document.querySelector('#results');function renderPages(items){results.replaceChildren();for(const x of items){const article=document.createElement('article');article.className='page';const h=document.createElement('h2'),a=document.createElement('a');a.href='/wiki/view/'+encodeURIComponent(x.slug);a.textContent=x.title;h.append(a);const meta=document.createElement('p');meta.className='muted';meta.textContent=x.discipline+' · '+x.language+' · revision '+x.revision_number+' · '+x.tags.join(', ');const p=document.createElement('p');p.textContent=x.summary||x.body.slice(0,280);article.append(h,meta,p);results.append(article)}}document.querySelector('#search').addEventListener('submit',async e=>{e.preventDefault();status.textContent='Searching…';const params=new URLSearchParams({q:document.querySelector('#q').value});const d=document.querySelector('#discipline').value.trim();if(d)params.set('discipline',d);const r=await fetch('/wiki/search?'+params);const data=await r.json();status.textContent=data.length+' result(s)';renderPages(data)});document.querySelector('#create').addEventListener('submit',async e=>{e.preventDefault();status.textContent='Creating page…';const refs=document.querySelector('#refs').value.split(/\\r?\\n/).map(x=>x.trim()).filter(Boolean).map(url=>({url}));const payload={slug:document.querySelector('#slug').value,title:document.querySelector('#title').value,discipline:document.querySelector('#kind').value,summary:document.querySelector('#summary').value,body:document.querySelector('#body').value,tags:document.querySelector('#tags').value.split(',').map(x=>x.trim()).filter(Boolean),references:refs,author:document.querySelector('#author').value};const r=await fetch('/wiki/pages',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});const data=await r.json();if(!r.ok){status.textContent=data.detail||'Could not create page';return}status.textContent='Created '+data.title+' (revision '+data.revision_number+')';renderPages([data])});</script></body></html>""")

@app.get("/",response_class=HTMLResponse)
def home():
    return HTMLResponse("""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Records Search Engine</title><style>body{font:16px system-ui;max-width:900px;margin:3rem auto;padding:0 1rem;color:#202124}input{padding:.7rem;width:min(65%,520px)}button{padding:.75rem;cursor:pointer}.muted{color:#666}.result{border-top:1px solid #ddd;padding:1rem 0}</style></head><body><h1>Records Search Engine</h1><p><a href="/wiki">Open STEM Wiki</a></p><p class="muted">Search indexed web records or add a public HTML page. Source versions are preserved.</p><form id="search"><input id="q" placeholder="Search indexed records" required><label><input id="history" type="checkbox" style="width:auto"> Include historical versions</label><button>Search</button></form><h2>Add a source</h2><form id="ingest"><input id="url" type="url" placeholder="https://example.org/page" required><button>Index URL</button></form><p id="status" class="muted"></p><section id="results"></section><script>const status=document.querySelector('#status'),results=document.querySelector('#results');document.querySelector('#search').addEventListener('submit',async e=>{e.preventDefault();status.textContent='Searching…';results.replaceChildren();const q=document.querySelector('#q').value,history=document.querySelector('#history').checked,r=await fetch('/search?q='+encodeURIComponent(q)+'&include_history='+history),data=await r.json();status.textContent=data.length+' result(s)';for(const x of data){const el=document.createElement('article');el.className='result';const h=document.createElement('h3'),a=document.createElement('a');a.href=x.url;a.target='_blank';a.rel='noopener noreferrer';a.textContent=x.title;h.append(a);const p=document.createElement('p');p.textContent=x.excerpt.replace(/<\/?mark>/g,'');const s=document.createElement('small');s.textContent='Source #'+x.source_id+' · Version '+x.version_number+(x.is_current?' (current)':' (historical)');el.append(h,p,s);results.append(el)}});document.querySelector('#ingest').addEventListener('submit',async e=>{e.preventDefault();status.textContent='Retrieving and indexing…';const r=await fetch('/ingest',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:document.querySelector('#url').value})}),data=await r.json();status.textContent=r.ok?'Indexed: '+data.title+' ('+data.outcome+')':(data.detail||'Ingestion failed')});</script></body></html>""")
