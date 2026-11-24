"""A real CLI process interrupted after paid stages must resume from disk."""
import subprocess
import sys

from carrel.store.paths import paper_dir, read_json


def test_ctrl_c_then_cli_resume_preserves_completed_stages(tmp_path):
    source = tmp_path / 'paper.pdf'
    source.write_bytes(b'%PDF-1.4 cli resume')
    library = tmp_path / 'library'
    code = r'''
from pathlib import Path
import _thread
import sys
from tests.conftest import FakeParser, FakeBackend, FakeRelations
from carrel import cli, scheduler
from carrel.pipeline import Pipeline

root, source, mode = sys.argv[1:]
class Parser(FakeParser):
    def parse(self, *args, **kwargs):
        with (Path(root) / 'parser-calls.txt').open('a') as stream:
            stream.write('parse\n')
        return super().parse(*args, **kwargs)

def make_pipeline(config, db, **kwargs):
    pipeline = Pipeline(config, db, parser=Parser(), backend=FakeBackend(),
                        relations=FakeRelations(), runtime=kwargs.get('runtime'))
    if mode == 'interrupt':
        def stop_after_structure(*args):
            _thread.interrupt_main()
            pipeline.runtime.stop_event.wait(10)
            pipeline.runtime.check_stopped()
        pipeline._stage_translate = stop_after_structure
    return pipeline
scheduler.Pipeline = make_pipeline
if mode == 'interrupt':
    arguments = ['--data-dir', root, 'batch', str(Path(source).parent / 'inputs.csv'), '--workers', '1', '--depth', 'full']
else:
    manifest = next((Path(root) / '_batches').glob('*.json'))
    arguments = ['--data-dir', root, 'batch', '--resume', manifest.stem, '--workers', '1']
raise SystemExit(cli.main(arguments))
'''
    import csv
    with (tmp_path / 'inputs.csv').open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=['local_path'])
        writer.writeheader()
        writer.writerow({'local_path': str(source)})
    command = [sys.executable, '-X', 'utf8', '-c', code, str(library), str(source)]
    first = subprocess.run([*command, 'interrupt'], capture_output=True, timeout=25)
    assert first.returncode == 130, first.stderr.decode('utf-8')
    manifest = next((library / '_batches').glob('*.json'))
    batch = read_json(manifest)
    assert not batch['finalized']
    base = paper_dir(library, batch['items'][0]['paper_id'])
    modules = {p.name: p.read_bytes() for p in (base / 'modules').glob('*.json')}
    assert modules
    second = subprocess.run([*command, 'resume'], capture_output=True, timeout=25)
    assert second.returncode == 0, second.stderr.decode('utf-8')
    assert read_json(manifest)['counts'] == {'succeeded': 1}
    assert (library / 'parser-calls.txt').read_text().splitlines() == ['parse']
    assert all((base / 'modules' / name).read_bytes() == data for name, data in modules.items())
    import sqlite3
    with sqlite3.connect(library / 'app.db') as conn:
        count = conn.execute("SELECT count(*) FROM model_runs WHERE task='extract'").fetchone()[0]
    assert count == len(modules)
