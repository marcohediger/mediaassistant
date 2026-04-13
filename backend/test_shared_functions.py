"""Systematischer Test ALLER shared Functions aus SHARED_FUNCTIONS.md.

Jede Funktion wird mit allen erdenklichen Eingaben getestet:
- None, leerer String, ungültige Typen
- Sehr lange Strings, Sonderzeichen, Unicode
- Nicht-existierende Pfade, gesperrte Dateien
- Ungültige IDs, kaputte Daten
"""
import asyncio, sys, os, time, tempfile, random
sys.path.insert(0, "/app")
os.environ.setdefault("DATABASE_PATH", "/app/data/mediaassistant.db")

PASS, FAIL = [], []
def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅ PASS' if cond else '❌ FAIL'}  {name}" + (f" — {detail}" if detail else ""))

def no_crash(name, fn, *args, **kwargs):
    """Run fn and check it doesn't crash. Returns result."""
    try:
        result = fn(*args, **kwargs)
        check(name, True, f"result={repr(result)[:80]}")
        return result
    except Exception as e:
        check(name, False, f"CRASH: {type(e).__name__}: {e}")
        return None

async def no_crash_async(name, fn, *args, **kwargs):
    """Run async fn and check it doesn't crash."""
    try:
        result = await fn(*args, **kwargs)
        check(name, True, f"result={repr(result)[:80]}")
        return result
    except Exception as e:
        check(name, False, f"CRASH: {type(e).__name__}: {e}")
        return None


async def main():
    print("=" * 60)
    print("  Systematischer Test ALLER shared Functions")
    print("=" * 60)

    # ==================================================================
    # file_operations.py
    # ==================================================================
    from file_operations import (
        sha256, resolve_filename_conflict, safe_remove, safe_remove_with_log,
        get_duplicate_dir, resolve_filepath, sanitize_path_component,
        validate_target_path, parse_date, is_folder_tags_active,
    )
    from models import Job

    # --- sha256 ---
    print("\n── sha256 ──")
    # Normal
    with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
        f.write(b"test data")
        tmp = f.name
    no_crash("sha256(normal)", sha256, tmp)
    os.unlink(tmp)

    # Leere Datei
    with tempfile.NamedTemporaryFile(delete=False) as f:
        tmp_empty = f.name
    r = no_crash("sha256(leer)", sha256, tmp_empty)
    if r:
        check("sha256(leer) != None", r is not None and len(r) == 64)
    os.unlink(tmp_empty)

    # Nicht-existierende Datei → muss crashen (FileNotFoundError erwartet)
    try:
        sha256("/tmp/nonexistent_xyz_test")
        check("sha256(nicht-existent) → Error", False, "kein Error geworfen")
    except (FileNotFoundError, OSError):
        check("sha256(nicht-existent) → Error", True)

    # --- resolve_filename_conflict ---
    print("\n── resolve_filename_conflict ──")
    os.makedirs("/tmp/rfc_test", exist_ok=True)
    no_crash("rfc(normal)", resolve_filename_conflict, "/tmp/rfc_test", "test.jpg")
    no_crash("rfc(leer filename)", resolve_filename_conflict, "/tmp/rfc_test", "")
    no_crash("rfc(kein ext)", resolve_filename_conflict, "/tmp/rfc_test", "noext")
    no_crash("rfc(nur ext)", resolve_filename_conflict, "/tmp/rfc_test", ".jpg")
    no_crash("rfc(doppel ext)", resolve_filename_conflict, "/tmp/rfc_test", "photo.jpg.jpg")
    no_crash("rfc(unicode)", resolve_filename_conflict, "/tmp/rfc_test", "Höhlen_Übersee.jpg")
    no_crash("rfc(emoji)", resolve_filename_conflict, "/tmp/rfc_test", "🏔️.jpg")
    no_crash("rfc(langer name)", resolve_filename_conflict, "/tmp/rfc_test", "A" * 200 + ".jpg")
    no_crash("rfc(separator leer)", resolve_filename_conflict, "/tmp/rfc_test", "test.jpg", "")
    import shutil; shutil.rmtree("/tmp/rfc_test")

    # --- safe_remove ---
    print("\n── safe_remove ──")
    no_crash("safe_remove(None)", safe_remove, None)
    no_crash("safe_remove('')", safe_remove, "")
    no_crash("safe_remove(nicht-existent)", safe_remove, "/tmp/sr_nonexistent")
    no_crash("safe_remove(Verzeichnis)", safe_remove, "/tmp")  # sollte False sein

    with tempfile.NamedTemporaryFile(delete=False) as f:
        tmp_sr = f.name
    r = no_crash("safe_remove(existierend)", safe_remove, tmp_sr)
    check("safe_remove existierend → True", r == True)

    # --- safe_remove_with_log ---
    print("\n── safe_remove_with_log ──")
    no_crash("srl(None)", safe_remove_with_log, None)
    no_crash("srl('')", safe_remove_with_log, "")
    no_crash("srl(immich:xxx)", safe_remove_with_log, "immich:abc-123")
    no_crash("srl(nicht-existent)", safe_remove_with_log, "/tmp/srl_gone")

    # Mit echten Dateien
    open("/tmp/srl_test.jpg", "w").close()
    open("/tmp/srl_test.jpg.log", "w").close()
    r = no_crash("srl(mit .log)", safe_remove_with_log, "/tmp/srl_test.jpg")
    check("srl löschte 2 Dateien", r is not None and len(r) == 2)

    # --- get_duplicate_dir ---
    print("\n── get_duplicate_dir ──")
    r = await no_crash_async("get_duplicate_dir()", get_duplicate_dir)
    if r:
        check("dup_dir existiert", os.path.isdir(r))

    # --- resolve_filepath ---
    print("\n── resolve_filepath ──")
    j = Job(filename="f.jpg", original_path=None, target_path=None, debug_key="RF-1", step_result={})
    no_crash("resolve_filepath(alles None)", resolve_filepath, j)

    j2 = Job(filename="f.jpg", original_path="/nonexistent", target_path="/also_gone",
             debug_key="RF-2", step_result={})
    no_crash("resolve_filepath(nichts existiert)", resolve_filepath, j2)

    j3 = Job(filename="f.jpg", original_path="/tmp", target_path=None,
             debug_key="RF-3", step_result={"IA-04": {"temp_path": "/tmp/also_nope"}})
    no_crash("resolve_filepath(nur Verzeichnis)", resolve_filepath, j3)

    # --- sanitize_path_component ---
    print("\n── sanitize_path_component ──")
    no_crash("sanitize(None)", sanitize_path_component, None)
    no_crash("sanitize('')", sanitize_path_component, "")
    no_crash("sanitize('../../etc')", sanitize_path_component, "../../etc/passwd")
    no_crash("sanitize(null bytes)", sanitize_path_component, "foo\x00bar")
    no_crash("sanitize(nur Punkte)", sanitize_path_component, ".....")
    no_crash("sanitize(unicode)", sanitize_path_component, "Höhlen Übersee 2026")
    no_crash("sanitize(emoji)", sanitize_path_component, "🏔️ Berge")
    no_crash("sanitize(200 chars)", sanitize_path_component, "X" * 200)

    # Verify no dangerous chars
    result = sanitize_path_component("../../etc/passwd")
    check("sanitize: kein ..", ".." not in result)
    check("sanitize: kein /", "/" not in result)

    # --- validate_target_path ---
    print("\n── validate_target_path ──")
    no_crash("validate(normal)", validate_target_path, "/library/photos", "/library")
    no_crash("validate(gleich)", validate_target_path, "/library", "/library")

    try:
        validate_target_path("/etc", "/library")
        check("validate(escape) → ValueError", False)
    except ValueError:
        check("validate(escape) → ValueError", True)

    try:
        validate_target_path("/../../../etc", "/library")
        check("validate(traversal) → ValueError", False)
    except ValueError:
        check("validate(traversal) → ValueError", True)

    # --- parse_date ---
    print("\n── parse_date ──")
    no_crash("parse_date(None)", parse_date, None)
    no_crash("parse_date('')", parse_date, "")
    no_crash("parse_date('Müll')", parse_date, "not a date at all")
    no_crash("parse_date(Zahl)", parse_date, "12345")
    no_crash("parse_date(EXIF)", parse_date, "2024:12:25 14:30:00")
    no_crash("parse_date(ISO)", parse_date, "2024-12-25T14:30:00")
    no_crash("parse_date(TZ+)", parse_date, "2024-12-25T14:30:00+02:00")
    no_crash("parse_date(Z)", parse_date, "2024-12-25T14:30:00Z")
    no_crash("parse_date(.subsec)", parse_date, "2024:12:25 14:30:00.123456")
    no_crash("parse_date(slash)", parse_date, "2024/12/25 14:30:00")
    no_crash("parse_date(nur Datum)", parse_date, "2024:12:25")

    # --- is_folder_tags_active ---
    print("\n── is_folder_tags_active ──")
    j_none = Job(filename="f.jpg", original_path="/inbox/f.jpg",
                 source_inbox_path=None, debug_key="IFTA-1")
    await no_crash_async("is_ft_active(source=None)", is_folder_tags_active, j_none)

    j_ok = Job(filename="f.jpg", original_path="/inbox/Sub/f.jpg",
               source_inbox_path="/inbox", debug_key="IFTA-2")
    await no_crash_async("is_ft_active(normal)", is_folder_tags_active, j_ok)

    # ==================================================================
    # safe_file.py
    # ==================================================================
    print("\n── safe_move ──")
    from safe_file import safe_move

    # Normal case
    src = "/tmp/sm_src.txt"
    dst = "/tmp/sm_dst.txt"
    with open(src, "w") as f:
        f.write("test content for safe_move")
    no_crash("safe_move(normal)", safe_move, src, dst, "test")
    check("safe_move: dst existiert", os.path.exists(dst))
    check("safe_move: src weg", not os.path.exists(src))
    safe_remove(dst)

    # Nicht-existierende Quelle
    try:
        safe_move("/tmp/sm_nonexistent", "/tmp/sm_dst2", "test")
        check("safe_move(nicht-existent) → Error", False)
    except (FileNotFoundError, OSError):
        check("safe_move(nicht-existent) → Error", True)

    # ==================================================================
    # thumbnail_utils.py
    # ==================================================================
    print("\n── thumbnail_utils ──")
    from thumbnail_utils import generate_thumbnail, heic_to_jpeg, video_to_jpeg, raw_to_jpeg

    no_crash("generate_thumbnail(None)", generate_thumbnail, None)
    no_crash("generate_thumbnail(nicht-existent)", generate_thumbnail, "/tmp/nope.jpg")
    no_crash("generate_thumbnail(Verzeichnis)", generate_thumbnail, "/tmp")
    no_crash("generate_thumbnail(0-byte)", generate_thumbnail, "/dev/null")
    no_crash("heic_to_jpeg(nicht-existent)", heic_to_jpeg, "/tmp/nope.heic")
    no_crash("video_to_jpeg(nicht-existent)", video_to_jpeg, "/tmp/nope.mp4")
    no_crash("raw_to_jpeg(nicht-existent)", raw_to_jpeg, "/tmp/nope.dng")

    # Mit echtem JPEG
    from PIL import Image
    img = Image.new("RGB", (100, 100), (255, 0, 0))
    img.save("/tmp/thumb_real.jpg", "JPEG")
    r = no_crash("generate_thumbnail(echtes JPEG)", generate_thumbnail, "/tmp/thumb_real.jpg")
    check("thumbnail bytes > 0", r is not None and len(r) > 0)
    safe_remove("/tmp/thumb_real.jpg")

    # ==================================================================
    # immich_client.py
    # ==================================================================
    print("\n── immich_client ──")
    from immich_client import (
        check_connection, asset_exists, get_asset_info, get_asset_albums,
        get_asset_thumbnail, get_asset_original, get_user_api_key,
    )

    await no_crash_async("check_connection()", check_connection)
    await no_crash_async("asset_exists(None)", asset_exists, None)
    await no_crash_async("asset_exists('')", asset_exists, "")
    await no_crash_async("asset_exists(ungültig)", asset_exists, "not-a-uuid")
    await no_crash_async("asset_exists(zero-uuid)", asset_exists, "00000000-0000-0000-0000-000000000000")
    await no_crash_async("get_asset_info(None)", get_asset_info, None)
    await no_crash_async("get_asset_info('')", get_asset_info, "")
    await no_crash_async("get_asset_info(ungültig)", get_asset_info, "xxx")
    await no_crash_async("get_asset_albums(None)", get_asset_albums, None)
    await no_crash_async("get_asset_albums('')", get_asset_albums, "")
    await no_crash_async("get_asset_albums(ungültig)", get_asset_albums, "xxx")
    await no_crash_async("get_asset_thumbnail(None)", get_asset_thumbnail, None)
    await no_crash_async("get_asset_thumbnail(ungültig)", get_asset_thumbnail, "xxx")
    await no_crash_async("get_asset_original(None)", get_asset_original, None)
    await no_crash_async("get_asset_original(ungültig)", get_asset_original, "xxx")
    await no_crash_async("get_user_api_key(0)", get_user_api_key, 0)
    await no_crash_async("get_user_api_key(99999)", get_user_api_key, 99999)

    # ==================================================================
    # system_logger.py
    # ==================================================================
    print("\n── system_logger ──")
    from system_logger import log_info, log_warning, log_error

    await no_crash_async("log_info(normal)", log_info, "test", "test message")
    await no_crash_async("log_info(leer)", log_info, "", "")
    await no_crash_async("log_warning(unicode)", log_warning, "test", "Ümläüte ⚠️")
    await no_crash_async("log_error(lang)", log_error, "test", "X" * 10000)

    # ==================================================================
    # config.py
    # ==================================================================
    print("\n── config ──")
    from config import config_manager

    await no_crash_async("config.get(nicht-existent)", config_manager.get, "nonexistent.key.xyz")
    await no_crash_async("config.get(leer)", config_manager.get, "")
    await no_crash_async("config.is_module_enabled(leer)", config_manager.is_module_enabled, "")
    await no_crash_async("config.is_module_enabled(xxx)", config_manager.is_module_enabled, "nonexistent_module")

    # ==================================================================
    # pipeline-spezifische Funktionen
    # ==================================================================
    print("\n── pipeline: _extract_folder_tags ──")
    from pipeline.step_ia02_duplicates import _extract_folder_tags, _quality_score

    # _extract_folder_tags
    no_crash("folder_tags(alles None)", _extract_folder_tags,
             Job(filename="f", original_path=None, source_inbox_path=None, debug_key="X"))
    no_crash("folder_tags(leer)", _extract_folder_tags,
             Job(filename="f", original_path="", source_inbox_path="", debug_key="X"))

    # _quality_score mit minimal Job
    j_min = Job(filename="f.jpg", debug_key="QS-1", step_result={})
    no_crash("quality_score(leer)", _quality_score, j_min)

    j_full = Job(filename="f.jpg", debug_key="QS-2", step_result={
        "IA-01": {"width": 4000, "height": 3000, "file_size": 5000000, "has_exif": True},
        "IA-07": {"tags_count": 10},
    })
    no_crash("quality_score(voll)", _quality_score, j_full)

    # ==================================================================
    # Zusammenfassung
    # ==================================================================
    print("\n" + "=" * 60)
    total = len(PASS) + len(FAIL)
    print(f"  Ergebnis: {len(PASS)}/{total}")
    if FAIL:
        print(f"\n  ❌ Fehlgeschlagen:")
        for f in FAIL:
            print(f"    - {f}")
    else:
        print("  🎉 Alle shared Functions robust — kein Crash bei kaputten Eingaben!")
    print("=" * 60)


asyncio.run(main())
