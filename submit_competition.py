#!/usr/bin/env python3
"""Submit the exact, already-pushed GitHub commit through the official CLI."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from tools.official_cli import cli_path

ROOT = Path(__file__).resolve().parent

def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT, text=True, encoding='utf-8').strip()

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--title', default='Native V4 GitHub submission')
    args = parser.parse_args()
    if git('status', '--porcelain'):
        raise SystemExit('Commit and push the working tree before submitting.')
    branch = git('branch', '--show-current')
    if not branch:
        raise SystemExit('Submit from a named pushed branch.')
    commit = git('rev-parse', 'HEAD')
    remote = git('remote', 'get-url', 'origin')
    match = re.fullmatch(r'(?:https://github\.com/|git@github\.com:)([\w.-]+/[\w.-]+?)(?:\.git)?/?', remote)
    if not match:
        raise SystemExit('origin must be a GitHub HTTPS or SSH repository URL.')
    repository = 'https://github.com/' + match.group(1)
    pushed = git('ls-remote', 'origin', 'refs/heads/' + branch).split()
    if not pushed or pushed[0] != commit:
        raise SystemExit('Push HEAD to origin before submitting.')
    templates = {'.env.example', '.env.sample', '.env.template'}
    for name in git('ls-files').splitlines():
        base = Path(name).name
        if base == '.env' or (base.startswith('.env.') and base not in templates):
            raise SystemExit('Remove tracked environment credentials before submitting.')
    manifest = json.loads((ROOT/'observer.project.json').read_text(encoding='utf-8'))
    if manifest.get('protocol') != 'jsonl-v4' or manifest.get('run') != ['python3', '-u', 'agent.py']:
        raise SystemExit('Unexpected root execution manifest.')
    done = subprocess.run([sys.executable, str(cli_path()), '--json', 'project', 'submit-repo',
                           repository, '--branch', commit, '--title', args.title, '--yes'],
                          capture_output=True, text=True, encoding='utf-8',
                          env={**os.environ, 'PYTHONIOENCODING':'utf-8'})
    if not done.stdout.strip():
        sys.stderr.write(done.stderr)
        return done.returncode or 1
    result = json.loads(done.stdout)
    folder = ROOT/'run_output/github_submissions'/commit
    folder.mkdir(parents=True, exist_ok=True)
    (folder/'submission.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    (folder/'source.json').write_text(json.dumps({'repository':repository,'commit':commit},indent=2),encoding='utf-8')
    sys.stdout.reconfigure(encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))
    return done.returncode

if __name__ == '__main__':
    raise SystemExit(main())
