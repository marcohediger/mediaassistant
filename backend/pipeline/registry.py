"""The module register: what each pipeline module waits for, reads, needs
and may do (docs/UMBAU_MODULE_AUSFUEHRUNG.md, sections 1 and 2.7).

Metadata only. The step functions themselves are taken from
``pipeline.MAIN_STEPS`` / ``pipeline.FINALIZERS`` at run time (tests swap
them), never from here.

Edges
- ``requires``: must be finished before the module starts (order).
- ``reads``: strong data edge — a change of these fields of the predecessor
  makes the module stale, and a rerun cascades along it.
- ``hints``: context for the AI only — a rerun follows it on request.
- IA-04 is ``ephemeral``: IA-10 removes its temp files, so IA-05 / IA-06
  need it again whenever they run again.

``fingerprint()`` hashes what a module read when it started (predecessor
fields, the content hash for modules that read the file, the settings it
depends on); ``runs.start`` stores it in ``module_runs.inputs_fingerprint``.
A finished run whose fingerprint differs from the one its inputs give now
is *stale*. NULL (migrated, copied, recorded) means unknown, never stale.
"""
import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Callable

MAIN = ("IA-01", "IA-02", "IA-03", "IA-04", "IA-05", "IA-06", "IA-07", "IA-08")
FINALIZER_CODES = ("IA-09", "IA-10", "IA-11")
ORDER = MAIN + FINALIZER_CODES
# Failures here fail a pipeline run (elsewhere they only warn).
CRITICAL = frozenset({"IA-01", "IA-07", "IA-08"})
RESOURCES = ("cpu", "ai", "geo", "immich", "io", "none")

_GEO = ("country", "state", "city", "suburb")
_FOLDER = ("folder_path", "donor_folder_paths", "folder_tags", "own_album", "donor_albums")
_IA01_CONTEXT = ("width", "height", "has_exif", "make", "model", "date", "gps", "gps_lat", "gps_lon",
                 "software", "file_type", "mime_type", "duration")


def _always(job, options) -> bool:
    return True


def _never(job, options) -> bool:
    return False


def _ia07_needs_file(job, options) -> bool:
    """IA-07 writes into the file — except an Immich job in API mode, whose
    keywords and description go to Immich through IA-08 only."""
    return not (getattr(job, "use_immich", False) and (options or {}).get("immich", "api") == "api")


def _ia08_resource(job, snap=None) -> str:
    return "immich" if getattr(job, "use_immich", False) else "io"


def _ia06_resource(job, snap=None) -> str:
    """OCR switched off in the set: IA-06 asks no AI, so it waits for no AI slot."""
    s = (snap or {}).get("set") or {}
    return "none" if s and not s.get("ocr_enabled") else "ai"


@dataclass(frozen=True)
class ModuleSpec:
    code: str
    requires: tuple
    reads: dict = field(default_factory=dict)       # strong: predecessor -> fields read
    hints: dict = field(default_factory=dict)       # weak: AI context only
    resource: object = "cpu"                        # class name, or callable(job) -> class name
    critical: bool = False
    destructive: bool = False
    standalone: bool = True
    needs_file: Callable = _always                  # (job, options) -> bool
    ephemeral: bool = False
    side_session: bool = False                      # may run next to the main path (IA-03)
    fp_version: int = 1
    reads_file: bool = False                        # the fingerprint includes job.file_hash

    def resource_for(self, job, snap: dict | None = None) -> str:
        return self.resource(job, snap) if callable(self.resource) else self.resource


SPECS: dict[str, ModuleSpec] = {s.code: s for s in (
    ModuleSpec("IA-01", (), critical=True, reads_file=True),
    ModuleSpec("IA-02", ("IA-01",), destructive=True, standalone=False, reads_file=True),
    ModuleSpec("IA-03", ("IA-02",), reads={"IA-01": ("gps_lat", "gps_lon")}, resource="geo",
               needs_file=_never, side_session=True),
    ModuleSpec("IA-04", ("IA-02",), reads={"IA-01": ("duration",)}, standalone=False, ephemeral=True,
               reads_file=True),
    ModuleSpec("IA-05", ("IA-03", "IA-04"), reads={"IA-04": ("converted",)},
               hints={"IA-01": _IA01_CONTEXT, "IA-03": _GEO}, resource="ai", reads_file=True),
    ModuleSpec("IA-06", ("IA-05",), reads={"IA-05": ("type", "source", "parse_error"), "IA-04": ("converted",)},
               resource=_ia06_resource, reads_file=True),
    ModuleSpec("IA-07", ("IA-06",), reads={
        "IA-01": ("file_type", "google_json", "google_json_path", "google_json_description"),
        "IA-02": _FOLDER, "IA-03": _GEO,
        "IA-05": ("type", "tags", "source", "quality", "description"),
        "IA-06": ("has_text", "text")},
        critical=True, destructive=True, needs_file=_ia07_needs_file, reads_file=True),
    # IA-08 also reads IA-07's file_hash: a rerun that rewrote the file
    # (direct mode, or the work copy of an Immich job in "replace" mode)
    # must reach IA-08 even when the keywords stayed the same.
    ModuleSpec("IA-08", ("IA-07",), reads={
        "IA-01": ("file_type", "mime_type", "has_exif", "date", "make", "model"),
        "IA-02": _FOLDER, "IA-03": ("country", "city"), "IA-05": ("type", "nsfw"),
        "IA-07": ("keywords_written", "description_written", "tag_paths_written", "file_hash")},
        resource=_ia08_resource, critical=True, destructive=True, needs_file=_never),
    ModuleSpec("IA-09", MAIN, resource="none", standalone=False, needs_file=_never),
    ModuleSpec("IA-10", MAIN, resource="none", destructive=True, standalone=False, needs_file=_never),
    ModuleSpec("IA-11", MAIN, resource="none", standalone=False, needs_file=_never),
)}


def spec(code: str) -> ModuleSpec:
    return SPECS[code]


def edges(code: str, *, hints: bool = False, ocr_mode: str | None = None) -> dict:
    """Predecessor -> fields `code` depends on (strong reads, plus hints)."""
    s = SPECS[code]
    out = dict(s.reads)
    if code == "IA-06" and ocr_mode is not None and ocr_mode != "smart":
        out.pop("IA-05", None)  # OCR "always" does not look at the AI result
    if hints:
        out.update(s.hints)
    return out


def dependents(code: str, hints: bool = False) -> list[str]:
    """Main modules that read `code` (directly), in pipeline order; with
    hints only the hint readers."""
    out = []
    for other in MAIN:
        s = SPECS[other]
        if code in (s.hints if hints else s.reads):
            out.append(other)
    return out


def standalone(code: str) -> bool:
    return code in SPECS and SPECS[code].standalone


def descriptor(code: str) -> dict:
    """The entry of GET /api/reruns/modules."""
    s = SPECS[code]
    if not s.standalone:
        return {"module": code, "standalone": False, "refused": "not_standalone"}
    resource = s.resource if isinstance(s.resource, str) else {"IA-08": "immich|io", "IA-06": "ai"}.get(code, "cpu")
    return {"module": code, "standalone": True, "needs_file": s.needs_file is not _never,
            "resource": resource, "destructive": s.destructive,
            "dependents": dependents(code), "hint_dependents": dependents(code, hints=True)}


# ---------------------------------------------------------------------------
# Settings and fingerprints
# ---------------------------------------------------------------------------

_CONFIG_KEYS = ("duplikat.phash_threshold", "duplikat.raw_jpg_pair", "geo.provider", "geo.url",
                "video.thumbnail_enabled", "video.thumbnail_frames", "video.thumbnail_scale",
                "ai.prompt", "ai.model", "ai2.model", "ai.image_resize", "ai.image_max_px")
_MODULE_KEYS = ("geocoding", "duplikat_erkennung", "ki_analyse", "ki_analyse_2")


def _json(value):
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


async def global_settings(session) -> dict:
    """The global settings fingerprints depend on, read in one go."""
    from sqlalchemy import select
    from models import Config, Module
    rows = (await session.execute(select(Config.key, Config.value, Config.encrypted)
                                  .where(Config.key.in_(_CONFIG_KEYS)))).all()
    cfg = {k: _json(v) for k, v, enc in rows if not enc}
    mods = dict((await session.execute(select(Module.name, Module.enabled)
                                       .where(Module.name.in_(_MODULE_KEYS)))).all())
    prompt = cfg.get("ai.prompt") or ""
    return {
        "modules": {name: bool(mods.get(name, False)) for name in _MODULE_KEYS},
        "duplikat": {"phash_threshold": cfg.get("duplikat.phash_threshold"),
                     "raw_jpg_pair": cfg.get("duplikat.raw_jpg_pair")},
        "geo": {"provider": cfg.get("geo.provider") or "nominatim",
                "url": cfg.get("geo.url") or "https://nominatim.openstreetmap.org"},
        "video": {"enabled": cfg.get("video.thumbnail_enabled", False),
                  "frames": cfg.get("video.thumbnail_frames", 8), "scale": cfg.get("video.thumbnail_scale", 50)},
        # The prompt as a hash: a changed prompt makes IA-05 stale, the text
        # itself does not belong into every run row.
        "ai": {"prompt": hashlib.sha256(str(prompt).encode("utf-8")).hexdigest()[:16] if prompt else "",
               "models": [cfg.get("ai.model") if mods.get("ki_analyse") else None,
                          cfg.get("ai2.model") if mods.get("ki_analyse_2") else None],
               "resize": bool(cfg.get("ai.image_resize", False)), "max_px": cfg.get("ai.image_max_px", 1024)},
    }


async def set_settings(session, set_id: int | None) -> dict:
    """The settings of one set fingerprints depend on (the set get_set
    resolves, so a job without set gets the first set's)."""
    from sqlalchemy import select
    from models import LibraryCategory, SortingRule
    from sets import get_set
    ps = await get_set(session, set_id)
    if ps is None:
        return {"set": None, "categories": [], "rules": []}
    cats = (await session.execute(select(LibraryCategory).where(LibraryCategory.set_id == ps.id)
                                  .order_by(LibraryCategory.position, LibraryCategory.id))).scalars().all()
    rules = (await session.execute(select(SortingRule).where(SortingRule.set_id == ps.id,
                                                             SortingRule.active == True)  # noqa: E712
                                   .order_by(SortingRule.position, SortingRule.id))).scalars().all()
    return {
        "set": {"id": ps.id, "google_json": bool(ps.google_json),
                "write_mode": ps.metadata_write_mode or "direct",
                "folder_tag_mode": ps.folder_tag_mode or "none", "folder_albums": bool(ps.folder_albums),
                "ocr_enabled": bool(ps.ocr_enabled), "ocr_mode": ps.ocr_mode or "smart",
                "base": ps.library_base_path or "/library"},
        "categories": [[c.key, c.label, c.path_template, bool(c.immich_archive)] for c in cats],
        "rules": [[r.condition, r.value, r.target_category, r.media_type] for r in rules],
    }


async def settings_snapshot(session, job, *, global_cfg: dict | None = None,
                            set_cache: dict | None = None) -> dict:
    """Everything fingerprint() needs besides the job: global settings and
    those of the job's set. `global_cfg` / `set_cache` let a caller that
    handles many jobs read each only once."""
    g = global_cfg if global_cfg is not None else await global_settings(session)
    key = getattr(job, "set_id", None)
    if set_cache is not None and key in set_cache:
        s = set_cache[key]
    else:
        s = await set_settings(session, key)
        if set_cache is not None:
            set_cache[key] = s
    return {**g, **s}


def _job_cfg(code: str, job, snap: dict) -> dict:
    s = snap.get("set") or {}
    if code == "IA-01":
        return {"google_json": s.get("google_json")}
    if code == "IA-02":
        return {"on": snap["modules"]["duplikat_erkennung"], **snap["duplikat"]}
    if code == "IA-03":
        return {"on": snap["modules"]["geocoding"], **snap["geo"]}
    if code == "IA-04":
        return dict(snap["video"])
    if code == "IA-05":
        return {**snap["ai"], "categories": [c[:2] for c in snap["categories"]], "rules": snap["rules"]}
    if code == "IA-06":
        return {"ocr_enabled": s.get("ocr_enabled"), "ocr_mode": s.get("ocr_mode")}
    if code == "IA-07":
        return {"write_mode": s.get("write_mode"), "folder_tag_mode": s.get("folder_tag_mode"),
                "folder_albums": s.get("folder_albums"), "dry_run": bool(getattr(job, "dry_run", False))}
    if code == "IA-08":
        return {"use_immich": bool(getattr(job, "use_immich", False)),
                "immich_user_id": getattr(job, "immich_user_id", None), "base": s.get("base"),
                "categories": snap["categories"], "rules": snap["rules"],
                "folder_tag_mode": s.get("folder_tag_mode"), "folder_albums": s.get("folder_albums"),
                "dry_run": bool(getattr(job, "dry_run", False))}
    return {}


def read_fields(code: str, view: dict, *, ocr_mode: str | None = None, hints: bool = True) -> dict:
    """{predecessor: {field: value}} as `code` reads them from `view`."""
    out = {}
    for up, fields in sorted(edges(code, hints=hints, ocr_mode=ocr_mode).items()):
        res = view.get(up) if isinstance(view.get(up), dict) else {}
        out[up] = {f: res.get(f) for f in fields}
    return out


def fingerprint(code: str, job, view: dict | None, snap: dict | None) -> str | None:
    """sha256 over module, version, content hash (modules reading the file),
    the predecessor fields read and the settings, first 32 hex digits.
    None for the finalizers (they always run) and without settings."""
    if code in FINALIZER_CODES or code not in SPECS or snap is None:
        return None
    s = SPECS[code]
    ocr_mode = (snap.get("set") or {}).get("ocr_mode")
    payload = {"m": code, "v": s.fp_version,
               "file": getattr(job, "file_hash", None) if s.reads_file else None,
               "in": read_fields(code, view or {}, ocr_mode=ocr_mode, hints=True),
               "cfg": _job_cfg(code, job, snap)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:32]


def is_stale(run, job, view: dict, snap: dict) -> bool:
    """A finished run whose stored fingerprint differs from now."""
    from models import RUN_RESULT_STATUSES
    if run is None or run.status not in RUN_RESULT_STATUSES or not run.inputs_fingerprint:
        return False
    now = fingerprint(run.module, job, view, snap)
    return now is not None and now != run.inputs_fingerprint


# ---------------------------------------------------------------------------
# What a rerun of a job reaches
# ---------------------------------------------------------------------------

def reachable(selected, *, hints: bool, present, cascade: str = "auto", ocr_mode: str | None = None) -> set:
    """Modules a rerun of `selected` may run: the selection, everything
    reachable over strong edges (and hint edges with `hints`) among the
    modules that ran before (`present`), and IA-04 as the prerequisite of
    IA-05 / IA-06. With cascade "off" only the selection (and IA-04)."""
    out = set(selected)
    if cascade != "off":
        changed = True
        while changed:
            changed = False
            for code in MAIN:
                if code in out or code not in present or code == "IA-02":
                    continue
                ups = edges(code, hints=hints, ocr_mode=ocr_mode)
                if any(u in out for u in ups):
                    out.add(code)
                    changed = True
    if ("IA-05" in out or "IA-06" in out) and "IA-04" in present:
        out.add("IA-04")
    return out


def native_format(path: str | None) -> bool:
    """IA-04 does not convert these (IA-05 reads the file itself)."""
    return os.path.splitext(path or "")[1].lower() in (".jpg", ".jpeg", ".png", ".webp")
