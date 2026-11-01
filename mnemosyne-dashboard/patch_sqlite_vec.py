#!/usr/bin/env python3
"""Patch dashboard_core.py in mnemosyne-dashboard to load sqlite-vec extension.

Ensures that sqlite3 connections enable extension loading and bind sqlite_vec,
allowing vec0 virtual tables (vec_episodes, vec_facts, vec_working) to query
without encountering 'no such module: vec0'.
"""
from __future__ import annotations

import sys
from pathlib import Path

TARGET = Path("/app/dashboard_core.py")
if not TARGET.exists():
    TARGET = Path("dashboard_core.py")

if not TARGET.exists():
    print(f"Error: {TARGET} not found", file=sys.stderr)
    sys.exit(1)

content = TARGET.read_text(encoding="utf-8")

MARKER = "# odin-patch: bind sqlite-vec extension"
if MARKER in content:
    print("dashboard_core.py already patched with sqlite-vec binding; skipping.")
    sys.exit(0)

# 1. Ensure sqlite_vec import and module-level sqlite3.connect hook
import_block = f"""{MARKER}
try:
    import sqlite_vec
except ImportError:
    sqlite_vec = None

if sqlite_vec is not None:
    import sqlite3 as _sqlite3_mod
    _orig_sqlite_connect = _sqlite3_mod.connect

    def _vec_sqlite_connect(*args, **kwargs):
        _con = _orig_sqlite_connect(*args, **kwargs)
        try:
            _con.enable_load_extension(True)
            sqlite_vec.load(_con)
        except Exception:
            pass
        return _con

    _sqlite3_mod.connect = _vec_sqlite_connect
"""

if "import sqlite3" in content:
    content = content.replace("import sqlite3", f"import sqlite3\n{import_block}", 1)
else:
    content = f"{import_block}\n{content}"

# 2. Patch connect()
old_connect = """        con = sqlite3.connect(uri, uri=True, timeout=10)
        con.row_factory = sqlite3.Row
        con.create_function("REGEXP", 2, self._regexp)
        return con"""

new_connect = """        con = sqlite3.connect(uri, uri=True, timeout=10)
        con.row_factory = sqlite3.Row
        con.create_function("REGEXP", 2, self._regexp)
        if sqlite_vec is not None:
            try:
                con.enable_load_extension(True)
                sqlite_vec.load(con)
            except Exception:
                pass
        return con"""

# 3. Patch connect_rw()
old_connect_rw = """        con = sqlite3.connect(str(self.db_path), timeout=10)
        con.row_factory = sqlite3.Row
        con.create_function("REGEXP", 2, self._regexp)
        con.execute("PRAGMA busy_timeout=5000")
        return con"""

new_connect_rw = """        con = sqlite3.connect(str(self.db_path), timeout=10)
        con.row_factory = sqlite3.Row
        con.create_function("REGEXP", 2, self._regexp)
        con.execute("PRAGMA busy_timeout=5000")
        if sqlite_vec is not None:
            try:
                con.enable_load_extension(True)
                sqlite_vec.load(con)
            except Exception:
                pass
        return con"""

if old_connect not in content:
    print("Error: could not find target connect() block in dashboard_core.py", file=sys.stderr)
    sys.exit(1)

if old_connect_rw not in content:
    print("Error: could not find target connect_rw() block in dashboard_core.py", file=sys.stderr)
    sys.exit(1)

content = content.replace(old_connect, new_connect, 1)
content = content.replace(old_connect_rw, new_connect_rw, 1)

TARGET.write_text(content, encoding="utf-8")
print(f"Successfully patched {TARGET} to bind sqlite-vec extension.")
