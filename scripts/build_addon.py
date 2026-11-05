"""Build a host-platform add-on: python scripts/build_addon.py [--output dist]."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('dist'))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    # Explicit target-dir makes artifact discovery independent of Cargo config/env.
    target = root / 'target' / 'addon-build'
    subprocess.run(['cargo', 'build', '--locked', '--release', '--lib', '--target-dir', str(target)],
                   cwd=root, check=True)
    filename = ('retopoflow_core.dll' if sys.platform == 'win32' else
                'libretopoflow_core.dylib' if sys.platform == 'darwin' else
                'libretopoflow_core.so')
    artifact = target / 'release' / filename
    if not artifact.is_file():
        raise SystemExit(f'Missing host library: {artifact}; cross-compilation is not supported')
    addon = root / 'blender' / 'retopoflow_rs'
    native = addon / 'native'
    native.mkdir(parents=True, exist_ok=True)
    shutil.copy2(artifact, native / filename)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    destination = output / f'retopoflow_rs-{sys.platform}.zip'
    with tempfile.TemporaryDirectory() as temporary:
        archive = Path(temporary) / destination.name
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as bundle:
            for path in sorted(addon.rglob('*.py')):
                if '__pycache__' not in path.parts:
                    bundle.write(path, path.relative_to(addon.parent))
            bundle.write(native / filename, f'retopoflow_rs/native/{filename}')
            for name in ('LICENSE', 'REFERENCE_LICENSE.txt', 'README.md'):
                bundle.write(root / name, f'retopoflow_rs/{name}')
            for document in sorted((root / 'docs').glob('*.md')):
                bundle.write(document, f'retopoflow_rs/docs/{document.name}')
        shutil.copy2(archive, destination)
    print(destination)


if __name__ == '__main__':
    main()
