"""Guard: nothing writes jobs.step_result except runs.py.

Rebuild "Module & Datensicherheit", data model (docs/UMBAU_MODULE.md):
module results live in ``module_runs``; ``jobs.step_result`` is the derived
view ("current finished run per module" plus the job values), written only by
runs.py in the same transaction as the run. A direct write would show a
result no run stands for, and the next run would not know about it. Tests
seed results through ``test_support.seed.seed_results`` (runs.apply).

This test reads every module under backend/, tests and test_support
included, and reports each write to step_result with file:line, so a new one
cannot slip in unnoticed.

    docker exec mediaassistant-dev python /app/test_no_direct_step_result.py
    python3 backend/test_no_direct_step_result.py       (works outside the container too)

Exit code 0 = nothing outside the allowlist, 1 = findings.

What counts as a write:

- assigning or deleting the attribute: ``x.step_result = …`` (also ``+=``,
  annotated, tuple targets), ``del x.step_result``, ``setattr(x, "step_result", …)``;
- changing the dict in place: ``x.step_result[k] = …`` (nested too), ``del
  x.step_result[k]``, ``x.step_result.update/setdefault/pop/popitem/clear(…)``
  — even unsaved, the object then shows a view the runs do not give;
- ``flag_modified(x, "step_result")``;
- ``Job(step_result=…)``;
- Core statements: ``.values(step_result=…)``, a dict with the key
  ``"step_result"`` or ``Job.step_result`` passed to ``values`` / ``execute``
  / ``bulk_*_mappings``;
- SQL text: ``UPDATE jobs … step_result =`` and ``INSERT INTO jobs (… step_result …)``.

Not a write: reading, a local variable or parameter named step_result,
``SimpleNamespace(step_result=…)`` (a stand-in object, never stored).

Not scanned: runs.py (the writer). Justified exceptions live in ALLOWLIST
below, each with its reason. An entry that no longer matches anything is
reported as stale (not a failure) so the list shrinks.
"""
import ast
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))

SKIP_DIRS = {"__pycache__", "static", "templates", "i18n"}
SKIP_FILES = {
    "runs.py",   # the one writer
}
FIELD = "step_result"
MUTATORS = {"update", "setdefault", "pop", "popitem", "clear"}
STATEMENT_CALLS = {"values", "execute", "executemany", "bulk_update_mappings", "bulk_insert_mappings"}
SQL_WRITE = [
    re.compile(r"\bUPDATE\s+jobs\b[\s\S]*\bstep_result\s*=", re.IGNORECASE),
    re.compile(r"\bINSERT\s+(?:OR\s+\w+\s+)?INTO\s+jobs\b[\s\S]*\bstep_result\b", re.IGNORECASE),
]

# ---------------------------------------------------------------------------
# Allowlist: (file relative to backend/, enclosing function or "*" for the
# whole file, what or "*", why). Keep every entry justified; remove it when
# the code it excuses is gone.
# ---------------------------------------------------------------------------
ALLOWLIST = [
    ("database.py", "_migrate_module_runs", "SQL UPDATE jobs",
     "Einmalige Übernahme beim Start: legt aus dem alten step_result Läufe an und schreibt die Sicht "
     "ohne verworfene Laufparameter, bevor runs.py übernimmt"),

    # A synthetic database in the shape of September 2026 (before module_runs):
    # what the startup migration is tested on. Never the app's database.
    ("test_module_runs.py", "scenario_legacy", "SQL INSERT INTO jobs",
     "Synthetische Alt-Datenbank (vor module_runs) als Eingabe der Migration, nie die App-Datenbank"),

    # Unsaved Job objects handed to pure functions (never added to a session).
    ("test_v29_merge.py", "_job", "Job(step_result=…)",
     "Job nur im Speicher; _merge und folder_targets lesen ihn nur (siehe Docstring der Datei)"),
    ("test_v29_merge.py", "test_folder_record", "Job(step_result=…)",
     "Job nur im Speicher, Eingabe der reinen Funktion _folder_record"),
    ("test_v29_merge.py", "test_quality_score", "Job(step_result=…)",
     "Job nur im Speicher, Eingabe der reinen Funktion _quality_score"),
    ("test_shared_functions.py", "main", "Job(step_result=…)",
     "Jobs nur im Speicher, Eingabe reiner Funktionen (resolve_filepath, _quality_score); "
     "der Test schreibt keine Jobs"),
]


class Finding:
    def __init__(self, path: str, line: int, what: str, func: str, text: str):
        self.path, self.line, self.what, self.func, self.text = path, line, what, func, text
        self.allowed_by = None

    def __str__(self) -> str:
        return f"{self.path}:{self.line}  {self.what}  (in {self.func})  {self.text}"


def _is_field(node) -> bool:
    """``x.step_result`` itself."""
    return isinstance(node, ast.Attribute) and node.attr == FIELD


def _reaches_field(node) -> bool:
    """``x.step_result`` or something inside it: ``x.step_result[a][b]``."""
    while isinstance(node, ast.Subscript):
        node = node.value
    return _is_field(node)


def _callee(node: ast.Call) -> str | None:
    fn = node.func
    return fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)


def _is_field_key(key) -> bool:
    return (isinstance(key, ast.Constant) and key.value == FIELD) or _is_field(key)


def _string_parts(node) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(str(v.value) if isinstance(v, ast.Constant) else " ? " for v in node.values)
    return None


class _Scanner(ast.NodeVisitor):
    def __init__(self, rel: str, source_lines: list[str]):
        self.rel = rel
        self.lines = source_lines
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

    # -- assignments and deletes -----------------------------------------
    def _targets(self, node, targets):
        for t in targets:
            for el in (t.elts if isinstance(t, (ast.Tuple, ast.List)) else [t]):
                if isinstance(el, ast.Starred):
                    el = el.value
                if _is_field(el):
                    self._add(node, "x.step_result =")
                elif isinstance(el, ast.Subscript) and _reaches_field(el):
                    self._add(node, "x.step_result[…] =")

    def visit_Assign(self, node):
        self._targets(node, node.targets)
        self.generic_visit(node)

    def visit_AugAssign(self, node):
        self._targets(node, [node.target])
        self.generic_visit(node)

    def visit_AnnAssign(self, node):
        if node.value is not None:
            self._targets(node, [node.target])
        self.generic_visit(node)

    def visit_Delete(self, node):
        for t in node.targets:
            if _is_field(t):
                self._add(node, "del x.step_result")
            elif isinstance(t, ast.Subscript) and _reaches_field(t):
                self._add(node, "del x.step_result[…]")
        self.generic_visit(node)

    # -- calls ------------------------------------------------------------
    def visit_Call(self, node):
        name = _callee(node)
        fn = node.func
        if isinstance(fn, ast.Attribute) and fn.attr in MUTATORS and _reaches_field(fn.value):
            self._add(node, f"x.step_result.{fn.attr}(…)")
        if name == "flag_modified" and any(isinstance(a, ast.Constant) and a.value == FIELD for a in node.args):
            self._add(node, "flag_modified(…, 'step_result')")
        if name == "setattr" and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) \
                and node.args[1].value == FIELD:
            self._add(node, "setattr(…, 'step_result', …)")
        has_kw = any(k.arg == FIELD for k in node.keywords)
        if has_kw and name == "Job":
            self._add(node, "Job(step_result=…)")
        elif has_kw and name == "values":
            self._add(node, ".values(step_result=…)")
        if name in STATEMENT_CALLS:
            for arg in node.args:
                items = arg.elts if isinstance(arg, (ast.List, ast.Tuple)) else [arg]
                if any(isinstance(d, ast.Dict) and any(_is_field_key(k) for k in d.keys) for d in items):
                    self._add(node, f".{name}({{'step_result': …}})")
                    break
        self.generic_visit(node)

    # -- SQL text ---------------------------------------------------------
    def _sql(self, node):
        text = _string_parts(node)
        if text and FIELD in text:
            for rx, label in zip(SQL_WRITE, ("SQL UPDATE jobs", "SQL INSERT INTO jobs")):
                if rx.search(text):
                    self._add(node, label)
                    break

    def visit_Expr(self, node):
        if _string_parts(node.value) is None:   # a docstring is not SQL
            self.generic_visit(node)

    def visit_Constant(self, node):
        self._sql(node)

    def visit_JoinedStr(self, node):
        self._sql(node)   # the literal parts as one text; do not descend into them again


def _files():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(filenames):
            if not name.endswith(".py"):
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
        scanner = _Scanner(rel, source.splitlines())
        scanner.visit(tree)
        findings.extend(scanner.findings)
    return findings, errors


def _match(entry, f: Finding) -> bool:
    rel, func, what, _why = entry
    return rel == f.path and func in ("*", f.func) and what in ("*", f.what)


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
    print("  Direkte Schreibzugriffe auf jobs.step_result ausserhalb von runs.py")
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
