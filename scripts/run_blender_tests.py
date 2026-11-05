"""Run every headless Blender suite against a built add-on.

    python3 scripts/build_addon.py
    python3 scripts/run_blender_tests.py [--blender /path/to/blender]
"""
import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUITES = ('smoke', 'tools', 'modal', 'sources', 'session')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--blender', default=os.environ.get('BLENDER', shutil.which('blender') or 'blender'))
    parser.add_argument('suites', nargs='*', default=SUITES)
    args = parser.parse_args()
    failures = []
    for suite in args.suites:
        script = ROOT / 'tests' / f'blender_{suite}.py'
        started = time.monotonic()
        result = subprocess.run(
            [args.blender, '--background', '--factory-startup', '--python-exit-code', '1', '--python', str(script)],
            cwd=ROOT, capture_output=True, text=True)
        elapsed = time.monotonic() - started
        ok = result.returncode == 0 and ' passed' in result.stdout  # guard against silent no-ops
        print(f'{"PASS" if ok else "FAIL"}  {suite:<8} {elapsed:5.1f}s', flush=True)
        if not ok:
            failures.append(suite)
            print(result.stdout[-6000:], result.stderr[-6000:], sep='\n')
    if failures:
        print('Failed:', ', '.join(failures))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
