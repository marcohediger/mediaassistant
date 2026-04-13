"""Setup: Testdaten für manuelles Testen der folder_tags / Album-Funktionalität.

Erzeugt VERSCHIEDENE Testbilder (unterschiedliche Farben, Grössen, EXIF)
damit man sie im UI und in Immich visuell unterscheiden kann.
"""
import asyncio, sys, os, time, shutil, struct, random, hashlib
sys.path.insert(0, "/app")
os.environ.setdefault("DATABASE_PATH", "/app/data/mediaassistant.db")


def create_test_jpeg(path, width=640, height=480, color=(255, 0, 0), label=""):
    """Create a minimal but valid JPEG with a solid color using raw bytes.
    Different colors make images visually distinguishable."""
    try:
        from PIL import Image, ImageDraw, ImageFont
        img = Image.new("RGB", (width, height), color)
        draw = ImageDraw.Draw(img)
        # Add random noise so each image is unique (different hash + pHash)
        import random as _rnd
        for _ in range(500):
            x = _rnd.randint(0, width - 1)
            y = _rnd.randint(0, height - 1)
            r2 = _rnd.randint(0, 255)
            g2 = _rnd.randint(0, 255)
            b2 = _rnd.randint(0, 255)
            img.putpixel((x, y), (r2, g2, b2))
        if label:
            # Draw label text
            try:
                font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 36)
            except Exception:
                font = ImageFont.load_default()
            # Black text with white outline for readability
            for dx in [-2, -1, 0, 1, 2]:
                for dy in [-2, -1, 0, 1, 2]:
                    draw.text((20+dx, 20+dy), label, fill=(0, 0, 0), font=font)
            draw.text((20, 20), label, fill=(255, 255, 255), font=font)
        # Add timestamp watermark bottom-right
        ts_label = str(int(time.time()))
        try:
            small_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 14)
        except Exception:
            small_font = ImageFont.load_default()
        draw.text((width - 120, height - 25), ts_label, fill=(200, 200, 200), font=small_font)
        img.save(path, "JPEG", quality=85)
        return True
    except ImportError:
        # No PIL — create minimal JPEG with raw bytes
        # This creates a tiny but valid JPEG
        import subprocess
        # Use ImageMagick if available
        try:
            r, g, b = color
            subprocess.run([
                "convert", "-size", f"{width}x{height}",
                f"xc:rgb({r},{g},{b})",
                "-gravity", "NorthWest", "-pointsize", "30",
                "-fill", "white", "-stroke", "black", "-strokewidth", "1",
                "-annotate", "+20+40", label or "test",
                path
            ], check=True, capture_output=True, timeout=10)
            return True
        except Exception:
            pass
        # Last resort: copy an existing file and modify slightly
        return False


async def main():
    from database import async_session
    from models import Job
    from sqlalchemy import select
    from sqlalchemy.orm.attributes import flag_modified
    from config import config_manager
    from pipeline import run_pipeline

    ts = int(time.time())
    print("=" * 60)
    print("  Setup: Verschiedene Testbilder für manuelles Testen")
    print("=" * 60)

    await config_manager.set("pipeline.use_immich", True)
    await config_manager.set("module.folder_tags", True)

    # Define visually distinct test images
    test_images = [
        {"color": (220, 50, 50),  "label": "ROT\nFerien Mallorca",   "w": 800, "h": 600},
        {"color": (50, 50, 220),  "label": "BLAU\nFerien Mallorca",  "w": 640, "h": 480},
        {"color": (50, 180, 50),  "label": "GRUEN\nUrlaub Spanien",  "w": 1024, "h": 768},
        {"color": (200, 200, 50), "label": "GELB\nOhne Album",       "w": 640, "h": 480},
        {"color": (180, 50, 180), "label": "LILA\nReisen Griechenland", "w": 800, "h": 600},
        {"color": (50, 200, 200), "label": "CYAN\nWanderung Alpen",  "w": 1024, "h": 768},
        {"color": (255, 140, 0),  "label": "ORANGE\nGeburtstag Lisa", "w": 800, "h": 600},
        {"color": (128, 128, 128),"label": "GRAU\nFlat (kein Album)", "w": 640, "h": 480},
    ]

    tmp_dir = "/tmp/ftag_testbilder"
    os.makedirs(tmp_dir, exist_ok=True)

    image_paths = []
    for i, spec in enumerate(test_images):
        path = os.path.join(tmp_dir, f"test_{i}_{spec['label'].split(chr(10))[0].lower()}.jpg")
        ok = create_test_jpeg(path, spec["w"], spec["h"], spec["color"], spec["label"])
        if ok:
            image_paths.append(path)
            sz = os.path.getsize(path)
            print(f"  ✅ {spec['label'].split(chr(10))[0]:8s} {spec['w']}x{spec['h']} {sz:>6} bytes → {path}")
        else:
            # Fallback: use existing file with random bytes appended
            fallback = None
            for d in ["/app/data/reprocess"]:
                for f in os.listdir(d):
                    if f.endswith(('.HEIC', '.jpg')):
                        fallback = os.path.join(d, f)
                        break
                if fallback:
                    break
            if fallback:
                with open(fallback, 'rb') as src:
                    data = src.read()
                with open(path, 'wb') as dst:
                    dst.write(data + os.urandom(64 + i * 17))
                image_paths.append(path)
                print(f"  ⚠️ {spec['label'].split(chr(10))[0]:8s} (Fallback, nicht visuell unterscheidbar)")
            else:
                print(f"  ❌ Konnte kein Bild erstellen")
                return

    if len(image_paths) < 8:
        print("Nicht genug Bilder")
        return

    created_jobs = []
    dup_dir = "/library/error/duplicates"
    os.makedirs(dup_dir, exist_ok=True)

    # ── 1. Duplikat-Gruppe: 3 Members mit verschiedenen folder_tags ──
    print("\n── 1. Duplikat-Gruppe (3 Members, verschiedene Bilder) ──")
    group_phash = f"manual_grp_{ts}"

    async with async_session() as session:
        # Original (ROT)
        fn0 = f"__manual_orig_ROT_{ts}.jpg"
        dst0 = f"/library/photos/2026/{fn0}"
        os.makedirs(os.path.dirname(dst0), exist_ok=True)
        shutil.copy2(image_paths[0], dst0)
        orig = Job(
            filename=fn0, original_path=dst0, source_inbox_path="/inbox",
            status="done", target_path=dst0,
            debug_key=f"MANUAL-Original-ROT-{ts}",
            file_hash=f"manual_orig_{ts}", phash=group_phash,
            use_immich=True,
            step_result={
                "IA-01": {"file_type": "JPEG", "file_size": os.path.getsize(dst0)},
                "IA-02": {"status": "ok"},
            },
        )
        session.add(orig)
        created_jobs.append(orig.debug_key)

        # Dup 1 (BLAU) — Ferien Mallorca
        fn1 = f"__manual_dup_BLAU_{ts}.jpg"
        dst1 = os.path.join(dup_dir, fn1)
        shutil.copy2(image_paths[1], dst1)
        dup1 = Job(
            filename=fn1, original_path=dst1, source_inbox_path="/inbox",
            status="duplicate", target_path=dst1,
            debug_key=f"MANUAL-Dup-BLAU-{ts}",
            file_hash=f"manual_dup1_{ts}", phash=group_phash,
            use_immich=True,
            step_result={
                "IA-01": {"file_type": "JPEG", "file_size": os.path.getsize(dst1)},
                "IA-02": {
                    "status": "duplicate", "match_type": "similar", "phash_distance": 3,
                    "original_debug_key": orig.debug_key,
                    "folder_tags": ["Ferien", "Mallorca", "Ferien Mallorca"],
                },
            },
        )
        session.add(dup1)
        created_jobs.append(dup1.debug_key)

        # Dup 2 (GRÜN) — Urlaub Spanien
        fn2 = f"__manual_dup_GRUEN_{ts}.jpg"
        dst2 = os.path.join(dup_dir, fn2)
        shutil.copy2(image_paths[2], dst2)
        dup2 = Job(
            filename=fn2, original_path=dst2, source_inbox_path="/inbox",
            status="duplicate", target_path=dst2,
            debug_key=f"MANUAL-Dup-GRUEN-{ts}",
            file_hash=f"manual_dup2_{ts}", phash=group_phash,
            use_immich=True,
            step_result={
                "IA-01": {"file_type": "JPEG", "file_size": os.path.getsize(dst2)},
                "IA-02": {
                    "status": "duplicate", "match_type": "similar", "phash_distance": 4,
                    "original_debug_key": orig.debug_key,
                    "folder_tags": ["Urlaub", "Spanien", "Urlaub Spanien"],
                },
            },
        )
        session.add(dup2)
        created_jobs.append(dup2.debug_key)
        await session.commit()

    print(f"  ✅ ROT = Original (done)")
    print(f"  ✅ BLAU = Dup mit 📁 Ferien Mallorca")
    print(f"  ✅ GRÜN = Dup mit 📁 Urlaub Spanien")

    # ── 2. Einzelnes Duplikat ohne folder_tags (GELB) ──
    print("\n── 2. Duplikat OHNE folder_tags (GELB) ──")
    async with async_session() as session:
        fn3 = f"__manual_noft_GELB_{ts}.jpg"
        dst3 = os.path.join(dup_dir, fn3)
        shutil.copy2(image_paths[3], dst3)
        job3 = Job(
            filename=fn3, original_path=dst3, source_inbox_path="/inbox",
            status="duplicate", target_path=dst3,
            debug_key=f"MANUAL-NoFT-GELB-{ts}",
            file_hash=f"manual_noft_{ts}", phash=f"noft_{ts}",
            use_immich=True,
            step_result={
                "IA-01": {"file_type": "JPEG", "file_size": os.path.getsize(dst3)},
                "IA-02": {"status": "duplicate", "match_type": "exact"},
            },
        )
        session.add(job3)
        created_jobs.append(job3.debug_key)
        await session.commit()
    print(f"  ✅ GELB = Duplikat ohne Album-Badge")

    # ── 3. Duplikat mit tiefem Subfolder (LILA) ──
    print("\n── 3. Tiefer Subfolder (LILA) ──")
    async with async_session() as session:
        fn4 = f"__manual_deep_LILA_{ts}.jpg"
        dst4 = os.path.join(dup_dir, fn4)
        shutil.copy2(image_paths[4], dst4)
        job4 = Job(
            filename=fn4, original_path=dst4, source_inbox_path="/inbox",
            status="duplicate", target_path=dst4,
            debug_key=f"MANUAL-Deep-LILA-{ts}",
            file_hash=f"manual_deep_{ts}", phash=f"deep_{ts}",
            use_immich=True,
            step_result={
                "IA-01": {"file_type": "JPEG", "file_size": os.path.getsize(dst4)},
                "IA-02": {
                    "status": "duplicate", "match_type": "similar",
                    "folder_tags": ["Reisen", "2026", "Griechenland", "Reisen 2026 Griechenland"],
                },
            },
        )
        session.add(job4)
        created_jobs.append(job4.debug_key)
        await session.commit()
    print(f"  ✅ LILA = 📁 Reisen 2026 Griechenland")

    # ── 4. Echte Pipeline-Runs mit Subfolder → Immich ──
    print("\n── 4. Echte Uploads → Immich (verschiedene Bilder) ──")

    scenarios = [
        {"album": "Wanderung_Alpen", "img": image_paths[5], "label": "CYAN"},
        {"album": "Geburtstag_Lisa", "img": image_paths[6], "label": "ORANGE"},
    ]

    for sc in scenarios:
        inbox_dir = f"/inbox/{sc['album']}"
        os.makedirs(inbox_dir, exist_ok=True)
        fn = f"__manual_{sc['album']}_{ts}.jpg"
        dst = os.path.join(inbox_dir, fn)
        shutil.copy2(sc["img"], dst)

        async with async_session() as session:
            job = Job(
                filename=fn, original_path=dst, source_inbox_path="/inbox",
                status="queued", debug_key=f"MANUAL-{sc['album']}-{ts}",
                use_immich=True,
            )
            session.add(job)
            await session.commit()
            job_id = job.id
            created_jobs.append(job.debug_key)

        print(f"  Pipeline: {sc['label']} → {sc['album']}...")
        await run_pipeline(job_id)

        async with async_session() as session:
            r = await session.execute(select(Job).where(Job.id == job_id))
            j = r.scalar()
            ia08 = (j.step_result or {}).get("IA-08", {})
            print(f"  ✅ {sc['label']}: status={j.status}, asset={j.immich_asset_id}, albums={ia08.get('immich_albums_added')}")

    # ── 5. Flat Upload (GRAU) → kein Album ──
    print("\n── 5. Flat Upload → kein Album (GRAU) ──")
    fn5 = f"__manual_flat_GRAU_{ts}.jpg"
    dst5 = f"/inbox/{fn5}"
    shutil.copy2(image_paths[7], dst5)

    async with async_session() as session:
        job5 = Job(
            filename=fn5, original_path=dst5, source_inbox_path="/inbox",
            status="queued", debug_key=f"MANUAL-Flat-GRAU-{ts}",
            use_immich=True,
        )
        session.add(job5)
        await session.commit()
        job5_id = job5.id
        created_jobs.append(job5.debug_key)

    print(f"  Pipeline: GRAU → flat inbox...")
    await run_pipeline(job5_id)

    async with async_session() as session:
        r = await session.execute(select(Job).where(Job.id == job5_id))
        j = r.scalar()
        ia08 = (j.step_result or {}).get("IA-08", {})
        print(f"  ✅ GRAU: status={j.status}, asset={j.immich_asset_id}, albums={ia08.get('immich_albums_added')}")

    # Restore config
    await config_manager.set("pipeline.use_immich", False)
    await config_manager.set("module.folder_tags", True)

    print("\n" + "=" * 60)
    print("  Testdaten bereit! Manuell prüfen:")
    print("=" * 60)
    print("""
  /duplicates:
    • Gruppe ROT/BLAU/GRÜN → BLAU hat 📁 Ferien Mallorca, GRÜN hat 📁 Urlaub Spanien
    • GELB → kein Album-Badge
    • LILA → 📁 Reisen 2026 Griechenland
    • "Behalten" bei BLAU klicken → Album "Ferien Mallorca" in Immich
    • "Kein Duplikat" bei LILA klicken → Album "Reisen 2026 Griechenland" in Immich

  Immich:
    • Album "Wanderung_Alpen" (CYAN-Bild)
    • Album "Geburtstag_Lisa" (ORANGE-Bild)
    • Tags prüfen: keine Pfad-Fragmente (app, .., data, reprocess)

  Logs:
    • GRAU: kein Album (flat inbox)
""")

asyncio.run(main())
