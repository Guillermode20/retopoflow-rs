# Native loading and add-on updates

Never load the installable native library directly with `ctypes.CDLL`. Blender's ZIP installer can overwrite that file while its code is mapped into the running process. Python exceptions cannot catch the resulting native segmentation fault / bus error.

The loader now copies the binary into a unique process-runtime directory before loading it. The runtime file is independent of the installation inode, is not overwritten on reinstall, and stays available until process exit. A reload obtains a new unique path rather than depending on dlopen's path cache. Restarting Blender after updates is still recommended, especially when updating from versions that used the unsafe direct loader.

## Evidence for the reported startup crash

- The crash dump located the fault at a null native call from `rf_source_create`, entered through libffi, not a Python draw callback.
- The mapped library path was the add-on's directly installed binary.
- The operation log showed repeated ZIP installations in the same Blender process.
- In an isolated child process, loading a directly installed copy and then truncating/overwriting that file reproduced a fatal signal. No Blender scene was involved.
- The private-copy regression test overwrites the installation file, successfully makes 20 further source-create/free calls, reloads a second independent copy and verifies both remain callable.

This identifies a concrete unsafe update path and provides a targeted regression test; it is not proof that every possible live-viewport crash is fixed.

```sh
python3 scripts/build_addon.py
python3 tests/test_native_reload.py
```

The regression test modifies only temporary copies, never the bundled or user-installed binary.
