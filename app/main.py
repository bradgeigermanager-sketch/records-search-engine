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
USER_AGENT = "RecordsSearchEngine/0.2 (+responsible research; contact site operator before bulk use)"
app = FastAPI(title=APP_NAME, version="0.2.0", description="Provenance-preserving web-record search prototype")

class IngestRequest(BaseModel):
    url: HttpUrl
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

@app.get("/search",response_model=list[SearchResult])
def search(q:str=Query(min_length=1,max_length=300),limit:int=Query(default=10,ge=1,le=50),include_history:bool=False):
    terms=[t.replace('"',' ').strip() for t in q.split() if t.strip()]
    if not terms: return []
    match=' AND '.join('"'+t.replace('"','')+'"' for t in terms)
    filter_sql='' if include_history else 'AND s.current_version_id=v.id'
    with connection() as conn:
        rows=conn.execute(f"""SELECT v.id,v.source_id,v.version_number,v.title,s.canonical_url,v.body,v.retrieved_at,v.content_hash,s.current_version_id,snippet(versions_fts,1,'<mark>','</mark>',' … ',24) excerpt FROM versions_fts JOIN document_versions v ON v.id=versions_fts.rowid JOIN sources s ON s.id=v.source_id WHERE versions_fts MATCH ? {filter_sql} ORDER BY bm25(versions_fts),v.retrieved_at DESC LIMIT ?""",(match,limit)).fetchall()
    return [SearchResult(document_id=r["id"],version_id=r["id"],source_id=r["source_id"],version_number=r["version_number"],title=r["title"],url=r["canonical_url"],excerpt=r["excerpt"] or r["body"][:240],retrieved_at=r["retrieved_at"],content_hash=r["content_hash"],is_current=r["current_version_id"]==r["id"]) for r in rows]

@app.get("/documents")
def documents(limit:int=Query(default=20,ge=1,le=100),include_history:bool=False):
    filt='' if include_history else 'WHERE s.current_version_id=v.id'
    with connection() as conn:
        rows=conn.execute(f"SELECT v.id version_id,s.id source_id,v.version_number,s.canonical_url url,v.title,v.retrieved_at,v.content_hash,v.extraction_status,v.http_status,v.content_type,(s.current_version_id=v.id) is_current FROM document_versions v JOIN sources s ON s.id=v.source_id {filt} ORDER BY v.retrieved_at DESC LIMIT ?",(limit,)).fetchall()
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

@app.get("/",response_class=HTMLResponse)
def home():
    return HTMLResponse("""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Records Search Engine</title><style>body{font:16px system-ui;max-width:900px;margin:3rem auto;padding:0 1rem;color:#202124}input{padding:.7rem;width:min(65%,520px)}button{padding:.75rem;cursor:pointer}.muted{color:#666}.result{border-top:1px solid #ddd;padding:1rem 0}</style></head><body><h1>Records Search Engine</h1><p class="muted">Search indexed web records or add a public HTML page. Source versions are preserved.</p><form id="search"><input id="q" placeholder="Search indexed records" required><label><input id="history" type="checkbox" style="width:auto"> Include historical versions</label><button>Search</button></form><h2>Add a source</h2><form id="ingest"><input id="url" type="url" placeholder="https://example.org/page" required><button>Index URL</button></form><p id="status" class="muted"></p><section id="results"></section><script>const status=document.querySelector('#status'),results=document.querySelector('#results');document.querySelector('#search').addEventListener('submit',async e=>{e.preventDefault();status.textContent='Searching…';results.replaceChildren();const q=document.querySelector('#q').value,history=document.querySelector('#history').checked,r=await fetch('/search?q='+encodeURIComponent(q)+'&include_history='+history),data=await r.json();status.textContent=data.length+' result(s)';for(const x of data){const el=document.createElement('article');el.className='result';const h=document.createElement('h3'),a=document.createElement('a');a.href=x.url;a.target='_blank';a.rel='noopener noreferrer';a.textContent=x.title;h.append(a);const p=document.createElement('p');p.textContent=x.excerpt.replace(/<\/?mark>/g,'');const s=document.createElement('small');s.textContent='Source #'+x.source_id+' · Version '+x.version_number+(x.is_current?' (current)':' (historical)');el.append(h,p,s);results.append(el)}});document.querySelector('#ingest').addEventListener('submit',async e=>{e.preventDefault();status.textContent='Retrieving and indexing…';const r=await fetch('/ingest',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:document.querySelector('#url').value})}),data=await r.json();status.textContent=r.ok?'Indexed: '+data.title+' ('+data.outcome+')':(data.detail||'Ingestion failed')});</script></body></html>""")
