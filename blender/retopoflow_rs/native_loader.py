"""Load a private immutable copy, never the reinstallable add-on binary.

On Linux, dlopen mappings refer to the original file's pages. Truncating or
replacing its contents during add-on installation can corrupt running code and
relocation tables. A new unique path also prevents dlopen reusing a stale handle
when Python modules are reloaded. Keep runtime copies until process exit.
"""
import atexit
import ctypes
import shutil
import tempfile
from pathlib import Path


def load_runtime_copy(source):
    source = Path(source)
    directory = Path(tempfile.mkdtemp(prefix='retopoflow-native-'))
    destination = directory / source.name
    try:
        shutil.copy2(source, destination)
        library = ctypes.CDLL(str(destination))
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    # Do not clean up on addon unregister/reload: functions/handles may still
    # reference this mapping. Windows may retain the DLL until process teardown.
    atexit.register(shutil.rmtree, directory, ignore_errors=True)
    library._retopo_runtime_directory = directory
    return library
