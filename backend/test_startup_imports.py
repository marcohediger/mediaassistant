"""The cheapest possible guard against shipping an app that cannot start.

v2.32.22 went out with an empty `version.py` and the container crash-looped
on `ImportError: cannot import name 'VERSION'`. Every unit test passed —
none of them imported the startup path. This one does nothing else.

    docker exec mediaassistant-dev python /app/test_startup_imports.py
"""
import sys


def main() -> int:
    failed = []

    def check(label, fn):
        try:
            fn()
            print(f"  ok    {label}")
        except Exception as e:
            failed.append(label)
            print(f"  FAIL  {label}: {type(e).__name__}: {e}")

    def version():
        from version import VERSION, VERSION_DATE
        assert VERSION and VERSION.count(".") == 2, f"unplausible Version: {VERSION!r}"
        assert VERSION_DATE, "VERSION_DATE fehlt"

    def app():
        import main
        assert main.app.routes, "keine Routen registriert"

    def pipeline():
        from pipeline import run_pipeline  # noqa: F401

    def watcher():
        from filewatcher import start_filewatcher  # noqa: F401

    check("version.py liefert VERSION und VERSION_DATE", version)
    check("main.py importiert und registriert Routen", app)
    check("pipeline importiert", pipeline)
    check("filewatcher importiert", watcher)

    print(f"\n{4 - len(failed)}/4 bestanden")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
