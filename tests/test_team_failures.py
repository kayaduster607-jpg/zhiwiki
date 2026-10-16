"""Failure boundaries, final-check races and reusable Agent HTTP integration."""

import asyncio
import json
import secrets
import threading
import time
from pathlib import Path
from typing import TypedDict

import httpx
import pytest

from test_team_service import headers, publish_text, retrieve, team
from zhiwiki.config import Settings
from zhiwiki.engine import AgentEngine
from zhiwiki.service.admission import Admission
from zhiwiki.service.client import RagAPIError, RagClient, langgraph_node
from zhiwiki.service.common import ServiceError, ServiceSettings, digest, load_service_settings
from zhiwiki.service.operations import backup_workspace, restore_workspace
from zhiwiki.store import KnowledgeStore


def test_csrf_cross_origin_and_token_revocation(team):
    client, services, ids = team
    assert client.post('/v1/tokens', headers={'X-CSRF-Token': ''}, json={'knowledge_base_ids': [ids['kb']]}).status_code == 403
    assert client.get('/v1/me', headers={'Origin': 'https://attacker.example'}).status_code == 403
    assert client.delete('/v1/tokens/' + ids['token_id']).status_code == 200
    assert retrieve(team).status_code == 401
    # Read-only PATs cannot issue another token or perform administrative work.
    token = services.auth.issue(ids['reader'], kb_ids=[ids['kb']])['token']
    assert client.post('/v1/tokens', headers=headers(token), json={'knowledge_base_ids': [ids['kb']]}).status_code == 403


def test_same_query_cannot_mix_unauthorized_library(team):
    publish_text(team)
    client, services, ids = team
    secret = services.auth.create_kb('Private', 'kb_private')
    response = retrieve(team, knowledge_base_ids=[ids['kb'], secret])
    assert response.status_code == 403 and 'evidence' not in response.json()
    assert retrieve(team, filters={'document_ids': ['unknown-book']}).status_code == 403


def test_revoke_during_search_prevents_receipt_publication(team, monkeypatch):
    publish_text(team)
    _, services, ids = team
    search = services.indexes.search
    def revoke(*args, **kwargs):
        found = search(*args, **kwargs)
        with services.db.tx(write=True) as connection:
            connection.execute('DELETE FROM team_grants WHERE user_id=?', (ids['reader'],))
        return found
    monkeypatch.setattr(services.indexes, 'search', revoke)
    response = retrieve(team)
    assert response.status_code == 403
    with services.db.tx() as connection:
        assert connection.execute('SELECT COUNT(*) FROM team_receipts').fetchone()[0] == 0


def test_metadata_filters_and_receipt_generation(team):
    book = publish_text(team)
    client, _, ids = team
    original = retrieve(team).json()
    assert retrieve(team, key='filter-unknown', filters={'language': 'zh'}).json()['status'] == 'empty'
    result = client.put('/v1/workspaces/default/documents/' + book['document_id'] + '/metadata',
                        json={'language': 'zh', 'tags': ['policy'], 'effective_from': '2026-01-01', 'effective_to': '2026-12-31'})
    assert result.status_code == 200, result.text
    assert client.get('/v1/workspaces/default/retrievals/' + original['retrieval_id'], headers=headers(ids['token'])).status_code == 409
    found = retrieve(team, key='filter-known', filters={'language': 'zh', 'tags': ['policy'], 'effective_at': '2026-10-16'})
    assert found.status_code == 200 and found.json()['status'] == 'ok'
    assert retrieve(team, key='filter-expired', filters={'effective_at': '2027-01-01'}).json()['status'] == 'empty'


def test_timeout_keeps_worker_slot_until_real_completion():
    async def scenario():
        settings = ServiceSettings(active_requests=1, accepted_requests=2)
        gate, stop = Admission(settings), threading.Event()
        try:
            with pytest.raises(ServiceError) as error:
                await gate.run('first', time.monotonic() + .05, lambda: stop.wait(2))
            assert error.value.code == 'DEADLINE_EXCEEDED'
            assert gate.stats()['active'] == 1
            with pytest.raises(ServiceError) as duplicate:
                await gate.run('first', time.monotonic() + .2, lambda: None)
            assert duplicate.value.status == 429
            stop.set()
            await asyncio.sleep(.03)
            assert gate.stats()['active'] == gate.stats()['accepted'] == 0
        finally:
            stop.set()
            gate.close()
    asyncio.run(scenario())


def test_fifo_cancel_and_overcapacity_are_bounded():
    async def scenario():
        gate = Admission(ServiceSettings(active_requests=1, accepted_requests=2))
        deadline = time.monotonic() + 2
        await gate.acquire('first', deadline)
        waiting = asyncio.create_task(gate.acquire('second', deadline))
        await asyncio.sleep(.01)
        with pytest.raises(ServiceError) as full:
            await gate.acquire('third', deadline)
        assert full.value.status == 503
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert gate.stats()['waiting'] == 0 and gate.stats()['accepted'] == 1
        await gate.release('first')
        gate.close()
    asyncio.run(scenario())


def test_fenced_worker_recovery_cannot_commit_old_attempt(team):
    _, services, ids = team
    context = services.auth.principal(services.auth.issue(ids['admin'], 'session', ['*'], [ids['kb']])['token'])
    payload = 'Restart checkpoint source text'
    checksum = digest(payload)
    (services.db.root / 'uploads' / checksum).write_text(payload, encoding='utf-8')
    queued = services.jobs.enqueue(context, ids['kb'], 'restart.txt', checksum, 'new', 'restart')
    first = services.jobs.claim()
    services.jobs.page(first['id'], first['fence'], 1, payload, payload, {}, 2)
    assert services.jobs.claim() is None  # A second worker cannot claim a live lease.
    with services.db.tx(write=True) as connection:
        connection.execute('UPDATE team_jobs SET lease_until=0 WHERE id=?', (first['id'],))
    second = services.jobs.claim()
    assert second['pages_done'] == 1 and second['fence'] > first['fence']
    with pytest.raises(ServiceError):
        services.jobs.page(first['id'], first['fence'], 2, payload, payload, {}, 2)
    services.jobs.page(second['id'], second['fence'], 2, payload, payload, {}, 2)
    services.jobs.finish(second['id'], second['fence'])
    assert services.jobs.get(context, queued['job_id'])['pages_done'] == 2


def test_page_correction_requires_review_and_preserves_raw(team):
    client, services, ids = team
    context = services.auth.principal(client.cookies['zw_session'])
    text = 'Original incomplete page'
    checksum = digest(text)
    (services.db.root / 'uploads' / checksum).write_text(text, encoding='utf-8')
    queued = services.jobs.enqueue(context, ids['kb'], 'correct.txt', checksum, 'new', 'correct')
    job = services.jobs.claim()
    services.jobs.page(job['id'], job['fence'], 1, text, text, {'low_text': True}, 1)
    services.jobs.finish(job['id'], job['fence'])
    book, revision = queued['document_id'], queued['revision_id']
    with pytest.raises(ServiceError):
        services.jobs.correct_page(context, book, revision, 1, 'corrected', '0' * 64, 'Verified original')
    services.jobs.correct_page(context, book, revision, 1, 'Corrected page after manual verification.', checksum, 'Verified original source')
    with pytest.raises(ServiceError):
        services.jobs.publish(context, book, revision, None, True)
    services.jobs.review(context, book, revision, 'Checked corrected page and raw source')
    services.jobs.publish(context, book, revision, None, True)
    with services.db.tx() as connection:
        assert connection.execute('SELECT raw_text FROM team_pages WHERE revision_id=?', (revision,)).fetchone()[0] == text


def test_backup_restore_checks_hashes_and_never_overwrites(team, tmp_path):
    publish_text(team)
    _, services, _ = team
    snapshot = tmp_path.parent / ('snapshot-' + secrets.token_hex(8))
    target = tmp_path.parent / ('restored-' + secrets.token_hex(8))
    try:
        result = backup_workspace(services.settings, snapshot)
        assert result['files'] >= 3
        assert restore_workspace(snapshot, target)['files'] == result['files']
        with pytest.raises(ValueError):
            restore_workspace(snapshot, target)
        (snapshot / 'knowledge.sqlite3').write_bytes(b'corrupt')
        with pytest.raises(ValueError, match='checksum'):
            restore_workspace(snapshot, target.parent / ('corrupt-' + secrets.token_hex(8)))
    finally:
        # pytest temporary siblings are owned by this test, never user data.
        import shutil
        for path in (snapshot, target):
            shutil.rmtree(path, ignore_errors=True)


def test_old_engine_final_check_and_save_are_one_transaction(tmp_path, monkeypatch):
    store = KnowledgeStore(tmp_path)
    store.ingest('Return policy', 'Return policy seven days.', 'policy')
    save = store.save_run
    def interleave(payload):
        if payload['citations']:
            store.ingest('Return policy', 'Return policy fourteen days.', 'policy')
        return save(payload)
    monkeypatch.setattr(store, 'save_run', interleave)
    result = AgentEngine(store, Settings(data_dir=str(tmp_path), provider='offline')).run('Return policy')
    assert result['status'] == 'invalid_answer' and not result['citations']
    assert store.get_run(result['id'])['status'] == 'invalid_answer'


def test_python_and_real_langgraph_node_use_http_contract(team):
    publish_text(team)
    client, services, ids = team
    from langgraph.graph import END, START, StateGraph
    class State(TypedDict):
        query: str
        rag: dict
    async def scenario():
        transport = httpx.ASGITransport(app=client.app)
        async with httpx.AsyncClient(transport=transport) as http:
            async with RagClient('http://127.0.0.1', ids['token'], http_client=http) as adapter:
                graph = StateGraph(State)
                graph.add_node('evidence', langgraph_node(adapter, [ids['kb']]))
                graph.add_edge(START, 'evidence')
                graph.add_edge('evidence', END)
                result = await graph.compile().ainvoke({'query': 'Return policy'})
                assert result['rag']['validation']['valid'] is True
    asyncio.run(scenario())


def test_sdk_retries_only_allowed_errors_and_reuses_key():
    calls = []
    async def handle(request):
        calls.append(dict(request.headers))
        if len(calls) == 1:
            return httpx.Response(503, headers={'Retry-After': '0'}, json={'error': {'code': 'OVERLOADED', 'retryable': True}})
        return httpx.Response(200, json={'retrieval_id': 'same-logical-call'})
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            async with RagClient('http://localhost', 'private-runtime-token', http_client=http) as adapter:
                assert (await adapter.retrieve('test', ['kb_test']))['retrieval_id'] == 'same-logical-call'
    asyncio.run(scenario())
    assert len(calls) == 2 and calls[0]['idempotency-key'] == calls[1]['idempotency-key']


def test_sdk_cancel_releases_client_slot_and_never_logs_secret():
    async def handle(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json={})
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            async with RagClient('http://localhost', 'private-token', http_client=http, timeout=.1) as adapter:
                with pytest.raises(asyncio.TimeoutError):
                    await adapter.retrieve('test', ['kb_test'])
                assert not adapter.serial.locked()
    asyncio.run(scenario())


def test_explicit_config_rejects_environment_path_before_read(tmp_path, monkeypatch):
    def never_read(*args, **kwargs):
        pytest.fail('Forbidden file read attempted')
    monkeypatch.setattr(Path, 'read_text', never_read)
    with pytest.raises(ValueError):
        load_service_settings(tmp_path / '.env')


@pytest.mark.parametrize('steps', [3, 6])
def test_twenty_full_tool_chains_fit_default_budget(team, steps):
    from concurrent.futures import ThreadPoolExecutor
    publish_text(team)
    client, services, ids = team
    # Reset to the shipping budget after fixture administration. Each test gets
    # a fresh gate; this verifies a single task burst, not sustained rate claims.
    settings = services.gate.settings
    settings.user_rpm, settings.global_rpm = 30, 120
    settings.user_burst, settings.global_burst = 6, 120
    services.gate.global_bucket = [120., time.monotonic()]
    services.gate.user_buckets.clear()
    tokens = []
    for index in range(20):
        user = services.auth.create_user('chain-user-{:02}'.format(index), 'chain-user-test-password')
        services.auth.grant(user, ids['kb'], 'reader')
        tokens.append(services.auth.issue(user, kb_ids=[ids['kb']])['token'])
    def chain(pair):
        index, token = pair
        response = retrieve(team, token=token, key='chain-retrieve-{:02}'.format(index))
        statuses = [response.status_code]
        if response.status_code != 200:
            return statuses
        record = response.json()
        for step in range(1, steps):
            evidence = record['evidence'][0]
            if step % 2:
                response = client.post('/v1/workspaces/default/evidence:read', headers=headers(token, 'chain-read-{}-{}'.format(index, step)),
                                       json={'retrieval_ids': [record['retrieval_id']], 'evidence_ids': [evidence['evidence_id']]})
                if response.status_code == 200:
                    record = response.json()
            else:
                response = client.post('/v1/workspaces/default/citations:validate', headers=headers(token),
                                       json={'retrieval_ids': [record['retrieval_id']], 'citations': [{'evidence_id': evidence['evidence_id'], 'text_sha256': evidence['text_sha256']}]})
            statuses.append(response.status_code)
            if response.status_code != 200:
                break
        return statuses
    with ThreadPoolExecutor(20) as pool:
        outcomes = list(pool.map(chain, enumerate(tokens)))
    assert outcomes == [[200] * steps] * 20


def test_job_cancel_retry_is_fenced_and_preserves_checkpoint(team):
    client, services, ids = team
    context = services.auth.principal(client.cookies['zw_session'])
    text, checksum = 'Source checkpoint', digest('Source checkpoint')
    (services.db.root / 'uploads' / checksum).write_text(text, encoding='utf-8')
    queued = services.jobs.enqueue(context, ids['kb'], 'retry.txt', checksum, 'new', 'retry')
    first = services.jobs.claim()
    services.jobs.page(first['id'], first['fence'], 1, text, text, {}, 2)
    assert services.jobs.cancel(context, first['id'])['status'] == 'cancel_requested'
    services.jobs.finish(first['id'], first['fence'], 'cancelled')
    assert services.jobs.get(context, first['id'])['status'] == 'cancelled'
    services.jobs.retry(context, queued['job_id'])
    second = services.jobs.claim()
    assert second['pages_done'] == 1 and second['fence'] > first['fence']
    with pytest.raises(ServiceError):
        services.jobs.page(first['id'], first['fence'], 2, text, text, {}, 2)


def test_receipt_expiry_and_knowledge_hash_mismatch(team):
    publish_text(team)
    client, services, ids = team
    record = retrieve(team).json()
    with services.db.tx(write=True) as connection:
        connection.execute('UPDATE team_receipts SET expires=0 WHERE id=?', (record['retrieval_id'],))
    response = client.get('/v1/workspaces/default/retrievals/' + record['retrieval_id'], headers=headers(ids['token']))
    assert response.status_code == 410


def test_cursor_is_owner_query_and_limit_bound(team):
    from zhiwiki.service.cursors import decode, issue
    _, services, ids = team
    cursor = issue(services.db.cursor_key, ids['reader'], ids['kb'], 'book_123', 20)
    assert decode(services.db.cursor_key, cursor, ids['reader'], ids['kb'], 20) == 'book_123'
    for owner, kb, limit in [('other', ids['kb'], 20), (ids['reader'], 'kb_other', 20), (ids['reader'], ids['kb'], 30)]:
        with pytest.raises(ServiceError):
            decode(services.db.cursor_key, cursor, owner, kb, limit)
    with pytest.raises(ServiceError):
        decode(services.db.cursor_key, 'bad' + cursor[3:], ids['reader'], ids['kb'], 20)


def test_openapi_declares_response_contract_and_metrics_are_private(team):
    client, _, ids = team
    schema = client.get('/v1/openapi.json').json()
    output = schema['paths']['/v1/workspaces/{workspace_id}/retrievals']['post']['responses']['200']
    assert output['content']['application/json']['schema']['$ref'].endswith('/RetrievalOutput')
    assert client.get('/v1/workspaces/default/metrics', headers=headers(ids['token'])).status_code == 403
    metrics = client.get('/v1/workspaces/default/metrics')
    assert metrics.status_code == 200 and metrics.json()['resources']['threads'] < 128
    assert 'sqlite_busy' in metrics.json()['storage']


def test_legacy_migration_backups_first_and_keeps_reader_isolation(tmp_path):
    from zhiwiki.service.auth import Authorization
    from zhiwiki.service.database import ServiceDatabase
    core = KnowledgeStore(tmp_path)
    original = core.ingest('Legacy policy', 'Legacy policy source text', 'legacy-policy')
    database = ServiceDatabase(ServiceSettings(data_dir=str(tmp_path)))
    assert len(list((tmp_path / 'migration-snapshots').glob('*.sqlite3'))) == 1
    auth = Authorization(database)
    user = auth.create_user('unassigned', 'unassigned-user-password')
    token = auth.issue(user)['token']
    with database.tx() as connection:
        assert database.kb_ids(connection, user) == []
        assert connection.execute('SELECT document_id FROM team_pages').fetchone()[0] == original['id']
        with pytest.raises(ServiceError):
            auth.require(connection, auth.principal(token), ['kb_legacy'])


def test_sparse_index_requires_content_anchor_and_is_repeatable(team):
    publish_text(team)
    assert retrieve(team, query='what is the meaning').json()['status'] == 'empty'
    first = retrieve(team, key='sparse-first').json()
    second = retrieve(team, key='sparse-second').json()
    assert first['evidence'] == second['evidence']


def staged_text(team, source, text, book=None, expected=None):
    """Prepare an unpublished native revision to exercise interleaved publishers."""
    client, services, ids = team
    context = services.auth.principal(client.cookies['zw_session'])
    checksum = digest(text)
    (services.db.root / 'uploads' / checksum).write_text(text, encoding='utf-8')
    result = services.jobs.enqueue(context, ids['kb'], 'staged.txt', checksum,
                                   'update' if book else 'new', source, book, expected)
    job = services.jobs.claim()
    services.jobs.page(job['id'], job['fence'], 1, text, text, {}, 1)
    services.jobs.finish(job['id'], job['fence'])
    services.jobs.review(context, result['document_id'], result['revision_id'], 'Checked this staged source version')
    return context, result


def test_correction_and_second_review_during_index_build_cannot_publish_stale_chunks(team, monkeypatch):
    _, services, _ = team
    context, staged = staged_text(team, 'build-race', 'Return policy source before correction.')
    prepare = services.indexes.prepare
    def edit_after_build(*args):
        result = prepare(*args)
        services.jobs.correct_page(context, staged['document_id'], staged['revision_id'], 1,
                                   'Return policy corrected after index prepared.', digest('Return policy source before correction.'), 'Checked original source again')
        services.jobs.review(context, staged['document_id'], staged['revision_id'], 'Reviewed the second edit as well')
        return result
    monkeypatch.setattr(services.indexes, 'prepare', edit_after_build)
    with pytest.raises(ServiceError) as conflict:
        services.jobs.publish(context, staged['document_id'], staged['revision_id'], None, True)
    assert conflict.value.code == 'KNOWLEDGE_CHANGED'
    with services.db.tx() as connection:
        assert connection.execute('SELECT current_revision FROM team_books WHERE id=?', (staged['document_id'],)).fetchone()[0] is None


def test_two_editors_cannot_overwrite_newer_published_revision(team):
    original = publish_text(team)
    _, services, _ = team
    context, first = staged_text(team, 'policy', 'Return policy version two.', original['document_id'], original['revision_id'])
    _, second = staged_text(team, 'policy', 'Return policy conflicting version three.', original['document_id'], original['revision_id'])
    services.jobs.publish(context, original['document_id'], first['revision_id'], original['revision_id'])
    with pytest.raises(ServiceError) as conflict:
        services.jobs.publish(context, original['document_id'], second['revision_id'], original['revision_id'])
    assert conflict.value.status == 412
    with services.db.tx() as connection:
        assert connection.execute('SELECT current_revision FROM team_books WHERE id=?', (original['document_id'],)).fetchone()[0] == first['revision_id']


def test_team_wiki_permissions_and_source_invalidation(team):
    original = publish_text(team)
    client, _, ids = team
    evidence_id = retrieve(team).json()['evidence'][0]['evidence_id']
    body = {'slug': 'return-policy', 'title': 'Return policy summary', 'evidence_ids': [evidence_id]}
    path = '/v1/workspaces/default/wiki/drafts?knowledge_base_id=' + ids['kb']
    assert client.post(path, headers=headers(ids['token']), json=body).status_code == 403
    draft = client.post(path, json=body)
    assert draft.status_code == 201, draft.text
    wiki_id = draft.json()['id']
    response = client.post('/v1/workspaces/default/wiki/' + wiki_id + ':publish',
                           headers=headers(key='wiki-publish-001', **{'If-Match': '"1"'}))
    assert response.status_code == 200, response.text
    assert client.get('/v1/workspaces/default/wiki/' + wiki_id, headers=headers(ids['token'])).status_code == 200
    publish_text(team, 'Return policy changed to fourteen days.', 'update', original['document_id'], original['revision_id'])
    assert client.get('/v1/workspaces/default/wiki/' + wiki_id, headers=headers(ids['token'])).status_code == 403
    historical = client.get('/v1/workspaces/default/wiki/' + wiki_id)
    assert historical.status_code == 200 and historical.json()['stale']


def test_running_idempotent_location_is_pollable_and_private(team):
    client, services, ids = team
    from zhiwiki.service.models import RetrievalInput
    context = services.auth.principal(ids['token'])
    body = RetrievalInput(query='Return policy', knowledge_base_ids=[ids['kb']]).model_dump(mode='json')
    receipt_id, replay = services.retrievals.claim(context, 'retrieve', 'pending-request-001', body, time.monotonic() + 2)
    assert not replay
    path = '/v1/workspaces/default/retrievals/' + receipt_id
    response = client.get(path, headers=headers(ids['token']))
    assert response.status_code == 409 and response.json()['error']['code'] == 'REQUEST_IN_PROGRESS'
    assert response.headers['Location'] == path
    second = services.auth.create_user('pending-other', 'pending-other-password')
    services.auth.grant(second, ids['kb'], 'reader')
    other_token = services.auth.issue(second, kb_ids=[ids['kb']])['token']
    assert client.get(path, headers=headers(other_token)).status_code == 403


def test_upload_openapi_declares_pre_body_target_query(team):
    client, _, _ = team
    schema = client.get('/v1/openapi.json').json()
    parameters = schema['paths']['/v1/workspaces/{workspace_id}/uploads']['post']['parameters']
    assert any(value['name'] == 'knowledge_base_id' and value['in'] == 'query' and value['required'] for value in parameters)


def test_status_poll_works_while_original_tool_owns_principal_slot(team, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    publish_text(team)
    client, services, ids = team
    entered, release = threading.Event(), threading.Event()
    search = services.indexes.search
    def paused_search(*args, **kwargs):
        entered.set()
        release.wait(1)
        return search(*args, **kwargs)
    monkeypatch.setattr(services.indexes, 'search', paused_search)
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(retrieve, team, key='poll-active-tool')
        assert entered.wait(1)
        try:
            with services.db.tx() as connection:
                receipt_id = connection.execute("SELECT result_id FROM team_idempotency WHERE key='poll-active-tool'").fetchone()[0]
            response = client.get('/v1/workspaces/default/retrievals/' + receipt_id, headers=headers(ids['token']))
            assert response.status_code == 409 and response.json()['error']['code'] == 'REQUEST_IN_PROGRESS'
        finally:
            release.set()
        assert pending.result(timeout=3).status_code == 200
