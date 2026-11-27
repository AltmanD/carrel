from __future__ import annotations

import json
from pathlib import Path

import pytest

from carrel.model import ModelReply, ModelSchemaError
from carrel.store.paths import atomic_write_json, paper_dir, read_json
from carrel.titles import (title_issues, ensure_title, project_title, save_human_title,
                           audit_title, _new_record, commit_title, recover_title)

SOURCE = 'Auditing Reward Hackability in Code RL Training Environments'
GOOD = '代码强化学习训练环境中的奖励漏洞可利用性审计'
BAD = '审计代码 RL 训练环境中的奖励可破解性\n\n**速览**\n\n' + '实验结果及核心贡献。' * 90


class TitleBackend:
    name = 'fake:title'

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def structured(self, **kwargs):
        self.calls.append(kwargs)
        answer = self.answers.pop(0)
        reply = ModelReply(text=json.dumps(answer, ensure_ascii=False), data=answer,
                           tokens_in=100, tokens_out=50, max_tokens=1500, model='fake',
                           task='title', stop_reason='stop', user_prompt=kwargs['user'])
        if isinstance(answer, list):
            raise ModelSchemaError('wrong shape', reply=reply)
        return reply


def seed(config, db, value='', *, trace=False):
    pid = 'pdf-title-test'
    db.upsert_paper({'id': pid, 'title': SOURCE, 'title_zh': value, 'status': 'ready'})
    base = paper_dir(config.data_dir, pid)
    atomic_write_json(base / 'preview.json', {'title_zh': value, 'one_line': '保留原速览'})
    if trace:
        atomic_write_json(base / 'runs/old/trace.json', {
            'task': 'preview', 'run_id': 'old', 'request': {'user': '标题：' + SOURCE + '\n\n摘要：内容'},
            'response_text': json.dumps({'title_zh': value}, ensure_ascii=False)})
    return pid, base


@pytest.mark.parametrize('value', [BAD, '正确输出应为：某个译名', '最终定稿：标题',
    '标题 {"title_zh":"另一个标题"}', {'title': '中文'}, ['标题'], 12, '',
    '这是一个足够长的重复中文标题；这是一个足够长的重复中文标题', SOURCE])
def test_reject_contaminated_title(value):
    assert title_issues(value, SOURCE)


@pytest.mark.parametrize('value', [
    'MuZero：基于学习模型的超人规划算法（MuZero）——不对，仅输出标题翻译',
    '标题——抱歉，重新翻译', '仅输出标题翻译',
    '学习模型规划——不对', '学习模型规划——错了，重新翻译',
    '抱歉，只输出中文标题', '学习模型规划——格式要求：仅输出标题',
    '标题翻译：学习模型规划',
])
def test_reject_translator_self_correction(value):
    assert title_issues(value, SOURCE)
    assert title_issues(value, SOURCE, human=True)
    projected = project_title({'id': 'p', 'title': SOURCE, 'title_zh': value})
    assert projected['title_pending']
    assert projected['display_title'] == SOURCE


def test_scientific_title_with_instruction_words_is_valid():
    assert not title_issues('JSON 输出格式的学习与评估', SOURCE)


@pytest.mark.parametrize('value', ['FlashAttention：面向 IO 的精确注意力',
    '[CLS] 表示的贡献：使用 $O(n^2)$ 复杂度的摘要方法', GOOD,
    '不对称博弈中的规划与学习', 'JSON 约束下的语言模型生成',
    '重新思考模型规划', '遵循规则的智能体学习', '标题翻译任务中的跨语言迁移'])
def test_valid_scientific_titles(value):
    assert not title_issues(value, SOURCE)


def test_long_titles_have_source_aware_and_human_confirmation_paths():
    long = '针对大规模科学计算系统的多任务建模方法' * 5
    assert not title_issues(long, 'A detailed scientific title ' * 9)
    assert title_issues(long, SOURCE)
    assert not title_issues(long * 2, SOURCE, human=True)
    assert title_issues(BAD, SOURCE, human=True)


def test_title_only_retry_accounting_and_preview_preserved(config, db):
    pid, base = seed(config, db, BAD, trace=True)
    backend = TitleBackend({'title_zh': BAD}, {'zh_title': GOOD})
    result = ensure_title(config, db, backend, pid)
    assert result['status'] == 'accepted'
    assert len(backend.calls) == 2
    assert all(c['user'] == '标题：' + SOURCE for c in backend.calls)
    assert '校验' in backend.calls[1]['system']
    assert db.get_paper(pid)['title_zh'] == GOOD
    assert read_json(base / 'preview.json') == {'title_zh': GOOD, 'one_line': '保留原速览'}
    runs = [dict(r) for r in db.query("SELECT * FROM model_runs WHERE task='title'")]
    assert [r['status'] for r in runs] == ['rejected', 'ok']
    assert [r['accepted'] for r in runs] == [0, 1]
    assert sum(r['tokens_out'] for r in runs) == 100
    assert runs[1]['retry_of'] == runs[0]['id']
    assert read_json(base / 'title.json')['source_title'] == SOURCE
    assert ensure_title(config, db, backend, pid) == result
    assert len(backend.calls) == 2


def test_shape_retry_is_not_multiplied_and_failed_cache_does_not_loop(config, db):
    pid, base = seed(config, db)
    backend = TitleBackend([], {'title_zh': BAD})
    result = ensure_title(config, db, backend, pid)
    assert result['status'] == 'failed'
    assert len(backend.calls) == 2
    assert db.get_paper(pid)['title_zh'] == ''
    assert project_title(db.get_paper(pid), base)['display_title'] == SOURCE
    ensure_title(config, db, backend, pid)
    assert len(backend.calls) == 2
    backend.answers.append({'title_zh': GOOD})
    assert ensure_title(config, db, backend, pid, retry=True)['status'] == 'accepted'


def test_source_change_invalidates_only_title(config, db):
    pid, base = seed(config, db)
    backend = TitleBackend({'title_zh': GOOD}, {'title_zh': '新论文的中文标题'})
    ensure_title(config, db, backend, pid)
    source2 = 'A New Paper About Training Environments'
    db.execute('UPDATE papers SET title=? WHERE id=?', (source2, pid))
    assert project_title(db.get_paper(pid), base)['display_title'] == source2
    ensure_title(config, db, backend, pid)
    assert len(backend.calls) == 2 and backend.calls[-1]['user'] == '标题：' + source2
    assert read_json(base / 'preview.json')['one_line'] == '保留原速览'


def test_legacy_provenance_and_source_change(config, db):
    pid, base = seed(config, db, GOOD, trace=True)
    new_source = 'A Different Paper On Reward Hacking'
    db.execute('UPDATE papers SET title=? WHERE id=?', (new_source, pid))
    audit = audit_title(db.get_paper(pid), base)
    assert audit['repairable'] and audit['source_title'] == new_source
    assert project_title(db.get_paper(pid), base)['display_title'] == new_source


def test_unknown_legacy_value_is_not_overwritten(config, db):
    pid, base = seed(config, db, BAD)
    backend = TitleBackend()
    assert ensure_title(config, db, backend, pid)['status'] == 'review'
    assert db.get_paper(pid)['title_zh'] == BAD
    assert not backend.calls
    assert project_title(db.get_paper(pid), base)['display_title'] == SOURCE


def test_manual_title_survives_source_and_prompt_changes(config, db, monkeypatch):
    import carrel.titles as titles
    pid, base = seed(config, db, BAD)
    long = '面向科学计算的中文标题' * 20
    save_human_title(config, db, pid, long)
    db.execute('UPDATE papers SET title=? WHERE id=?', ('An Updated Original Title', pid))
    monkeypatch.setattr(titles, 'PROMPT_VERSION', 2)
    assert ensure_title(config, db, TitleBackend(), pid, retry=True)['origin'] == 'human'
    assert project_title(db.get_paper(pid), base)['title_zh'] == long
    assert not audit_title(db.get_paper(pid), base)['repairable']


def test_missing_source_does_not_guess_from_filename(config, db):
    pid, base = seed(config, db)
    db.execute('UPDATE papers SET title=? WHERE id=?', ('SWE-agent', pid))
    backend = TitleBackend()
    assert ensure_title(config, db, backend, pid)['status'] == 'source_missing'
    assert not backend.calls


def test_interrupted_commit_recovers_without_new_model_call(config, db, monkeypatch):
    import carrel.titles as titles
    pid, base = seed(config, db)
    real_write = titles.atomic_write_json

    def fail_preview(path, data):
        if path.name == 'preview.json':
            raise OSError('simulated disk interruption')
        real_write(path, data)

    monkeypatch.setattr(titles, 'atomic_write_json', fail_preview)
    with pytest.raises(OSError):
        ensure_title(config, db, TitleBackend({'title_zh': GOOD}), pid)
    assert (base / 'title.pending.json').exists()
    monkeypatch.setattr(titles, 'atomic_write_json', real_write)
    backend = TitleBackend()
    assert ensure_title(config, db, backend, pid)['title_zh'] == GOOD
    assert not backend.calls and not (base / 'title.pending.json').exists()
    assert read_json(base / 'preview.json')['title_zh'] == GOOD


def test_commit_does_not_overwrite_concurrent_edit(config, db):
    pid, base = seed(config, db)
    expected = db.get_paper(pid)
    db.execute('UPDATE papers SET title_zh=? WHERE id=?', ('人工修改', pid))
    assert not commit_title(db, pid, base, expected, _new_record(SOURCE, GOOD))
    assert db.get_paper(pid)['title_zh'] == '人工修改'


def test_preview_drops_title_even_if_model_supplies_it():
    from carrel.structure.preview import build_preview
    backend = TitleBackend({'title_zh': BAD, 'one_line': '速览仍有效', 'tldr': '摘要'})
    preview, _ = build_preview(backend, title=SOURCE, abstract='Abstract text')
    assert 'title_zh' not in preview and preview['one_line'] == '速览仍有效'


def test_repair_backups_and_resumes_without_touching_body(config, db):
    from carrel.title_repair import audit_library, repair_library
    pid, base = seed(config, db, BAD, trace=True)
    (base / 'source.pdf').write_bytes(b'%PDF-preserve')
    atomic_write_json(base / 'translation/abstract.json', {'translations': {'p1': '原有正文译文'}})
    before = {p.relative_to(base): p.read_bytes() for p in base.rglob('*') if p.is_file()}
    report = audit_library(config.data_dir)
    assert report['repairable'] == 1
    assert before == {p.relative_to(base): p.read_bytes() for p in base.rglob('*') if p.is_file()}
    result = repair_library(config, backend=TitleBackend({'title_zh': GOOD}))
    assert result['counts'] == {'accepted': 1}
    backup = Path(result['backup'])
    assert (backup / 'app.db').exists()
    assert read_json(backup / 'papers' / base.name / 'preview.json')['title_zh'] == BAD
    for relative, data in before.items():
        if relative.name != 'preview.json':
            assert (base / relative).read_bytes() == data
    # Simulate a report checkpoint lost after the individual title commit.
    result['items'][0]['outcome'] = 'pending'
    atomic_write_json(backup / 'report.json', result)
    backend = TitleBackend()
    assert repair_library(config, backend=backend, resume=backup)['counts'] == {'accepted': 1}
    assert not backend.calls


def test_ready_pipeline_source_update_only_calls_title(pipeline_factory, tmp_path, db):
    from tests.conftest import FakeBackend, FakeParser
    backend, parser = FakeBackend(), FakeParser()
    pipeline = pipeline_factory(backend=backend, parser=parser)
    pdf = tmp_path / 'source.pdf'
    pdf.write_bytes(b'%PDF-1.4 title-only')
    pid = pipeline.process(str(pdf))
    previous = len(backend.calls)
    pipeline.process(str(pdf), title=SOURCE)
    assert [c['task'] for c in backend.calls[previous:]] == ['title']
    assert parser.calls == 1
    assert read_json(paper_dir(db.path.parent, pid) / 'title.json')['source_title'] == SOURCE


def test_title_interruption_preserves_completed_preview_and_extractions(pipeline_factory, tmp_path, db):
    from carrel.runtime.context import RunInterrupted
    from tests.conftest import FakeBackend

    class InterruptedTitle(FakeBackend):
        def structured(self, **kwargs):
            if kwargs.get('task') == 'title':
                raise RunInterrupted()
            return super().structured(**kwargs)

    pdf = tmp_path / 'source.pdf'
    pdf.write_bytes(b'%PDF-1.4 interrupted-title')
    with pytest.raises(RunInterrupted):
        pipeline_factory(backend=InterruptedTitle()).process(str(pdf))
    pid = db.list_papers()[0]['id']
    base = paper_dir(db.path.parent, pid)
    assert read_json(base / 'preview.json')['one_line']
    assert list((base / 'modules').glob('*.json'))
    assert any(row['task'] == 'extract' for row in db.runs_for(pid))


def test_model_title_regenerated_after_validator_upgrade(config, db, monkeypatch):
    import carrel.titles as titles
    pid, base = seed(config, db)
    backend = TitleBackend({'title_zh': GOOD}, {'title_zh': GOOD})
    ensure_title(config, db, backend, pid)
    monkeypatch.setattr(titles, 'VALIDATOR_VERSION', 2)
    assert project_title(db.get_paper(pid), base)['title_pending']
    assert ensure_title(config, db, backend, pid)['validator_version'] == 2
    assert len(backend.calls) == 2


def test_web_hides_invalid_database_and_preview_and_can_save(config, db):
    from fastapi.testclient import TestClient
    from carrel.web.app import create_app
    pid, base = seed(config, db, BAD, trace=True)
    app = create_app(config, db)
    client = TestClient(app)
    try:
        runs_before = len(db.runs_for(pid))
        for path in ('/', '/papers/' + pid, '/api/papers/' + pid, '/api/papers'):
            response = client.get(path)
            assert response.status_code == 200
            assert '**速览**' not in response.text
        assert len(db.runs_for(pid)) == runs_before
        assert client.post('/api/papers/' + pid + '/title', json={'action': 'save', 'title_zh': GOOD}).status_code == 403
        result = client.post('/api/papers/' + pid + '/title', headers={'X-Carrel': '1'},
                             json={'action': 'save', 'title_zh': GOOD})
        assert result.status_code == 200 and result.json()['status'] == 'accepted'
        assert db.get_paper(pid)['title_zh'] == GOOD
        assert read_json(base / 'title.json')['origin'] == 'human'
    finally:
        app.state.jobs.scheduler.shutdown()
