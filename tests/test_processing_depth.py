"""Processing scope, upgrades, truthful coverage and graph preflight contracts."""
import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from carrel.profile import PRESETS, Profile, default_profile, parse_profile, requires, save_default, stored_profile
from carrel.coverage import paper_coverage, missing_capabilities
from carrel.graph.requirements import readiness
from carrel.motivation.tree import extend_tree, library_revision
from carrel.store.paths import paper_dir, read_json, atomic_write_json
from tests.conftest import FakeBackend


def test_profiles_normalize_validate_union_and_preference(data_dir):
    assert default_profile(data_dir) == PRESETS['skim']
    assert Profile().name == 'skim'
    assert Profile('none', motivation=True).extract == 'key'
    assert PRESETS['full'].union(PRESETS['skim']) == PRESETS['full']
    assert PRESETS['notes'].union(Profile('key', True)) == PRESETS['full']
    custom = Profile('all', False, False, False, False, False)
    assert custom.name == 'custom'
    save_default(data_dir, custom)
    assert default_profile(data_dir) == custom
    assert requires(PRESETS['indexed']) == set()
    with pytest.raises(ValueError):
        parse_profile({'extract':'key'})
    with pytest.raises(ValueError):
        Profile(translate='false')


def test_roman_numbered_intro_is_not_an_appendix():
    from carrel.profile import selected_section
    assert selected_section({'kind': 'introduction', 'title': 'I. INTRODUCTION'}, 'key')
    assert selected_section({'kind': 'unknown', 'title': 'A. Dataset collection'}, 'core')
    assert not selected_section({'kind': 'appendix', 'title': 'Appendix'}, 'core')
    assert selected_section({'kind': 'appendix', 'title': 'Appendix'}, 'all')


def test_skim_upgrade_reuses_paid_artifacts_and_never_downgrades(pipeline_factory, sample_pdf, db):
    backend = FakeBackend()
    pipeline = pipeline_factory(backend=backend)
    pid = pipeline.process(str(sample_pdf), profile='skim')
    base = paper_dir(pipeline.data_dir, pid)
    assert db.get_paper(pid)['status'] == 'ready'
    modules = {p.name:p.read_bytes() for p in (base/'modules').glob('*.json')}
    assert set(modules) == {'abstract.json','introduction.json','conclusion.json'}
    assert not (base/'translation').exists()
    assert not (base/'glossary.json').exists()
    assert not {'translate','glossary','term_review'} & {c['task'] for c in backend.calls}
    assert not missing_capabilities(PRESETS['skim'], paper_coverage(base))
    before = len(backend.calls)
    pipeline.process(str(sample_pdf), profile='full')
    calls = [c['task'] for c in backend.calls[before:]]
    assert 'extract' in calls and calls.count('summarize') == 1 and calls.count('motivation') == 1
    assert not {'classify','assess','preview'} & set(calls)
    assert all((base/'modules'/name).read_bytes() == body for name,body in modules.items())
    assert not missing_capabilities(PRESETS['full'], paper_coverage(base))
    snapshots = {p.relative_to(base):hashlib.sha256(p.read_bytes()).hexdigest() for p in base.rglob('*') if p.is_file()}
    before = len(backend.calls)
    pipeline.process(str(sample_pdf), profile='skim')
    assert len(backend.calls) == before
    assert stored_profile(db.get_paper(pid)) == PRESETS['full']
    assert snapshots == {p.relative_to(base):hashlib.sha256(p.read_bytes()).hexdigest() for p in base.rglob('*') if p.is_file()}


def test_indexed_then_skim_and_disabled_classification(pipeline_factory, sample_pdf, db):
    pipeline = pipeline_factory()
    pid = pipeline.process(str(sample_pdf), profile='indexed')
    assert db.get_paper(pid)['status'] == 'indexed'
    assert not pipeline.backend.calls and not pipeline.parser.calls
    profile = Profile('key', False, False, False, False, False)
    pipeline.process(str(sample_pdf), profile=profile)
    assert db.get_paper(pid)['status'] == 'ready'
    assert not (paper_dir(pipeline.data_dir,pid)/'classification.json').exists()
    assert not missing_capabilities(profile, paper_coverage(paper_dir(pipeline.data_dir,pid)))


@pytest.mark.parametrize('artifact,capability', [('summary','summary'),('classification','classified'),('assessment','assessed')])
def test_stale_fingerprints_are_not_coverage(pipeline_factory, sample_pdf, artifact, capability):
    pipeline = pipeline_factory()
    pid = pipeline.process(str(sample_pdf), profile='skim')
    base = paper_dir(pipeline.data_dir,pid)
    assert capability in paper_coverage(base)
    data = read_json(base/(artifact+'.json')); data['doc_fingerprint']='stale'
    atomic_write_json(base/(artifact+'.json'),data)
    assert capability not in paper_coverage(base)
    assert paper_coverage(base,'different-document') == set()


def test_graph_minimal_fill_exclusion_and_revision(pipeline_factory, sample_pdf, db):
    pipeline = pipeline_factory()
    pid = pipeline.process(str(sample_pdf), profile='skim')
    report = readiness(db,pipeline.data_dir,'motivation_tree')
    assert report['eligible']==[pid] and not report['missing']
    revision = library_revision(pipeline.config,db)
    db.upsert_paper({'id':'arxiv:2501.10000','title':'Unprocessed','status':'indexed'})
    assert library_revision(pipeline.config,db) == revision
    report = readiness(db,pipeline.data_dir,'motivation_tree')
    missing = report['missing'][0]
    fill = parse_profile(missing['fill_profile'])
    assert fill.extract == 'key' and fill.classify and fill.motivation
    assert not fill.translate and not fill.assess and not fill.relations
    before = len(pipeline.backend.calls)
    result = extend_tree(pipeline.config,db,pipeline.backend)
    assert result['paper_ids'] == [pid]
    assert result['excluded'][0]['id'] == missing['paper_id']
    assert len(pipeline.backend.calls) == before


def test_api_default_and_graph_selected_fill(config, db, pipeline_factory, sample_pdf):
    from carrel.web.app import create_app
    pipeline=pipeline_factory()
    pid=pipeline.process(str(sample_pdf),profile='indexed')
    app=create_app(config,db)
    received=[]
    app.state.jobs.submit_batch=lambda items,**kw: received.extend(items) or {'batch_id':'test'}
    client=TestClient(app)
    assert client.post('/api/preferences/processing',json={'profile':'notes'},headers={'X-Carrel':'1'}).status_code==200
    assert client.get('/api/processing-profiles').json()['default']['extract']=='core'
    response=client.post('/api/graph-fill',json={'graph':'motivation_tree','paper_ids':[pid]},headers={'X-Carrel':'1'})
    assert response.status_code==200
    assert received[0]['profile']['extract']=='key' and not received[0]['profile']['translate']
    assert client.post('/api/graph-fill',json={'paper_ids':['missing']},headers={'X-Carrel':'1'}).status_code==400
    assert client.get('/api/graph-readiness?graph=invalid').status_code==400


def test_graph_poll_does_not_pair_old_graph_with_terminal_job(config, db, monkeypatch):
    from carrel.graph.model import Graph
    from carrel.web.app import create_app

    app = create_app(config, db)
    db.create_job('snapshot-race', '@library', 'motivation_tree', '2026-11-23', status='running')

    def finish_during_read(focus=''):
        db.finish_job('snapshot-race', 'succeeded', '2026-11-23')
        return Graph(scope='motivation_tree', revision='old-artifact')

    monkeypatch.setattr(app.state.graphs, 'motivation_tree', finish_during_read)
    payload = TestClient(app).get('/api/motivation-tree').json()
    assert payload['revision'] == 'old-artifact'
    assert payload['stats']['rebuilding'] is True


def test_schema_migration_preserves_indexed_intent_and_existing_data(tmp_path):
    from carrel.store.db import Database

    path = tmp_path / 'app.db'
    db = Database(path)
    db.upsert_paper({'id': 'old-full', 'status': 'partial', 'title': 'Existing paper'})
    db.upsert_paper({'id': 'old-indexed', 'status': 'indexed', 'title': 'Local PDF'})
    db.execute('ALTER TABLE papers DROP COLUMN profile')
    db.execute("UPDATE schema_meta SET value='6' WHERE key='version'")
    db.close()
    db = Database(path)
    try:
        from carrel.store.db import SCHEMA_VERSION
        assert db.version == SCHEMA_VERSION
        assert stored_profile(db.get_paper('old-full')) == PRESETS['full']
        assert stored_profile(db.get_paper('old-indexed')) == PRESETS['indexed']
        assert db.get_paper('old-full')['title'] == 'Existing paper'
        assert list((tmp_path / '_backup').glob('schema-v6-*.db'))
    finally:
        db.close()


def test_indexed_human_classification_survives_upgrade(pipeline_factory, sample_pdf, db):
    pipeline = pipeline_factory()
    pid = pipeline.process(str(sample_pdf), profile='indexed')
    base = paper_dir(pipeline.data_dir, pid)
    categories = {'origin': 'human', 'discipline': 'My field', 'field': 'My category',
                  'categories': [{'discipline': 'My field', 'field': 'My category'}]}
    atomic_write_json(base / 'classification.json', categories)
    assert pid in readiness(db, pipeline.data_dir, 'library')['eligible']
    pipeline.process(str(sample_pdf), profile='skim')
    assert not any(c['task'] == 'classify' for c in pipeline.backend.calls)
    assert read_json(base / 'classification.json') == categories
    assert db.get_paper(pid)['field'] == 'My category'


def test_section_skips_are_scoped_to_one_chapter():
    from carrel.profile import section_identity, skipped_sections

    sections = [{'kind': 'unknown', 'title': 'Sparse table', 'block_ids': ['b1']},
                {'kind': 'unknown', 'title': 'Method details', 'block_ids': ['b2', 'b3']}]
    legacy = {'doc_fingerprint': 'fp', 'sections': ['unknown']}
    assert skipped_sections(legacy, sections, 'fp') == set()
    first = section_identity(sections[0])
    current = {**legacy, 'schema_version': 2, 'sections': [first]}
    assert skipped_sections(current, sections, 'fp') == {first}
    assert section_identity(sections[1]) not in skipped_sections(current, sections, 'fp')


def test_relations_only_profile_needs_no_parse_or_model(pipeline_factory, sample_pdf):
    pipeline = pipeline_factory()
    profile = Profile('none', False, False, False, False, True)
    pid = pipeline.process(str(sample_pdf), profile=profile)
    base = paper_dir(pipeline.data_dir, pid)
    assert paper_coverage(base) == {'relations'}
    before = (base / 'relations.json').stat().st_mtime_ns
    pipeline.process(str(sample_pdf), profile=profile)
    assert (base / 'relations.json').stat().st_mtime_ns == before
    assert not pipeline.backend.calls and not pipeline.parser.calls


def test_paid_modules_outside_requested_scope_are_kept(pipeline_factory, sample_pdf):
    pipeline = pipeline_factory()
    pid = pipeline.process(str(sample_pdf), profile=Profile('all', False))
    base = paper_dir(pipeline.data_dir, pid)
    original = {p.name: p.read_bytes() for p in (base / 'modules').glob('*.json')}
    # Emulate a restored target setting; actual artifacts may have wider scope.
    pipeline.db.execute('UPDATE papers SET profile=? WHERE id=?', (json.dumps(PRESETS['skim'].to_dict()), pid))
    (base / 'summary.json').unlink()
    pipeline.process(str(sample_pdf), profile='skim')
    assert all((base / 'modules' / key).read_bytes() == value for key, value in original.items())


def test_key_translation_is_incomplete_for_full_scope(pipeline_factory, sample_pdf):
    pipeline = pipeline_factory()
    pid = pipeline.process(str(sample_pdf), profile=Profile('key', True))
    base = paper_dir(pipeline.data_dir, pid)
    assert 'translated' not in missing_capabilities(Profile('key', True), paper_coverage(base))
    assert 'translated' in missing_capabilities(PRESETS['full'], paper_coverage(base))
    before = len(pipeline.backend.calls)
    pipeline.process(str(sample_pdf), profile='full')
    assert 'translate' in [c['task'] for c in pipeline.backend.calls[before:]]
    assert not missing_capabilities(PRESETS['full'], paper_coverage(base))


def test_estimates_do_not_discount_skim_history_twice(db):
    from carrel.profile import task_estimates, estimate

    db.upsert_paper({'id': 'skim-history', 'status': 'ready', 'profile': PRESETS['skim'].to_dict()})
    db.record_run({'id': 'extract-history', 'paper_id': 'skim-history', 'task': 'extract',
                   'status': 'ok', 'tokens_in': 2200, 'cost_usd': 0})
    tasks = task_estimates(db)
    assert tasks['extract']['tokens'] == 10000
    assert tasks['extract']['price_source'] == 'reference'
    cost = estimate(db, PRESETS['skim'], tasks=tasks)
    assert round(cost['tasks']['extract'] * tasks['extract']['tokens']) == 2200


def test_batch_resume_keeps_saved_profile_instead_of_new_default(config, db, monkeypatch):
    from carrel.scheduler import ImportScheduler
    from tests.test_scheduler import Pipeline

    first = ImportScheduler(config, db, pipeline=Pipeline(db), finalizer=lambda batch: None)
    monkeypatch.setattr(first, '_start', lambda: None)
    bid = first.submit(['arxiv:2401.00003'], profile='notes')
    save_default(config.data_dir, PRESETS['indexed'])
    received = []
    pipeline = Pipeline(db)
    original = pipeline.process

    def process(raw, **options):
        received.append(options['profile'])
        return original(raw, **options)

    pipeline.process = process
    second = ImportScheduler(config, db, pipeline=pipeline, finalizer=lambda batch: None)
    try:
        second.resume(bid)
        assert second.wait(bid, timeout=5)['finalized']
        assert received == [PRESETS['notes'].to_dict()]
    finally:
        second.shutdown()
