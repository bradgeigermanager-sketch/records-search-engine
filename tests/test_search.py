import hashlib
import sqlite3
import pytest
from fastapi.testclient import TestClient
import app.main as search_app

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(search_app, "DB_PATH", str(tmp_path / "test.db"))
    search_app.init_db()
    with TestClient(search_app.app) as test_client:
        yield test_client

def test_health(client):
    response = client.get('/health')
    assert response.status_code == 200
    assert response.json()['status'] == 'ok'

def test_search_returns_indexed_source(client):
    body = 'The archive preserves historical process records and source provenance.'
    saved = search_app.save_document('https://example.org/archive', 'Historical Archive', body, 200, 'text/html')
    response = client.get('/search', params={'q': 'historical archive'})
    assert response.status_code == 200
    results = response.json()
    assert len(results) == 1
    assert results[0]['title'] == 'Historical Archive'
    assert results[0]['url'] == 'https://example.org/archive'
    assert results[0]['content_hash'] == hashlib.sha256(body.encode()).hexdigest()
    assert results[0]['source_id'] == saved['source_id']
    assert results[0]['version_number'] == 1

def test_search_no_match(client):
    search_app.save_document('https://example.org/a', 'A record', 'Only a sample document.', 200, 'text/html')
    assert client.get('/search', params={'q': 'unrelated term'}).json() == []

def test_changed_content_creates_immutable_version_and_keeps_history(client):
    first = search_app.save_document('https://example.org/a', 'Old title', 'old words and an original snapshot', 200, 'text/html')
    second = search_app.save_document('https://example.org/a', 'New title', 'new words and a changed snapshot', 200, 'text/html')
    assert first['source_id'] == second['source_id']
    assert first['version_id'] != second['version_id']
    assert first['version_number'] == 1 and second['version_number'] == 2
    assert client.get('/search', params={'q': 'old words'}).json() == []
    current = client.get('/search', params={'q': 'new words'}).json()
    assert len(current) == 1 and current[0]['is_current'] is True
    history = client.get('/search', params={'q': 'old words', 'include_history': 'true'}).json()
    assert len(history) == 1 and history[0]['is_current'] is False
    assert history[0]['version_number'] == 1
    versions = client.get(f"/sources/{first['source_id']}/versions").json()
    assert [v['version_number'] for v in versions] == [2, 1]
    assert versions[1]['title'] == 'Old title'

def test_unchanged_content_does_not_duplicate_version_but_logs_retrieval(client):
    first = search_app.save_document('https://example.org/a', 'Same title', 'same body', 200, 'text/html')
    second = search_app.save_document('https://example.org/a', 'Same title', 'same body', 200, 'text/html')
    assert second['outcome'] == 'unchanged'
    assert first['version_id'] == second['version_id']
    assert client.get(f"/sources/{first['source_id']}/versions").json().__len__() == 1
    events = client.get(f"/sources/{first['source_id']}/events").json()
    assert len(events) == 2
    assert {event['outcome'] for event in events} == {'created', 'unchanged'}

def test_source_registry_contains_provenance_and_status(client):
    saved = search_app.save_document('https://example.org/a', 'A title', 'a body', 200, 'text/html',
        provenance={'method': 'test', 'archive': 'Example Archive'})
    source = client.get(f"/sources/{saved['source_id']}")
    assert source.status_code == 200
    assert source.json()['provenance']['archive'] == 'Example Archive'
    version = source.json()['versions'][0]
    assert version['http_status'] == 200
    assert version['content_type'] == 'text/html'
    assert version['extraction_status'] == 'success'
    assert version['content_hash'] == hashlib.sha256(b'a body').hexdigest()

def test_unknown_source_returns_404(client):
    assert client.get('/sources/999').status_code == 404
    assert client.get('/sources/999/versions').status_code == 404

def test_migration_preserves_legacy_documents(tmp_path, monkeypatch):
    db = tmp_path / 'legacy.db'
    conn = sqlite3.connect(db)
    conn.execute('''CREATE TABLE documents (id INTEGER PRIMARY KEY, canonical_url TEXT NOT NULL UNIQUE, title TEXT NOT NULL, body TEXT NOT NULL, content_hash TEXT NOT NULL, retrieved_at TEXT NOT NULL, http_status INTEGER NOT NULL, content_type TEXT NOT NULL)''')
    body = 'legacy historical content'
    conn.execute('INSERT INTO documents VALUES(1,?,?,?,?,?,?,?)', ('https://example.org/legacy', 'Legacy', body, hashlib.sha256(body.encode()).hexdigest(), '2026-01-01T00:00:00+00:00', 200, 'text/html'))
    conn.commit(); conn.close()
    monkeypatch.setattr(search_app, 'DB_PATH', str(db))
    search_app.init_db()
    with TestClient(search_app.app) as c:
        results = c.get('/search', params={'q': 'legacy historical'}).json()
        assert len(results) == 1
        assert results[0]['title'] == 'Legacy'
        assert results[0]['version_number'] == 1

def test_canonicalize_url_removes_fragment():
    assert search_app.canonicalize_url('https://Example.org/page#section') == 'https://example.org/page'

def test_reject_non_http_scheme():
    with pytest.raises(ValueError):
        search_app.canonicalize_url('file:///etc/passwd')

def test_reject_localhost():
    with pytest.raises(ValueError):
        search_app.validate_public_host('http://localhost/admin')

def test_document_versions_are_immutable(client):
    saved = search_app.save_document('https://example.org/immutable', 'Immutable', 'original content', 200, 'text/html')
    with search_app.connection() as conn:
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            conn.execute('UPDATE document_versions SET title=? WHERE id=?', ('Changed directly', saved['version_id']))
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            conn.execute('DELETE FROM document_versions WHERE id=?', (saved['version_id'],))

def test_schema_migration_is_idempotent(client):
    search_app.init_db()
    search_app.init_db()
    with search_app.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM schema_migrations WHERE version=1').fetchone()[0] == 1

def test_entities_relationships_and_metadata_are_provenance_aware(client):
    first = client.post('/entities', json={
        'canonical_name': 'Example Archive', 'entity_type': 'organization',
        'aliases': ['Archive Example'], 'provenance': {'source': 'test'}
    })
    second = client.post('/entities', json={'canonical_name': 'Example Record', 'entity_type': 'document'})
    assert first.status_code == 200 and second.status_code == 200
    a, b = first.json(), second.json()
    rel = client.post('/relationships', json={
        'subject_entity_id': a['entity_id'], 'predicate': 'maintains',
        'object_entity_id': b['entity_id'], 'confidence': 0.9,
        'provenance': {'method': 'manual_assertion'}
    })
    assert rel.status_code == 200
    assert client.get(f"/entities/{a['entity_id']}").json()['relationships'][0]['object_entity_name'] == 'Example Record'

    saved = search_app.save_document('https://example.org/metadata', 'Metadata source', 'metadata body', 200, 'text/html')
    source_id = saved['source_id']
    m1 = client.post(f'/sources/{source_id}/metadata', json={'key': 'language', 'value': 'en', 'provenance': {'method': 'header'}})
    m2 = client.post(f'/sources/{source_id}/metadata', json={'key': 'language', 'value': 'fr', 'provenance': {'method': 'review'}})
    assert m1.status_code == 200 and m2.status_code == 200
    assert m2.json()['supersedes_id'] == m1.json()['metadata_id']
    current = client.get(f'/sources/{source_id}/metadata').json()
    history = client.get(f'/sources/{source_id}/metadata?include_history=true').json()
    assert len(current) == 1 and current[0]['value'] == 'fr' and current[0]['is_current']
    assert len(history) == 2 and sum(x['is_current'] for x in history) == 1

def test_relationship_requires_exactly_one_object(client):
    entity = client.post('/entities', json={'canonical_name': 'Only entity', 'entity_type': 'thing'}).json()
    response = client.post('/relationships', json={
        'subject_entity_id': entity['entity_id'], 'predicate': 'related_to'
    })
    assert response.status_code == 422



def test_search_and_documents_filter_by_current_metadata(client):
    first = search_app.save_document('https://example.org/english', 'Archive language guide', 'A guide about archive language metadata.', 200, 'text/html')
    second = search_app.save_document('https://example.org/french', 'Archive language notes', 'Notes about archive language metadata.', 200, 'text/html')
    client.post(f"/sources/{first['source_id']}/metadata", json={'key': 'language', 'value': 'en'})
    client.post(f"/sources/{second['source_id']}/metadata", json={'key': 'language', 'value': 'fr'})
    client.post(f"/sources/{second['source_id']}/metadata", json={'key': 'language', 'value': 'en'})
    client.post(f"/sources/{second['source_id']}/metadata", json={'key': 'reviewed', 'value': 'yes'})

    by_key = client.get('/search', params={'q': 'archive language', 'metadata_key': 'language'})
    assert by_key.status_code == 200 and len(by_key.json()) == 2
    by_value = client.get('/search', params={'q': 'archive language', 'metadata_key': 'language', 'metadata_value': 'en'})
    assert by_value.status_code == 200
    assert {r['source_id'] for r in by_value.json()} == {first['source_id'], second['source_id']}
    by_other_value = client.get('/search', params={'q': 'archive language', 'metadata_key': 'language', 'metadata_value': 'fr'})
    assert by_other_value.json() == []
    docs = client.get('/documents', params={'metadata_key': 'reviewed', 'metadata_value': 'yes'})
    assert docs.status_code == 200 and [d['source_id'] for d in docs.json()] == [second['source_id']]

def test_metadata_value_filter_requires_key(client):
    response = client.get('/search', params={'q': 'anything', 'metadata_value': 'en'})
    assert response.status_code == 422
