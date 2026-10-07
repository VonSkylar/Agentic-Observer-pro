"""Run the official survey26 CLI, cached locally and never committed with secrets."""
import os
from pathlib import Path
import subprocess
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
CLI_URL = 'https://create.gosim.org/survey26/platform/survey26.py'
CACHE = ROOT / '.survey26-cache' / 'survey26.py'

def cli_path():
    if not CACHE.is_file():
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        data = urllib.request.urlopen(CLI_URL, timeout=45).read()
        compile(data, CLI_URL, 'exec')
        temporary = CACHE.with_suffix('.tmp')
        temporary.write_bytes(data)
        temporary.replace(CACHE)
    return CACHE

def main():
    env = {**os.environ, 'PYTHONIOENCODING': 'utf-8'}
    return subprocess.call([sys.executable, str(cli_path()), *sys.argv[1:]], env=env)

if __name__ == '__main__':
    raise SystemExit(main())
