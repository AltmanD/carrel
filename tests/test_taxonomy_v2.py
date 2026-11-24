import json
import pytest
from carrel.classify.taxonomy import Taxonomy, project_path
from carrel.classify.migration import plan_migration, execute_plan, project_classification
from carrel.models import Classification


def test_two_levels_and_explicit_primary():
    assert all(not p.get('subfield') for p in Taxonomy.default().paths())
    raw = Classification(discipline='A', field_name='B').to_dict()
    assert 'subfield' not in raw
    assert raw['primary_category_id'] == raw['categories'][0]['category_id']
    other = project_path({'discipline': 'X', 'field': 'Y'})
    raw['categories'].insert(0, other)
    assert project_classification(raw)['discipline'] == 'A'


def test_rename_before_projection():
    path = {'discipline': 'Old', 'field': 'F', 'subfield': 'S'}
    result = project_path(path, [{'discipline': 'Old', 'name': 'New'},
                                 {'discipline': 'New', 'field': 'F', 'subfield': 'S', 'name': 'Topic'}])
    assert result['field'] == 'Topic'
    assert path['subfield'] == 'S'


def test_migration_is_read_only_resumable_and_reversible(tmp_path):
    path = tmp_path / 'papers' / 'p' / 'classification.json'
    path.parent.mkdir(parents=True)
    original = json.dumps({'discipline': 'A', 'field': 'B', 'subfield': 'C',
                           'tags': [{'tag': 'keep', 'origin': 'human'}]}).encode()
    path.write_bytes(original)
    plan = plan_migration(tmp_path)
    assert path.read_bytes() == original
    assert plan['entries'][0]['source'] == 'legacy'
    manifest = tmp_path / 'plan.json'
    manifest.write_text(json.dumps(plan), encoding='utf-8')
    execute_plan(tmp_path, manifest)
    first = path.read_bytes()
    execute_plan(tmp_path, manifest)
    assert path.read_bytes() == first
    assert json.loads(first)['tags'][0]['origin'] == 'human'
    path.write_text('{}', encoding='utf-8')
    with pytest.raises(ValueError, match='conflict'):
        execute_plan(tmp_path, manifest)
    path.write_bytes(first)
    execute_plan(tmp_path, manifest, rollback=True)
    assert path.read_bytes() == original
