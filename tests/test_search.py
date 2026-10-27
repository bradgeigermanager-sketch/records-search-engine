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


def test_stem_wiki_create_search_filter_and_revision_history(client):
    created = client.post('/wiki/pages', json={
        'slug': 'newton-second-law', 'title': "Newton's Second Law",
        'summary': 'Relationship between force, mass, and acceleration.',
        'body': 'The net force on an object equals its mass multiplied by its acceleration. F = m a.',
        'discipline': 'physics', 'language': 'en',
        'tags': ['mechanics', 'classical physics'],
        'references': [{'title': 'Example reference', 'url': 'https://example.org/physics'}],
        'author': 'test', 'provenance': {'review_status': 'unreviewed'}
    })
    assert created.status_code == 200
    assert created.json()['revision_number'] == 1
    found = client.get('/wiki/search', params={'q': 'force acceleration'})
    assert found.status_code == 200 and found.json()[0]['slug'] == 'newton-second-law'
    filtered = client.get('/wiki/pages', params={'discipline': 'physics', 'tag': 'mechanics'})
    assert filtered.status_code == 200 and filtered.json()[0]['title'] == "Newton's Second Law"

    revised = client.post('/wiki/pages/newton-second-law/revisions', json={
        'title': "Newton's Second Law", 'summary': 'Updated explanation.',
        'body': 'Net force equals mass times acceleration. F = m a.',
        'discipline': 'physics', 'language': 'en', 'tags': ['mechanics'],
        'change_note': 'Clarified wording', 'provenance': {'review_status': 'reviewed'}
    })
    assert revised.status_code == 200 and revised.json()['revision_number'] == 2
    assert client.get('/wiki/pages/newton-second-law').json()['revision_number'] == 2
    history = client.get('/wiki/pages/newton-second-law', params={'include_history': 'true'}).json()
    assert [r['revision_number'] for r in history['revisions']] == [2, 1]
    assert client.get('/wiki/search', params={'q': 'relationship force'}).json() == []

def test_stem_wiki_duplicate_slug_and_missing_page(client):
    payload = {'slug': 'unit-test-page', 'title': 'Unit Test', 'body': 'Sample body', 'discipline': 'mathematics'}
    assert client.post('/wiki/pages', json=payload).status_code == 200
    assert client.post('/wiki/pages', json=payload).status_code == 409
    assert client.get('/wiki/pages/does-not-exist').status_code == 404
    assert client.post('/wiki/pages/does-not-exist/revisions', json={
        'title': 'Missing', 'body': 'Missing page body', 'discipline': 'mathematics'
    }).status_code == 404

def test_wiki_revisions_are_immutable(client):
    client.post('/wiki/pages', json={'slug': 'immutable-wiki', 'title': 'Immutable', 'body': 'Original body', 'discipline': 'engineering'})
    with search_app.connection() as conn:
        revision_id = conn.execute('SELECT id FROM wiki_page_revisions').fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            conn.execute('UPDATE wiki_page_revisions SET title=? WHERE id=?', ('Edited in place', revision_id))
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            conn.execute('DELETE FROM wiki_page_revisions WHERE id=?', (revision_id,))


def test_stem_wiki_browser_pages_and_migration(client):
    assert client.get('/wiki').status_code == 200
    assert 'Create a page' in client.get('/wiki').text
    assert client.get('/wiki/view/missing-page').status_code == 200
    client.post('/wiki/pages', json={
        'slug': 'basic-algebra', 'title': 'Basic Algebra',
        'body': 'A branch of mathematics using symbols and equations.',
        'discipline': 'mathematics'
    })
    view = client.get('/wiki/view/basic-algebra')
    assert view.status_code == 200 and 'STEM Wiki Page' in view.text
    assert 'Structured STEM artifacts' in view.text and 'Data (JSON)' in view.text
    search_app.init_db()
    with search_app.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM schema_migrations WHERE version=3').fetchone()[0] == 1


def test_stem_artifact_equation_constant_algorithm_and_experiment(client):
    page = client.post('/wiki/pages', json={
        'slug': 'stem-artifact-host', 'title': 'STEM Artifacts',
        'body': 'A page hosting structured STEM records.', 'discipline': 'engineering'
    })
    assert page.status_code == 200

    equation = client.post('/wiki/pages/stem-artifact-host/artifacts', json={
        'artifact_type': 'equation', 'name': 'Newton second law',
        'data': {'expression': 'F = m * a', 'variables': [
            {'symbol': 'F', 'meaning': 'net force', 'unit': 'N'},
            {'symbol': 'm', 'meaning': 'mass', 'unit': 'kg'},
            {'symbol': 'a', 'meaning': 'acceleration', 'unit': 'm/s^2'}
        ]},
        'provenance': {'status': 'review_required'}
    })
    assert equation.status_code == 200
    assert equation.json()['data']['expression'] == 'F = m * a'

    constant = client.post('/wiki/pages/stem-artifact-host/artifacts', json={
        'artifact_type': 'constant', 'name': 'Standard gravity (illustrative)',
        'data': {'value': 9.80665, 'unit': 'm/s^2', 'uncertainty': 'context-dependent'}
    })
    algorithm = client.post('/wiki/pages/stem-artifact-host/artifacts', json={
        'artifact_type': 'algorithm', 'name': 'Sum a list',
        'data': {'pseudocode': 'total <- sum(values)', 'inputs': ['values'], 'outputs': ['total']}
    })
    experiment = client.post('/wiki/pages/stem-artifact-host/artifacts', json={
        'artifact_type': 'experiment', 'name': 'Measure acceleration',
        'data': {'procedure': ['Record distance', 'Measure elapsed time'], 'independent_variables': ['distance'], 'dependent_variables': ['elapsed time']}
    })
    assert constant.status_code == algorithm.status_code == experiment.status_code == 200
    all_artifacts = client.get('/wiki/pages/stem-artifact-host/artifacts').json()
    assert {item['artifact_type'] for item in all_artifacts} == {'equation', 'constant', 'algorithm', 'experiment'}
    assert len(client.get('/wiki/pages/stem-artifact-host/artifacts', params={'artifact_type': 'equation'}).json()) == 1

def test_stem_artifact_validation_and_immutability(client):
    client.post('/wiki/pages', json={
        'slug': 'artifact-validation', 'title': 'Artifact validation',
        'body': 'Validation test page.', 'discipline': 'mathematics'
    })
    invalid = client.post('/wiki/pages/artifact-validation/artifacts', json={
        'artifact_type': 'equation', 'name': 'Incomplete equation', 'data': {'expression': 'x + y'}
    })
    assert invalid.status_code == 422
    unsupported = client.post('/wiki/pages/artifact-validation/artifacts', json={
        'artifact_type': 'poem', 'name': 'Unsupported', 'data': {}
    })
    assert unsupported.status_code == 422
    client.post('/wiki/pages/artifact-validation/artifacts', json={
        'artifact_type': 'constant', 'name': 'Example', 'data': {'value': 1, 'unit': 'm'}
    })
    with search_app.connection() as conn:
        artifact_id = conn.execute('SELECT id FROM stem_artifacts').fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            conn.execute('UPDATE stem_artifacts SET name=? WHERE id=?', ('Mutated', artifact_id))
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            conn.execute('DELETE FROM stem_artifacts WHERE id=?', (artifact_id,))


def test_stem_knowledge_edges_and_graph_traversal(client):
    page = client.post('/wiki/pages', json={
        'slug': 'graph-law', 'title': 'Newton Second Law', 'body': 'Force equals mass times acceleration.',
        'discipline': 'physics'
    }).json()
    artifact = client.post('/wiki/pages/graph-law/artifacts', json={
        'artifact_type': 'equation', 'name': 'Force equation',
        'data': {'expression': 'F = m * a', 'variables': [
            {'symbol': 'F', 'meaning': 'net force', 'unit': 'N'},
            {'symbol': 'm', 'meaning': 'mass', 'unit': 'kg'},
            {'symbol': 'a', 'meaning': 'acceleration', 'unit': 'm/s^2'}
        ]}
    }).json()
    edge = client.post('/wiki/edges', json={
        'subject_kind': 'page', 'subject_id': page['revision_id'], 'predicate': 'includes equation',
        'object_kind': 'artifact', 'object_id': artifact['artifact_id'],
        'evidence_page_revision_id': page['revision_id'], 'confidence': 0.9,
        'status': 'proposed', 'provenance': {'basis': 'editor assertion'}
    })
    assert edge.status_code == 200
    assert edge.json()['predicate'] == 'includes_equation'
    listed = client.get('/wiki/edges', params={'subject_kind': 'page', 'subject_id': page['revision_id']})
    assert listed.status_code == 200 and len(listed.json()) == 1
    graph = client.get(f"/wiki/graph/page/{page['revision_id']}")
    assert graph.status_code == 200
    assert {n['kind'] for n in graph.json()['nodes']} == {'page', 'artifact'}
    assert graph.json()['edges'][0]['status'] == 'proposed'

def test_stem_knowledge_edge_validation_and_immutability(client):
    page = client.post('/wiki/pages', json={
        'slug': 'graph-checks', 'title': 'Graph checks', 'body': 'Graph tests.', 'discipline': 'mathematics'
    }).json()
    bad = client.post('/wiki/edges', json={
        'subject_kind': 'person', 'subject_id': 1, 'predicate': 'uses',
        'object_kind': 'page', 'object_id': page['revision_id']
    })
    assert bad.status_code == 422
    missing = client.post('/wiki/edges', json={
        'subject_kind': 'page', 'subject_id': page['revision_id'], 'predicate': 'uses',
        'object_kind': 'artifact', 'object_id': 9999
    })
    assert missing.status_code == 404
    created = client.post('/wiki/edges', json={
        'subject_kind': 'page', 'subject_id': page['revision_id'], 'predicate': 'related to',
        'object_kind': 'page', 'object_id': page['revision_id']
    }).json()
    with search_app.connection() as conn:
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            conn.execute('UPDATE knowledge_edges SET status=? WHERE id=?', ('supported', created['edge_id']))
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            conn.execute('DELETE FROM knowledge_edges WHERE id=?', (created['edge_id'],))


def test_stem_artifact_validation_reports_and_history(client):
    page = client.post('/wiki/pages', json={
        'slug': 'validation-lab', 'title': 'Validation lab', 'body': 'Validation examples.',
        'discipline': 'physics'
    }).json()
    equation = client.post('/wiki/pages/validation-lab/artifacts', json={
        'artifact_type': 'equation', 'name': 'Illustrative equation',
        'data': {'expression': 'x = y', 'variables': [
            {'symbol': 'x', 'meaning': 'position', 'unit': 'm'},
            {'symbol': 'y', 'meaning': 'position estimate', 'unit': 'm'}
        ]}
    }).json()
    report = client.post(f"/wiki/artifacts/{equation['artifact_id']}/validate")
    assert report.status_code == 200
    assert report.json()['outcome'] == 'review'
    assert any('dimensional consistency was not evaluated' in w.lower() for w in report.json()['warnings'])
    again = client.post(f"/wiki/artifacts/{equation['artifact_id']}/validate")
    assert again.status_code == 200
    history = client.get(f"/wiki/artifacts/{equation['artifact_id']}/validation-reports")
    assert history.status_code == 200 and len(history.json()) == 2
    with search_app.connection() as conn:
        report_id = history.json()[0]['report_id']
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            conn.execute('UPDATE stem_validation_reports SET outcome=? WHERE id=?', ('pass', report_id))
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            conn.execute('DELETE FROM stem_validation_reports WHERE id=?', (report_id,))

def test_stem_validator_detects_incomplete_experiment_and_unit_mismatch(client):
    page = client.post('/wiki/pages', json={
        'slug': 'validation-experiment', 'title': 'Experiment validation', 'body': 'Checks.',
        'discipline': 'science'
    }).json()
    experiment = client.post('/wiki/pages/validation-experiment/artifacts', json={
        'artifact_type': 'experiment', 'name': 'Incomplete experiment',
        'data': {'procedure': [''], 'independent_variables': [], 'dependent_variables': []}
    }).json()
    report = client.post(f"/wiki/artifacts/{experiment['artifact_id']}/validate").json()
    assert report['outcome'] == 'fail'
    assert report['errors']

    equation = client.post('/wiki/pages/validation-experiment/artifacts', json={
        'artifact_type': 'equation', 'name': 'Declared unit comparison',
        'data': {'expression': 'x = t', 'variables': [{'symbol': 'x', 'meaning': 'distance', 'unit': 'm'}],
                 'lhs_unit': 'm', 'rhs_unit': 's'}
    }).json()
    unit_report = client.post(f"/wiki/artifacts/{equation['artifact_id']}/validate").json()
    assert unit_report['outcome'] == 'fail'
    assert any('dimensionally incompatible' in e for e in unit_report['errors'])


def test_versioned_validation_rules_and_compound_units(client):
    rules = client.get('/wiki/validation-rule-sets')
    assert rules.status_code == 200
    assert any(item['name'] == 'stem-core' and item['version'] == '0.2' for item in rules.json())

    page = client.post('/wiki/pages', json={
        'slug': 'compound-unit-check', 'title': 'Compound unit check',
        'body': 'Check supported compound units.', 'discipline': 'physics'
    }).json()
    good = client.post('/wiki/pages/compound-unit-check/artifacts', json={
        'artifact_type': 'equation', 'name': 'Force dimensions',
        'data': {'expression': 'F = m * a', 'variables': [
            {'symbol': 'F', 'meaning': 'force', 'unit': 'N'},
            {'symbol': 'm', 'meaning': 'mass', 'unit': 'kg'},
            {'symbol': 'a', 'meaning': 'acceleration', 'unit': 'm/s^2'}
        ], 'lhs_unit': 'N', 'rhs_unit': 'kg*m/s^2'}
    }).json()
    report = client.post(f"/wiki/artifacts/{good['artifact_id']}/validate")
    assert report.status_code == 200
    assert report.json()['validator_version'] == '0.2'
    assert report.json()['rule_set'] == {'name': 'stem-core', 'version': '0.2'}
    assert report.json()['outcome'] != 'fail'
    assert any('dimensionally compatible' in check for check in report.json()['checks'])

def test_validation_rule_sets_are_immutable(client):
    client.get('/wiki/validation-rule-sets')
    with search_app.connection() as conn:
        rule_id = conn.execute("SELECT id FROM stem_validation_rule_sets WHERE name='stem-core'").fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            conn.execute('UPDATE stem_validation_rule_sets SET version=? WHERE id=?', ('0.3', rule_id))
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            conn.execute('DELETE FROM stem_validation_rule_sets WHERE id=?', (rule_id,))


def test_github_repository_ingestion_indexes_text_files_and_preserves_versions(client, monkeypatch):
    def fake_get_json(url):
        if url.endswith('/repos/example-owner/example-repo'):
            return {'default_branch': 'main', 'private': False, 'html_url': 'https://github.com/example-owner/example-repo'}
        if '/git/trees/' in url:
            return {'truncated': False, 'tree': [
                {'type': 'blob', 'path': 'README.md', 'sha': 'blob-readme', 'size': 20},
                {'type': 'blob', 'path': 'docs/process.txt', 'sha': 'blob-process', 'size': 20},
                {'type': 'blob', 'path': 'image.png', 'sha': 'blob-image', 'size': 20},
                {'type': 'tree', 'path': 'docs'}
            ]}
        raise AssertionError('unexpected GitHub API URL: ' + url)

    contents = {'blob-readme': '# Repository overview\n', 'blob-process': 'A repeatable process record.\n'}
    monkeypatch.setattr(search_app, '_github_get_json', fake_get_json)
    monkeypatch.setattr(search_app, '_github_text_blob', lambda url: next((v for k, v in contents.items() if k in url), None))
    payload = {'owner': 'example-owner', 'repository': 'example-repo', 'max_files': 10}
    first = client.post('/github/ingest-repository', json=payload)
    assert first.status_code == 200
    assert first.json()['files_indexed'] == 2
    assert first.json()['ref'] == 'main'
    assert first.json()['skipped']['binary_or_unsupported_extension'] == 1
    second = client.post('/github/ingest-repository', json=payload)
    assert second.status_code == 200
    assert {item['outcome'] for item in second.json()['results']} == {'unchanged'}
    assert client.get('/search', params={'q': 'repeatable process'}).status_code == 200

def test_github_repository_ingestion_validates_request_and_tree_size(client, monkeypatch):
    bad = client.post('/github/ingest-repository', json={'owner': '../bad', 'repository': 'repo'})
    assert bad.status_code == 422
    assert client.post('/github/ingest-repository', json={'owner': 'owner', 'repository': 'repo', 'max_files': 101}).status_code == 422

    def fake_get_json(url):
        if url.endswith('/repos/example-owner/large-repo'):
            return {'default_branch': 'main', 'private': False, 'html_url': 'https://github.com/example-owner/large-repo'}
        return {'truncated': True, 'tree': []}
    monkeypatch.setattr(search_app, '_github_get_json', fake_get_json)
    response = client.post('/github/ingest-repository', json={'owner': 'example-owner', 'repository': 'large-repo'})
    assert response.status_code == 413


def test_github_owner_ingestion_scans_bounded_repository_list(client, monkeypatch):
    def fake_get_json(url):
        if url.endswith('/users/example-owner/repos?type=owner&per_page=100&sort=updated'):
            return [
                {'name': 'repo-one', 'private': False, 'fork': False, 'archived': False},
                {'name': 'repo-two', 'private': False, 'fork': False, 'archived': False},
                {'name': 'forked-repo', 'private': False, 'fork': True, 'archived': False}
            ]
        if url.endswith('/repos/example-owner/repo-one') or url.endswith('/repos/example-owner/repo-two'):
            name = 'repo-one' if 'repo-one' in url else 'repo-two'
            return {'default_branch': 'main', 'private': False, 'html_url': 'https://github.com/example-owner/' + name}
        if '/git/trees/' in url:
            name = 'repo-one' if 'repo-one' in url else 'repo-two'
            return {'truncated': False, 'tree': [{'type': 'blob', 'path': 'README.md', 'sha': 'blob-' + name, 'size': 40}]}
        raise AssertionError('unexpected GitHub API URL: ' + url)

    monkeypatch.setattr(search_app, '_github_get_json', fake_get_json)
    monkeypatch.setattr(search_app, '_github_text_blob', lambda url: 'Documentation from ' + ('repo-one' if 'blob-repo-one' in url else 'repo-two'))
    response = client.post('/github/ingest-owner', json={'owner': 'example-owner', 'max_repositories': 10, 'max_files_per_repository': 5})
    assert response.status_code == 200
    assert response.json()['repositories_considered'] == 2
    assert response.json()['repositories_indexed'] == 2
    assert {item['repository'] for item in response.json()['results']} == {'repo-one', 'repo-two'}

def test_github_owner_ingestion_bounds_request(client):
    assert client.post('/github/ingest-owner', json={'owner': '../bad'}).status_code == 422
    assert client.post('/github/ingest-owner', json={'owner': 'owner', 'max_repositories': 21}).status_code == 422


def test_github_owner_search_and_source_navigation(client):
    first = search_app.save_document(
        'https://github.com/example-owner/process-a/blob/main/README.md',
        'example-owner/process-a: README.md',
        'Canonical process library describes source provenance and repeatable records.',
        200, 'text/plain; charset=utf-8',
        {'method': 'github_repository_ingestion', 'owner': 'example-owner', 'repository': 'process-a',
         'ref': 'main', 'path': 'README.md', 'repository_url': 'https://github.com/example-owner/process-a'})
    second = search_app.save_document(
        'https://github.com/example-owner/process-b/blob/main/guide.md',
        'example-owner/process-b: guide.md',
        'Cross repository records connect shared entities and source provenance.',
        200, 'text/plain; charset=utf-8',
        {'method': 'github_repository_ingestion', 'owner': 'example-owner', 'repository': 'process-b',
         'ref': 'main', 'path': 'guide.md', 'repository_url': 'https://github.com/example-owner/process-b'})
    search = client.get('/github/search', params={'q': 'source provenance', 'owner': 'example-owner'})
    assert search.status_code == 200
    assert {item['repository'] for item in search.json()} == {'process-a', 'process-b'}
    assert all(item['source_navigation'].startswith('/sources/') for item in search.json())
    filtered = client.get('/github/search', params={'q': 'source provenance', 'owner': 'example-owner', 'repository': 'process-b'})
    assert len(filtered.json()) == 1
    assert filtered.json()[0]['source_id'] == second['source_id']
    repositories = client.get('/github/repositories', params={'owner': 'example-owner'}).json()
    assert {item['repository'] for item in repositories} == {'process-a', 'process-b'}
    files = client.get('/github/repositories/example-owner/process-a').json()
    assert len(files) == 1 and files[0]['path'] == 'README.md'
    assert files[0]['source_id'] == first['source_id']
    assert files[0]['history_navigation'] == f"/sources/{first['source_id']}/versions"


def test_cross_repository_relationships_are_explicit_and_immutable(client):
    first = search_app.save_document(
        'https://github.com/example-owner/process-a/blob/main/README.md', 'process-a README',
        'Defines the process interface.', 200, 'text/plain',
        {'method': 'github_repository_ingestion', 'owner': 'example-owner', 'repository': 'process-a', 'path': 'README.md'})
    second = search_app.save_document(
        'https://github.com/example-owner/process-b/blob/main/README.md', 'process-b README',
        'Implements the process interface.', 200, 'text/plain',
        {'method': 'github_repository_ingestion', 'owner': 'example-owner', 'repository': 'process-b', 'path': 'README.md'})
    created = client.post('/github/repository-relationships', json={
        'from_source_id': first['source_id'], 'to_source_id': second['source_id'],
        'predicate': 'implements', 'confidence': 0.85, 'status': 'proposed',
        'provenance': {'basis': 'manual review'}
    })
    assert created.status_code == 200
    relationship = created.json()
    assert relationship['predicate'] == 'implements'
    listed = client.get('/github/repository-relationships', params={'owner': 'example-owner'}).json()
    assert len(listed) == 1
    assert listed[0]['from_provenance']['repository'] == 'process-a'
    assert listed[0]['to_provenance']['repository'] == 'process-b'
    same_repo = client.post('/github/repository-relationships', json={
        'from_source_id': first['source_id'], 'to_source_id': first['source_id'],
        'predicate': 'duplicates'
    })
    assert same_repo.status_code == 422
    with search_app.connection() as conn:
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            conn.execute('UPDATE repository_relationships SET status=? WHERE id=?', ('supported', relationship['relationship_id']))
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            conn.execute('DELETE FROM repository_relationships WHERE id=?', (relationship['relationship_id'],))


def test_github_relationship_candidate_discovery_is_explainable_and_non_persistent(client):
    first = search_app.save_document(
        'https://github.com/example-owner/process-a/blob/main/README.md',
        'example-owner/process-a: README.md',
        'Implementation specification for tracking OPS-20261026 and process lineage.',
        200, 'text/plain; charset=utf-8',
        {'method': 'github_repository_ingestion', 'owner': 'example-owner', 'repository': 'process-a',
         'ref': 'main', 'path': 'README.md', 'repository_url': 'https://github.com/example-owner/process-a'})
    second = search_app.save_document(
        'https://github.com/example-owner/process-b/blob/main/guide.md',
        'example-owner/process-b: guide.md',
        'Validation guide references OPS-20261026 and confirms process lineage.',
        200, 'text/plain; charset=utf-8',
        {'method': 'github_repository_ingestion', 'owner': 'example-owner', 'repository': 'process-b',
         'ref': 'main', 'path': 'guide.md', 'repository_url': 'https://github.com/example-owner/process-b'})
    response = client.get('/github/relationship-candidates', params={'owner': 'example-owner'})
    assert response.status_code == 200
    payload = response.json()
    assert payload['sources_scanned'] == 2
    assert payload['scan_truncated'] is False
    assert payload['candidate_count_returned'] >= 1
    candidate = next(c for c in payload['candidates']
                     if {c['from_source_id'], c['to_source_id']} == {first['source_id'], second['source_id']})
    assert candidate['status'] == 'candidate_only'
    assert candidate['review_required'] is True
    assert any(e['type'] == 'shared_identifier' and 'ops-20261026' in e['values']
               for e in candidate['evidence'])
    assert client.get('/github/repository-relationships', params={'owner': 'example-owner'}).json() == []


def test_github_relationship_candidate_discovery_requires_owner_and_bounds(client):
    assert client.get('/github/relationship-candidates').status_code == 422
    assert client.get('/github/relationship-candidates', params={'owner': 'example-owner', 'max_sources': 501}).status_code == 422
    assert client.get('/github/relationship-candidates', params={'owner': '../bad'}).status_code == 422


def test_relationship_review_acceptance_is_immutable_and_visible_in_graph(client):
    first = search_app.save_document(
        'https://github.com/example-owner/review-a/blob/main/README.md', 'review-a README',
        'A source with a stable interface.', 200, 'text/plain',
        {'method': 'github_repository_ingestion', 'owner': 'example-owner', 'repository': 'review-a', 'path': 'README.md'})
    second = search_app.save_document(
        'https://github.com/example-owner/review-b/blob/main/README.md', 'review-b README',
        'A source implementing the stable interface.', 200, 'text/plain',
        {'method': 'github_repository_ingestion', 'owner': 'example-owner', 'repository': 'review-b', 'path': 'README.md'})
    review = client.post('/github/relationship-reviews', json={
        'from_source_id': first['source_id'], 'to_source_id': second['source_id'],
        'decision': 'accepted', 'predicate': 'implements', 'reviewer': 'test-reviewer',
        'rationale': 'The documented interface and implementation match.',
        'confidence': 0.9, 'evidence_snapshot': [{'type': 'manual_review', 'version_ids': [first['version_id'], second['version_id']]}],
        'provenance': {'method': 'test'}
    })
    assert review.status_code == 200
    payload = review.json()
    assert payload['decision'] == 'accepted'
    assert payload['resulting_relationship_id'] is not None
    reviews = client.get('/github/relationship-reviews', params={'owner': 'example-owner', 'decision': 'accepted'}).json()
    assert len(reviews) == 1
    assert reviews[0]['reviewer'] == 'test-reviewer'
    assert reviews[0]['evidence_snapshot'][0]['type'] == 'manual_review'
    graph = client.get('/github/relationship-graph', params={'owner': 'example-owner'}).json()
    assert graph['node_count'] == 2
    assert any(edge['predicate'] == 'implements' and edge['status'] == 'supported' for edge in graph['edges'])
    with search_app.connection() as conn:
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            conn.execute('UPDATE repository_relationship_reviews SET decision=? WHERE id=?', ('rejected', payload['review_id']))
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            conn.execute('DELETE FROM repository_relationship_reviews WHERE id=?', (payload['review_id'],))


def test_relationship_review_rejection_does_not_create_graph_edge(client):
    first = search_app.save_document(
        'https://github.com/example-owner/reject-a/blob/main/README.md', 'reject-a README',
        'Potential relation.', 200, 'text/plain',
        {'method': 'github_repository_ingestion', 'owner': 'example-owner', 'repository': 'reject-a', 'path': 'README.md'})
    second = search_app.save_document(
        'https://github.com/example-owner/reject-b/blob/main/README.md', 'reject-b README',
        'Potentially unrelated.', 200, 'text/plain',
        {'method': 'github_repository_ingestion', 'owner': 'example-owner', 'repository': 'reject-b', 'path': 'README.md'})
    response = client.post('/github/relationship-reviews', json={
        'from_source_id': first['source_id'], 'to_source_id': second['source_id'],
        'decision': 'rejected', 'reviewer': 'reviewer', 'rationale': 'Evidence insufficient.'
    })
    assert response.status_code == 200
    assert response.json()['resulting_relationship_id'] is None
    assert client.get('/github/repository-relationships', params={'owner': 'example-owner'}).json() == []
    assert len(client.get('/github/relationship-reviews', params={'owner': 'example-owner', 'decision': 'rejected'}).json()) == 1


def test_relationship_review_validation(client):
    assert client.post('/github/relationship-reviews', json={
        'from_source_id': 1, 'to_source_id': 2, 'decision': 'maybe'
    }).status_code == 422


def test_github_relationship_explorer_page_is_available(client):
    response = client.get('/github/explorer')
    assert response.status_code == 200
    assert 'GitHub Relationship Explorer' in response.text
    assert '/github/relationship-candidates' in response.text
    assert '/github/relationship-reviews' in response.text
    assert '/github/relationship-graph' in response.text


def test_source_change_creates_immutable_relationship_staleness_event(client):
    left_provenance = {'method': 'github_repository_ingestion', 'owner': 'example-owner', 'repository': 'change-a', 'path': 'README.md', 'git_blob_sha': 'old-a'}
    right_provenance = {'method': 'github_repository_ingestion', 'owner': 'example-owner', 'repository': 'change-b', 'path': 'README.md', 'git_blob_sha': 'stable-b'}
    left = search_app.save_document(
        'https://github.com/example-owner/change-a/blob/main/README.md', 'change-a README',
        'Original API contract.', 200, 'text/plain', left_provenance)
    right = search_app.save_document(
        'https://github.com/example-owner/change-b/blob/main/README.md', 'change-b README',
        'Implementation of the original API contract.', 200, 'text/plain', right_provenance)
    accepted = client.post('/github/relationship-reviews', json={
        'from_source_id': left['source_id'], 'to_source_id': right['source_id'],
        'decision': 'accepted', 'predicate': 'implements', 'reviewer': 'reviewer',
        'rationale': 'The current source versions align.',
        'evidence_snapshot': [{'version_ids': [left['version_id'], right['version_id']]}]
    })
    assert accepted.status_code == 200
    relationship_id = accepted.json()['resulting_relationship_id']
    changed = search_app.save_document(
        'https://github.com/example-owner/change-a/blob/main/README.md', 'change-a README',
        'Revised API contract with a changed method signature.', 200, 'text/plain',
        {**left_provenance, 'git_blob_sha': 'new-a'})
    assert changed['outcome'] == 'changed'
    response = client.get('/github/relationship-change-events', params={'owner': 'example-owner', 'relationship_id': relationship_id})
    assert response.status_code == 200
    events = response.json()
    assert len(events) == 1
    assert events[0]['source_id'] == left['source_id']
    assert events[0]['previous_version_id'] == left['version_id']
    assert events[0]['current_version_id'] == changed['version_id']
    assert events[0]['reviewed_version_id'] == left['version_id']
    assert events[0]['revalidation_recommended'] is True
    with search_app.connection() as conn:
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            conn.execute('UPDATE relationship_change_events SET reason=? WHERE id=?', ('silently cleared', events[0]['event_id']))
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            conn.execute('DELETE FROM relationship_change_events WHERE id=?', (events[0]['event_id'],))


def test_unchanged_source_does_not_create_relationship_staleness_event(client):
    provenance = {'method': 'github_repository_ingestion', 'owner': 'example-owner', 'repository': 'same-a', 'path': 'README.md'}
    first = search_app.save_document(
        'https://github.com/example-owner/same-a/blob/main/README.md', 'same-a README',
        'Unchanged body.', 200, 'text/plain', provenance)
    same = search_app.save_document(
        'https://github.com/example-owner/same-a/blob/main/README.md', 'same-a README',
        'Unchanged body.', 200, 'text/plain', provenance)
    assert same['outcome'] == 'unchanged'
    assert client.get('/github/relationship-change-events', params={'owner': 'example-owner'}).json() == []


def test_relationship_revalidation_reaffirms_current_versions_and_supersedes_old_edge(client):
    left_provenance = {'method': 'github_repository_ingestion', 'owner': 'example-owner', 'repository': 'reval-a', 'path': 'README.md', 'git_blob_sha': 'old-a'}
    right_provenance = {'method': 'github_repository_ingestion', 'owner': 'example-owner', 'repository': 'reval-b', 'path': 'README.md', 'git_blob_sha': 'stable-b'}
    left = search_app.save_document(
        'https://github.com/example-owner/reval-a/blob/main/README.md', 'reval-a README',
        'API contract v1.', 200, 'text/plain', left_provenance)
    right = search_app.save_document(
        'https://github.com/example-owner/reval-b/blob/main/README.md', 'reval-b README',
        'Implementation of API contract v1.', 200, 'text/plain', right_provenance)
    accepted = client.post('/github/relationship-reviews', json={
        'from_source_id': left['source_id'], 'to_source_id': right['source_id'],
        'decision': 'accepted', 'predicate': 'implements', 'reviewer': 'reviewer',
        'rationale': 'Initial version comparison.'
    })
    assert accepted.status_code == 200
    old_relationship_id = accepted.json()['resulting_relationship_id']
    changed = search_app.save_document(
        'https://github.com/example-owner/reval-a/blob/main/README.md', 'reval-a README',
        'API contract v2 with a clarified signature.', 200, 'text/plain',
        {**left_provenance, 'git_blob_sha': 'new-a'})
    assert changed['outcome'] == 'changed'
    events = client.get('/github/relationship-change-events', params={'owner': 'example-owner'}).json()
    event = next(e for e in events if e['relationship_id'] == old_relationship_id)
    response = client.post('/github/relationship-revalidations', json={
        'event_id': event['event_id'], 'decision': 'reaffirmed', 'reviewer': 'second-reviewer',
        'rationale': 'Compared the new contract and implementation; the relationship still holds.',
        'evidence_snapshot': [{'checked_version_ids': [changed['version_id'], right['version_id']]}]
    })
    assert response.status_code == 200
    payload = response.json()
    assert payload['replacement_relationship_id'] is not None
    history = client.get('/github/relationship-revalidations', params={'owner': 'example-owner'}).json()
    assert len(history) == 1
    assert history[0]['decision'] == 'reaffirmed'
    assert history[0]['replacement_relationship_id'] == payload['replacement_relationship_id']
    graph = client.get('/github/relationship-graph', params={'owner': 'example-owner'}).json()
    edge_ids = {edge['id'] for edge in graph['edges']}
    assert old_relationship_id not in edge_ids
    assert payload['replacement_relationship_id'] in edge_ids
    with search_app.connection() as conn:
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            conn.execute('UPDATE relationship_revalidations SET decision=? WHERE id=?', ('disputed', payload['revalidation_id']))
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            conn.execute('DELETE FROM relationship_revalidations WHERE id=?', (payload['revalidation_id'],))


def test_relationship_revalidation_can_mark_edge_no_longer_supported(client):
    left_provenance = {'method': 'github_repository_ingestion', 'owner': 'example-owner', 'repository': 'obsolete-a', 'path': 'README.md'}
    right_provenance = {'method': 'github_repository_ingestion', 'owner': 'example-owner', 'repository': 'obsolete-b', 'path': 'README.md'}
    left = search_app.save_document('https://github.com/example-owner/obsolete-a/blob/main/README.md', 'obsolete-a README', 'Old contract.', 200, 'text/plain', left_provenance)
    right = search_app.save_document('https://github.com/example-owner/obsolete-b/blob/main/README.md', 'obsolete-b README', 'Old implementation.', 200, 'text/plain', right_provenance)
    accepted = client.post('/github/relationship-reviews', json={
        'from_source_id': left['source_id'], 'to_source_id': right['source_id'],
        'decision': 'accepted', 'predicate': 'implements', 'rationale': 'Initially verified.'
    }).json()
    changed = search_app.save_document('https://github.com/example-owner/obsolete-a/blob/main/README.md', 'obsolete-a README', 'The contract was removed.', 200, 'text/plain', left_provenance)
    assert changed['outcome'] == 'changed'
    event = next(e for e in client.get('/github/relationship-change-events', params={'owner': 'example-owner'}).json() if e['relationship_id'] == accepted['resulting_relationship_id'])
    response = client.post('/github/relationship-revalidations', json={
        'event_id': event['event_id'], 'decision': 'no_longer_supported',
        'reviewer': 'reviewer', 'rationale': 'The current contract no longer supports this link.'
    })
    assert response.status_code == 200
    assert response.json()['replacement_relationship_id'] is None
    graph = client.get('/github/relationship-graph', params={'owner': 'example-owner'}).json()
    assert accepted['resulting_relationship_id'] not in {edge['id'] for edge in graph['edges']}


def test_relationship_revalidation_rejects_duplicate_decision_for_same_event(client):
    left_provenance = {'method': 'github_repository_ingestion', 'owner': 'example-owner', 'repository': 'duplicate-a', 'path': 'README.md'}
    right_provenance = {'method': 'github_repository_ingestion', 'owner': 'example-owner', 'repository': 'duplicate-b', 'path': 'README.md'}
    left = search_app.save_document('https://github.com/example-owner/duplicate-a/blob/main/README.md', 'duplicate-a README', 'A.', 200, 'text/plain', left_provenance)
    right = search_app.save_document('https://github.com/example-owner/duplicate-b/blob/main/README.md', 'duplicate-b README', 'B.', 200, 'text/plain', right_provenance)
    accepted = client.post('/github/relationship-reviews', json={
        'from_source_id': left['source_id'], 'to_source_id': right['source_id'], 'decision': 'accepted',
        'predicate': 'possibly_related_to', 'rationale': 'Initial acceptance.'
    }).json()
    changed = search_app.save_document('https://github.com/example-owner/duplicate-a/blob/main/README.md', 'duplicate-a README', 'A changed.', 200, 'text/plain', left_provenance)
    event = next(e for e in client.get('/github/relationship-change-events', params={'owner': 'example-owner'}).json() if e['relationship_id'] == accepted['resulting_relationship_id'])
    payload = {'event_id': event['event_id'], 'decision': 'disputed', 'rationale': 'Need further review.'}
    assert client.post('/github/relationship-revalidations', json=payload).status_code == 200
    assert client.post('/github/relationship-revalidations', json=payload).status_code == 409


def test_explorer_includes_stale_evidence_revalidation_controls(client):
    response = client.get('/github/explorer')
    assert response.status_code == 200
    assert 'Stale evidence awaiting review' in response.text
    assert '/github/relationship-change-events' in response.text
    assert '/github/relationship-change-comparisons' in response.text
    assert 'Compare exact source versions' in response.text
    assert '/github/relationship-revalidations' in response.text
    assert 'no_longer_supported' in response.text


def test_relationship_change_comparison_returns_exact_version_diff_and_context(client):
    left_provenance = {'method': 'github_repository_ingestion', 'owner': 'diff-owner', 'repository': 'diff-contract', 'path': 'README.md', 'git_blob_sha': 'old'}
    right_provenance = {'method': 'github_repository_ingestion', 'owner': 'diff-owner', 'repository': 'diff-impl', 'path': 'README.md', 'git_blob_sha': 'impl'}
    left = search_app.save_document('https://github.com/diff-owner/diff-contract/blob/main/README.md',
        'Contract README', 'Line one\nold contract line\nLine three', 200, 'text/plain', left_provenance)
    right = search_app.save_document('https://github.com/diff-owner/diff-impl/blob/main/README.md',
        'Implementation README', 'Implements old contract line', 200, 'text/plain', right_provenance)
    accepted = client.post('/github/relationship-reviews', json={
        'from_source_id': left['source_id'], 'to_source_id': right['source_id'],
        'decision': 'accepted', 'predicate': 'implements', 'rationale': 'Initial review.'
    })
    assert accepted.status_code == 200
    changed = search_app.save_document('https://github.com/diff-owner/diff-contract/blob/main/README.md',
        'Contract README', 'Line one\nnew contract line\nLine three\nNew requirement', 200, 'text/plain',
        {**left_provenance, 'git_blob_sha': 'new'})
    assert changed['outcome'] == 'changed'
    events = client.get('/github/relationship-change-events', params={'owner': 'diff-owner'}).json()
    event = next(e for e in events if e['relationship_id'] == accepted.json()['resulting_relationship_id'])
    response = client.get('/github/relationship-change-comparisons', params={'event_id': event['event_id']})
    assert response.status_code == 200
    payload = response.json()
    assert payload['previous_version_id'] == left['version_id']
    assert payload['current_version_id'] == changed['version_id']
    assert payload['stored_hashes_match_versions'] is True
    assert payload['added_lines'] >= 2
    assert payload['removed_lines'] >= 1
    assert any('old contract line' in line for line in payload['diff_lines'])
    assert any('new contract line' in line for line in payload['diff_lines'])
    assert payload['other_endpoint_current_version']['version_id'] == right['version_id']
    assert client.get('/github/relationship-change-comparisons', params={'event_id': 999999}).status_code == 404


def test_relationship_change_comparison_bounds_diff_output(client):
    left_provenance = {'method': 'github_repository_ingestion', 'owner': 'diff-owner', 'repository': 'diff-large-a', 'path': 'large.txt'}
    right_provenance = {'method': 'github_repository_ingestion', 'owner': 'diff-owner', 'repository': 'diff-large-b', 'path': 'small.txt'}
    old_body = '\n'.join('old line %s' % i for i in range(250))
    new_body = '\n'.join('new line %s' % i for i in range(250))
    left = search_app.save_document('https://github.com/diff-owner/diff-large-a/blob/main/large.txt',
        'Large old', old_body, 200, 'text/plain', left_provenance)
    right = search_app.save_document('https://github.com/diff-owner/diff-large-b/blob/main/small.txt',
        'Small', 'related', 200, 'text/plain', right_provenance)
    accepted = client.post('/github/relationship-reviews', json={
        'from_source_id': left['source_id'], 'to_source_id': right['source_id'],
        'decision': 'accepted', 'predicate': 'possibly_related_to', 'rationale': 'Initial review.'
    }).json()
    changed = search_app.save_document('https://github.com/diff-owner/diff-large-a/blob/main/large.txt',
        'Large new', new_body, 200, 'text/plain', left_provenance)
    event = next(e for e in client.get('/github/relationship-change-events', params={'owner': 'diff-owner'}).json() if e['relationship_id'] == accepted['resulting_relationship_id'])
    response = client.get('/github/relationship-change-comparisons', params={'event_id': event['event_id'], 'max_diff_lines': 100})
    assert response.status_code == 200
    assert response.json()['diff_truncated'] is True
    assert len(response.json()['diff_lines']) <= 100
    assert response.json()['added_lines'] >= 250

def test_search_any_mode_returns_documents_matching_either_term(client):
    search_app.save_document('https://example.org/a', 'Orchard notes', 'apples grow here', 200, 'text/plain')
    search_app.save_document('https://example.org/b', 'River notes', 'water flows here', 200, 'text/plain')
    response = client.get('/search', params={'q': 'apples water', 'mode': 'any'})
    assert response.status_code == 200
    assert {item['url'] for item in response.json()} == {'https://example.org/a', 'https://example.org/b'}

def test_search_phrase_mode_requires_adjacent_phrase(client):
    search_app.save_document('https://example.org/a', 'Phrase', 'records search engine', 200, 'text/plain')
    search_app.save_document('https://example.org/b', 'Separate', 'records are searchable; engine is separate', 200, 'text/plain')
    response = client.get('/search', params={'q': 'records search', 'mode': 'phrase'})
    assert response.status_code == 200
    assert [item['url'] for item in response.json()] == ['https://example.org/a']

def test_search_field_title_and_prefix_modes(client):
    search_app.save_document('https://example.org/a', 'Provenance Registry', 'unrelated text', 200, 'text/plain')
    search_app.save_document('https://example.org/b', 'Other title', 'provenance registry details', 200, 'text/plain')
    title_only = client.get('/search', params={'q': 'provenance', 'field': 'title'})
    assert [item['url'] for item in title_only.json()] == ['https://example.org/a']
    prefix = client.get('/search', params={'q': 'prove', 'mode': 'prefix'})
    assert {item['url'] for item in prefix.json()} == {'https://example.org/a', 'https://example.org/b'}

def test_search_rejects_unknown_mode_or_field(client):
    assert client.get('/search', params={'q': 'archive', 'mode': 'wildcard'}).status_code == 422
    assert client.get('/search', params={'q': 'archive', 'field': 'metadata'}).status_code == 422

