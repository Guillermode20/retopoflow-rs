"""Native reinstall regression; runs without Blender. Build native lib first."""
import ctypes
import importlib.util
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
LOADER = ROOT / 'blender' / 'retopoflow_rs' / 'native_loader.py'
spec = importlib.util.spec_from_file_location('retopo_native_loader', LOADER)
loader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(loader)
FILENAME = ('retopoflow_core.dll' if sys.platform == 'win32' else
            'libretopoflow_core.dylib' if sys.platform == 'darwin' else 'libretopoflow_core.so')
BINARY = ROOT / 'blender' / 'retopoflow_rs' / 'native' / FILENAME
SOURCE = b'{"vertices":[[0,0,0],[1,0,0],[0,1,0]],"triangles":[[0,1,2]]}'


class NativeReloadTests(unittest.TestCase):
    def configured(self, path):
        lib = loader.load_runtime_copy(path)
        lib.rf_source_create.argtypes = [ctypes.c_char_p]
        lib.rf_source_create.restype = ctypes.c_uint64
        lib.rf_source_free.argtypes = [ctypes.c_uint64]
        lib.rf_source_free.restype = None
        return lib

    def test_installed_binary_can_be_overwritten_without_touching_loaded_code(self):
        with tempfile.TemporaryDirectory() as directory:
            installed = Path(directory) / FILENAME
            shutil.copy2(BINARY, installed)
            lib = self.configured(installed)
            loaded = Path(lib._name)
            self.assertFalse(loaded.samefile(installed))
            handle = lib.rf_source_create(SOURCE)
            self.assertNotEqual(handle, 0)
            lib.rf_source_free(handle)
            # Simulate destructive ZIP extraction over the installed file. The
            # old direct-CDLL loader mapped this same inode and could crash.
            installed.write_bytes(b'\0' * 4096)
            self.assertNotEqual(loaded.read_bytes(), installed.read_bytes())
            for _ in range(20):
                handle = lib.rf_source_create(SOURCE)
                self.assertNotEqual(handle, 0)
                lib.rf_source_free(handle)
            # Reinstallation/reloading obtains a new independent loader path.
            shutil.copy2(BINARY, installed)
            newer = self.configured(installed)
            self.assertNotEqual(lib._name, newer._name)
            handle = newer.rf_source_create(SOURCE)
            self.assertNotEqual(handle, 0)
            newer.rf_source_free(handle)
            # Both existing and new copies remain callable.
            handle = lib.rf_source_create(SOURCE)
            self.assertNotEqual(handle, 0)
            lib.rf_source_free(handle)

    def test_failed_load_does_not_hide_loader_error(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(OSError):
                loader.load_runtime_copy(Path(directory) / FILENAME)


if __name__ == '__main__':
    unittest.main()
