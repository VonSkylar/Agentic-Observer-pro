"""Immutable public-card benchmarks. Never starts platform evaluations."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--kit', type=Path, required=True, help='Official examples/_local directory')
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--cards', nargs='+', default=['L1', 'L2', 'L3', 'L4'])
    p.add_argument('--source', type=Path, default=ROOT)
    p.add_argument('--set', action='append', default=[], metavar='PRO_NAME=VALUE')
    p.add_argument('--fixed-level', type=int, choices=range(4))
    p.add_argument('--env-file', type=Path)
    p.add_argument('--with-model', action='store_true')
    args = p.parse_args()
    out = args.out.resolve()
    if out.exists():
        p.error('Use a new output directory; snapshots must remain immutable.')
    snapshot = out / 'source'
    snapshot.mkdir(parents=True)
    for f in args.source.glob('*.py'):
        shutil.copy2(f, snapshot/f.name)
    overrides = {}
    for setting in args.set:
        key, sep, value = setting.partition('=')
        if not sep or not key.startswith('PRO_'):
            p.error('--set accepts only PRO_NAME=VALUE')
        overrides[key] = value
    if args.fixed_level is not None:
        overrides['PRO_FIXED_LEVEL'] = str(args.fixed_level)
    env = {k:v for k,v in os.environ.items() if not k.startswith(('PRO_', 'OPENAI_', 'KIMI_', 'OBSERVER_'))}
    if args.env_file:
        # The official runner reads .env inside the private snapshot only.
        shutil.copy2(args.env_file, snapshot/'.env')
        (snapshot/'.env').chmod(0o600)
    if args.with_model and not args.env_file:
        p.error('--with-model requires an explicit private --env-file')
    env.update(overrides)
    env['OBSERVER_MODEL_DISABLED'] = '0' if args.with_model else '1'
    kit = args.kit.resolve()
    metadata = {'commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
                'source_hashes':{f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in snapshot.glob('*.py')},
                'overrides':overrides,'model_enabled':args.with_model,
                'engine_manifest_sha256':hashlib.sha256((kit/'runner/ENGINE_MANIFEST.json').read_bytes()).hexdigest()}
    (out/'metadata.json').write_text(json.dumps(metadata,indent=2))
    results = []
    try:
        for card in args.cards:
            folder = out/card
            folder.mkdir()
            started = time.monotonic()
            command = [sys.executable,str(kit/'runner/run_local.py'),'--inherit-env',
                       '--card',str(kit/'cards'/card),'--agent',f'"{sys.executable}" -u agent.py',
                       '--agent-cwd',str(snapshot),'--wallclock','900','--out',str(folder),'--quiet']
            with (folder/'runner.log').open('w') as log:
                subprocess.run(command,env=env,stdout=log,stderr=subprocess.STDOUT,check=True)
            workflow = json.loads((folder/'workflow_result.json').read_text())
            score = workflow['score_report']
            row = {'card':card,'score':score['total'],'components':score['components'],'counts':score['counts'],
                   'termination':workflow['termination_reason'],'clock':workflow['fair_clock'],
                   'elapsed_seconds':time.monotonic()-started,'scenario_sha256':score.get('sha256',{}).get('scenario')}
            results.append(row)
            (out/'summary.json').write_text(json.dumps({'results':results,'mean_score':sum(r['score'] for r in results)/len(results)},indent=2))
            print(json.dumps(row),flush=True)
    finally:
        (snapshot/'.env').unlink(missing_ok=True)


if __name__ == '__main__':
    main()
