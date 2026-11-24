"""Smoke-test the installed CLI and browser assets with an empty temporary library.

Uses only the standard library and local HTTP; never calls a provider API.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.request import urlopen


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--temp-dir', type=Path, help='parent for the temporary library')
    args = parser.parse_args()
    config = Path(__file__).resolve().parents[1] / 'config/example.toml'
    if args.temp_dir:
        args.temp_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='carrel-install-', dir=args.temp_dir) as temp:
        root = Path(temp)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(('CARREL_', 'MINERU_', 'ANTHROPIC_',
                                      'OPENAI_', 'OPENALEX_', 'SCIVERSE_'))
               and key != 'PYTHONPATH'}
        command = [sys.executable, '-m', 'carrel.cli', '--config', str(config),
                   '--data-dir', str(root / 'data'), 'serve', '--port', str(port)]
        with (root / 'server.log').open('w+', encoding='utf-8') as log:
            process = subprocess.Popen(command, cwd=root, env=env, stdout=log, stderr=log,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            try:
                base = f'http://127.0.0.1:{port}'
                deadline = time.monotonic() + 30
                while True:
                    try:
                        with urlopen(base + '/api/papers', timeout=1) as response:
                            assert response.status == 200
                        break
                    except (URLError, TimeoutError):
                        if process.poll() is not None or time.monotonic() >= deadline:
                            raise RuntimeError('The installed CLI did not become ready')
                        time.sleep(.1)
                paths = ['/', '/settings', '/categories', '/relations', '/import', '/jobs',
                         '/api/papers', '/api/graph/explore', '/static/app.js',
                         '/static/graph-layout-worker.js', '/static/vendor/pdf.worker.min.js',
                         '/static/vendor/katex/fonts/KaTeX_Main-Regular.woff2',
                         '/static/vendor/elk.LICENSE']
                for path in paths:
                    with urlopen(base + path, timeout=10) as response:
                        assert response.status == 200 and response.read(), path
                print(f'Installed CLI smoke passed: {len(paths)} HTTP routes/assets, empty library.')
            except Exception:
                log.flush()
                log.seek(0)
                print(log.read(), file=sys.stderr)
                raise
            finally:
                # Windows venv python.exe is a launcher with a child interpreter.
                # Terminate our whole test process tree before removing its DB.
                if os.name == 'nt' and process.poll() is None:
                    subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=subprocess.CREATE_NO_WINDOW, check=False)
                else:
                    process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == '__main__':
    main()
