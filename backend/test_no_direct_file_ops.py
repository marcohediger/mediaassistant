"""Guard: no destructive file operation outside fileops.py.

Rebuild "Module & Datensicherheit", principle 1 (docs/UMBAU_MODULE.md): every
move, delete, overwrite, download, upload, replace and metadata write — local
and in Immich — goes through fileops.py, which writes the ledger (file_ops),
keeps the copy register (file_copies) and trashes instead of deleting. This
test reads every module under backend/ and reports each direct use of a
destructive primitive with file:line, so a new one cannot slip in unnoticed.

    docker exec mediaassistant-dev python /app/test_no_direct_file_ops.py
    python3 backend/test_no_direct_file_ops.py          (works outside the container too)

Exit code 0 = nothing outside the allowlist, 1 = findings.

What counts (references, not only calls — ``asyncio.to_thread(os.remove, p)``
passes the function without calling it):

- os.remove / os.unlink / os.rename / os.renames / os.replace, Path.unlink
  (any ``.unlink``), shutil.move / shutil.rmtree;
- safe_move, safe_remove, safe_remove_with_log (safe_file.py,
  file_operations.py);
- Immich: delete_asset, safe_upload_asset, safe_replace_asset, download_asset
  (writes with "wb" into whatever directory it is given — R-03), and
  upload_asset / trash_asset / restore_asset / lock_asset when they come from
  immich_client (fileops has functions of the same name that are the
  sanctioned way);
- a raw HTTP DELETE on Immich's ``/api/assets`` past immich_client;
- exiftool ``-overwrite_original`` / ``-overwrite_original_in_place`` — except
  inside a function handed to ``fileops.write_metadata`` as its writer: that
  one edits fileops' private copy, which is then swapped in atomically.

Not scanned: tests (test_*.py, test_support/), fileops.py itself, and the
primitives it builds on (immich_client.py, safe_file.py).

Justified exceptions live in ALLOWLIST below, each with its reason. An entry
that no longer matches anything is reported as stale (not a failure) so the
list shrinks with the migration.
"""
import ast
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))

SKIP_DIRS = {"__pycache__", "test_support", "static", "templates", "i18n"}
SKIP_FILES = {
    "fileops.py",       # the one sanctioned place
    "immich_client.py",  # the Immich HTTP primitives fileops calls
    "safe_file.py",      # the copy-verify-delete primitive (safe_move)
}

FS_PRIMITIVES = {
    ("os", "remove"), ("os", "unlink"), ("os", "rename"), ("os", "renames"), ("os", "replace"),
    ("shutil", "move"), ("shutil", "rmtree"),
}
# Destructive wherever they come from: no fileops function has these names.
UNIQUE_NAMES = {
    "safe_move", "safe_remove", "safe_remove_with_log",
    "delete_asset", "safe_upload_asset", "safe_replace_asset", "download_asset",
}
# Names fileops reuses for its own, sanctioned functions: flagged only when
# they resolve to immich_client.
IMMICH_ONLY = {"upload_asset", "trash_asset", "restore_asset", "lock_asset"}
OVERWRITE_FLAG = "-overwrite_original"

# ---------------------------------------------------------------------------
# Allowlist: (file relative to backend/, enclosing function or "*" for the
# whole file, what or "*", why). Keep every entry justified; remove it when
# the code it excuses is gone.
# ---------------------------------------------------------------------------
ALLOWLIST = [
    # The legacy primitives themselves. Every *caller* of them is reported;
    # the definitions go once no caller is left.
    ("file_operations.py", "safe_remove", "os.remove",
     "Definition der Alt-Primitive; ihre Aufrufer meldet dieser Test einzeln"),
    ("file_operations.py", "safe_remove_with_log", "safe_remove",
     "Definition der Alt-Primitive; ihre Aufrufer meldet dieser Test einzeln"),

    # Dev-only helper in backend/ (inventory I7): run by hand on dev, deletes
    # only the Immich assets and albums of MANUAL-/FTAG- test jobs; never
    # imported by the app. Named file by file, so a new helper is reported
    # until someone decides about it.
    ("_clean_test_immich.py", "main", "HTTP DELETE /api/assets",
     "Dev-Helfer, löscht nur Test-Assets (MANUAL-/FTAG-), nie von der App importiert"),
]


class Finding:
    def __init__(self, path: str, line: int, what: str, func: str, text: str):
        self.path, self.line, self.what, self.func, self.text = path, line, what, func, text
        self.allowed_by = None

    def __str__(self) -> str:
        return f"{self.path}:{self.line}  {self.what}  (in {self.func})  {self.text}"


class _Imports(ast.NodeVisitor):
    """Module aliases (``import os as o``) and imported names
    (``from os import remove as rm``) anywhere in the file."""

    def __init__(self):
        self.modules: dict[str, str] = {}
        self.names: dict[str, tuple[str, str]] = {}

    def visit_Import(self, node):
        for a in node.names:
            self.modules[a.asname or a.name.split(".")[0]] = a.name

    def visit_ImportFrom(self, node):
        mod = (node.module or "").split(".")[-1]
        for a in node.names:
            self.names[a.asname or a.name] = (mod, a.name)


def _writer_spans(tree: ast.AST) -> list[tuple[int, int]]:
    """Line ranges of functions passed to write_metadata as the writer."""
    writer_names: set[str] = set()
    spans: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
        if name != "write_metadata":
            continue
        arg = node.args[2] if len(node.args) >= 3 else next(
            (k.value for k in node.keywords if k.arg == "writer"), None)
        if isinstance(arg, ast.Call) and getattr(arg.func, "id", getattr(arg.func, "attr", "")) == "partial" \
                and arg.args:
            arg = arg.args[0]
        if isinstance(arg, ast.Name):
            writer_names.add(arg.id)
        elif isinstance(arg, ast.Attribute):
            writer_names.add(arg.attr)
        elif isinstance(arg, ast.Lambda):
            spans.append((arg.lineno, arg.end_lineno or arg.lineno))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in writer_names:
            spans.append((node.lineno, node.end_lineno or node.lineno))
    return spans


class _Scanner(ast.NodeVisitor):
    def __init__(self, rel: str, source_lines: list[str], imports: _Imports, writer_spans):
        self.rel = rel
        self.lines = source_lines
        self.imp = imports
        self.writer_spans = writer_spans
        self.stack: list[str] = []
        self.findings: list[Finding] = []

    # -- scope ------------------------------------------------------------
    def _scoped(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = _scoped
    visit_AsyncFunctionDef = _scoped
    visit_ClassDef = _scoped

    def _add(self, node, what: str):
        text = self.lines[node.lineno - 1].strip() if node.lineno - 1 < len(self.lines) else ""
        self.findings.append(Finding(self.rel, node.lineno, what,
                                     ".".join(self.stack) or "<module>", text[:110]))

    # -- references -------------------------------------------------------
    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Load):
            mod, attr = self.imp.names.get(node.id, (None, node.id))
            if (mod, attr) in FS_PRIMITIVES:
                self._add(node, f"{mod}.{attr}")
            elif attr in UNIQUE_NAMES:
                self._add(node, f"{mod}.{attr}" if mod else attr)
            elif mod == "immich_client" and attr in IMMICH_ONLY:
                self._add(node, f"immich_client.{attr}")
        self.generic_visit(node)

    def visit_Attribute(self, node):
        if isinstance(node.ctx, ast.Load):
            base = self.imp.modules.get(node.value.id) if isinstance(node.value, ast.Name) else None
            base = (base or "").split(".")[-1] or None
            if node.attr == "unlink":
                self._add(node, "os.unlink" if base == "os" else "Path.unlink")
            elif base and (base, node.attr) in FS_PRIMITIVES:
                self._add(node, f"{base}.{node.attr}")
            elif node.attr in UNIQUE_NAMES:
                self._add(node, f"{base}.{node.attr}" if base else node.attr)
            elif base == "immich_client" and node.attr in IMMICH_ONLY:
                self._add(node, f"immich_client.{node.attr}")
        self.generic_visit(node)

    def visit_Call(self, node):
        # A raw HTTP DELETE on Immich's asset endpoint, past immich_client.
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else None
        args = list(node.args) + [k.value for k in node.keywords]
        is_delete = name == "delete" or (
            name == "request" and node.args and isinstance(node.args[0], ast.Constant)
            and str(node.args[0].value).upper() == "DELETE")
        if is_delete and any(_mentions(a, "/assets") for a in args):
            self._add(node, "HTTP DELETE /api/assets")
        self.generic_visit(node)

    def visit_Constant(self, node):
        if isinstance(node.value, str) and OVERWRITE_FLAG in node.value:
            inside_writer = any(a <= node.lineno <= b for a, b in self.writer_spans)
            if not inside_writer:
                self._add(node, "exiftool " + node.value.strip().split()[0][:40])


def _mentions(node, needle: str) -> bool:
    """A string or f-string argument that contains `needle` in its literal parts."""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str) and needle in node.value
    if isinstance(node, ast.JoinedStr):
        return any(isinstance(v, ast.Constant) and needle in str(v.value) for v in node.values)
    return False


def _files():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(filenames):
            if not name.endswith(".py") or name.startswith("test_"):
                continue
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, ROOT)
            if rel in SKIP_FILES:
                continue
            yield path, rel


def scan() -> tuple[list[Finding], list[str]]:
    findings: list[Finding] = []
    errors: list[str] = []
    for path, rel in _files():
        try:
            with open(path, encoding="utf-8") as f:
                source = f.read()
            tree = ast.parse(source, filename=rel)
        except (SyntaxError, UnicodeDecodeError) as exc:
            errors.append(f"{rel}: nicht lesbar ({type(exc).__name__}: {exc})")
            continue
        imports = _Imports()
        imports.visit(tree)
        scanner = _Scanner(rel, source.splitlines(), imports, _writer_spans(tree))
        scanner.visit(tree)
        findings.extend(scanner.findings)
    return findings, errors


def _match(entry, f: Finding) -> bool:
    rel, func, what, _why = entry
    return (rel == f.path and func in ("*", f.func) and what in ("*", f.what))


def main() -> int:
    findings, errors = scan()
    used = set()
    for f in findings:
        for i, entry in enumerate(ALLOWLIST):
            if _match(entry, f):
                f.allowed_by = entry[3]
                used.add(i)
                break
    open_ = [f for f in findings if f.allowed_by is None]
    allowed = [f for f in findings if f.allowed_by is not None]

    print("=" * 94)
    print("  Direkte Dateioperationen ausserhalb von fileops.py")
    print("=" * 94)
    by_file: dict[str, list[Finding]] = {}
    for f in open_:
        by_file.setdefault(f.path, []).append(f)
    for rel in sorted(by_file):
        print(f"\n  {rel} ({len(by_file[rel])})")
        for f in by_file[rel]:
            print(f"    {f.path}:{f.line:<5} {f.what:<34} in {f.func}")
            print(f"           {f.text}")
    if errors:
        print("\n  Nicht lesbar:")
        for e in errors:
            print(f"    {e}")

    stale = [ALLOWLIST[i] for i in range(len(ALLOWLIST)) if i not in used]
    if stale:
        print("\n  Veraltete Ausnahmen (passen auf nichts mehr — entfernen):")
        for rel, func, what, _why in stale:
            print(f"    {rel} {func} {what}")

    print("\n" + "-" * 94)
    print(f"  {len(open_)} Fundstellen in {len(by_file)} Dateien, {len(allowed)} begründete Ausnahmen"
          + (f", {len(errors)} Dateien nicht lesbar" if errors else ""))
    print("  " + ("GRUEN" if not open_ and not errors else "ROT"))
    return 1 if open_ or errors else 0


if __name__ == "__main__":
    sys.exit(main())
