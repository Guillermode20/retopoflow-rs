"""Fail unless Cargo.toml, bl_info and (optionally) a release tag agree.

    python3 scripts/check_version.py [--tag v0.3.0]
"""
import argparse
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def cargo_version():
    match = re.search(r'^version\s*=\s*"([^"]+)"', (ROOT / 'Cargo.toml').read_text(), re.M)
    return match.group(1)


def addon_version():
    tree = ast.parse((ROOT / 'blender' / 'retopoflow_rs' / '__init__.py').read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, 'id', None) == 'bl_info' for t in node.targets):
            info = ast.literal_eval(node.value)
            return '.'.join(str(part) for part in info['version'])
    raise SystemExit('bl_info not found')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tag', help='release tag such as v0.3.0')
    args = parser.parse_args()
    versions = {'Cargo.toml': cargo_version(), 'bl_info': addon_version()}
    if args.tag:
        versions['tag'] = args.tag.removeprefix('refs/tags/').removeprefix('v')
    changelog = (ROOT / 'CHANGELOG.md').read_text()
    if f'## [{versions["Cargo.toml"]}]' not in changelog:
        versions['CHANGELOG.md'] = 'missing entry'
    if len(set(versions.values())) != 1:
        print('Version mismatch:', ', '.join(f'{k}={v}' for k, v in versions.items()))
        return 1
    print('Version', versions['Cargo.toml'], 'consistent across', ', '.join(versions))
    return 0


if __name__ == '__main__':
    sys.exit(main())
