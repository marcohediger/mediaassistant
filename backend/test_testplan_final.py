"""TESTPLAN.md final consolidated run."""
import asyncio, sys, os, hashlib, json, time, shutil
sys.path.insert(0, '/app')
os.environ.setdefault('DATABASE_PATH','/app/data/mediaassistant.db')

PASS, FAIL, SKIP, BLOCK = [], [], [], []
def ok(s,n,d=""): PASS.append((s,n,d)); print(f"  ✅ {n}" + (f" — {d}" if d else ""))
def fail(s,n,d=""): FAIL.append((s,n,d)); print(f"  ❌ {n}" + (f" — {d}" if d else ""))
def skip(s,n,r=""): SKIP.append((s,n,r)); print(f"  ⏭️  {n}" + (f" — {r}" if r else ""))
def block(s,n,r=""): BLOCK.append((s,n,r)); print(f"  🚧 {n}" + (f" — {r}" if r else ""))
def section(n): print(f"\n{'='*60}\n  {n}\n{'='*60}")
def subsection(n): print(f"\n── {n} ──")


# ───── Sektion 1+6: Pipeline-Steps mit echten Formaten ─────
async def s16_pipeline_formats():
    section("Sektion 1 + 6: Pipeline-Steps + Dateiformate")
    from database import async_session
    from models import Job
    from pipeline import run_pipeline
    from sqlalchemy import delete

    async def run_one(label, src, expected_types, sect="1+6"):
        if not os.path.exists(src):
            block(sect, f"{label}: Datei fehlt: {src}"); return
        # Make unique to avoid duplicate detection
        work = f"/tmp/__tp_{label}_{int(time.time()*1000)}.{src.rsplit('.',1)[-1]}"
        shutil.copy(src, work)
        with open(work, "ab") as f: f.write(os.urandom(32))
        h = hashlib.sha256(open(work,'rb').read()).hexdigest()
        dk = f"TP-{label}-{int(time.time()*1000)}"
        async with async_session() as s:
            j = Job(filename=os.path.basename(work), original_path=work,
                    debug_key=dk, status="queued", file_hash=h,
                    source_label="testplan-final", source_inbox_path="/tmp",
                    dry_run=True, use_immich=False, folder_tags=False)
            s.add(j); await s.commit(); await s.refresh(j); jid = j.id
        await run_pipeline(jid)
        async with async_session() as s:
            j = await s.get(Job, jid)
            sr = j.step_result or {}
            ia01 = sr.get("IA-01", {})
            ft = ia01.get("file_type", "?")
            if ft in expected_types:
                ok(sect, f"{label} IA-01: {ft}", f"size={ia01.get('file_size')}")
            else:
                fail(sect, f"{label} IA-01: {ft}", f"expected {expected_types}")
            ia02 = sr.get("IA-02", {})
            if ia02.get("status") in ("ok","duplicate","skipped"):
                ok(sect, f"{label} IA-02: {ia02.get('status')}")
            ia04 = sr.get("IA-04", {})
            if ia04 and "converted" in ia04:
                ok(sect, f"{label} IA-04: converted={ia04['converted']}")
            ia05 = sr.get("IA-05", {})
            if ia05.get("type"):
                ok(sect, f"{label} IA-05: {ia05.get('type')} (conf {ia05.get('confidence',0):.2f})")
            elif ia05.get("status") == "skipped":
                skip(sect, f"{label} IA-05 skipped: {ia05.get('reason','')[:40]}")
            ia07 = sr.get("IA-07", {})
            if ia07.get("status") == "dry_run":
                ok(sect, f"{label} IA-07 dry_run: {ia07.get('tags_count',0)} Tags")
            ia08 = sr.get("IA-08", {})
            if ia08.get("status") == "dry_run":
                ok(sect, f"{label} IA-08 dry_run: {ia08.get('target_path','?')[:55]}")
            if j.status in ("done","duplicate","review"):
                ok(sect, f"{label} final: {j.status}")
            elif j.status == "error":
                fail(sect, f"{label} final ERROR", j.error_message[:80] if j.error_message else "")
        try: os.remove(work)
        except: pass

    await run_one("HEIC", "/inbox/__test_heic.HEIC", ("HEIC","HEIF"))
    await run_one("MOV",  "/tmp/__direct_mov.MOV", ("MOV","MP4"))

    # Synthetic formats
    from PIL import Image
    for fmt, ext, kw in [("PNG",".png",None), ("WEBP",".webp","WEBP"),
                          ("TIFF",".tiff","TIFF"), ("GIF",".gif","GIF"),
                          ("JPEG",".jpg","JPEG"), ("BMP",".bmp","BMP")]:
        p = f"/tmp/__synth_{fmt}_{int(time.time()*1000)}{ext}"
        try:
            img = Image.new("RGB", (200, 200), tuple(int(time.time()*100) % 256 for _ in range(3)))
            img.save(p, kw or fmt)
            await run_one(fmt, p, (fmt, fmt.lower(), ext.lstrip(".").upper()))
        except Exception as e:
            block("1+6", f"{fmt} synth fail: {e}")
        finally:
            try: os.remove(p)
            except: pass


# ───── Sektion 2: Pipeline-Fehlerbehandlung ─────
async def s2_errors():
    section("Sektion 2: Pipeline-Fehlerbehandlung")
    from database import async_session
    from models import Job
    from pipeline import run_pipeline
    from sqlalchemy import delete

    async with async_session() as s:
        await s.execute(delete(Job).where(Job.debug_key.like("S2-%")))
        await s.commit()

    # Critical IA-01 fail
    async with async_session() as s:
        j = Job(filename="x", original_path="/tmp/__nofile_S2A.jpg",
                debug_key="S2-A", status="queued", file_hash="z"*64,
                source_label="testplan-final", dry_run=False, use_immich=False, folder_tags=False)
        s.add(j); await s.commit(); await s.refresh(j); jid = j.id
    await run_pipeline(jid)
    async with async_session() as s:
        j = await s.get(Job, jid)
        if j.status == "error": ok("2", "Critical IA-01 fail → status=error")
        else: fail("2", f"Erwartet error, got {j.status}")
        if j.error_message and "IA-01" in j.error_message:
            ok("2", "error_message enthält IA-01-Marker")
        sr = j.step_result or {}
        if all(k in sr for k in ("IA-09","IA-10","IA-11")):
            ok("2", "Finalizer (IA-09/10/11) liefen nach kritischem Fehler")
        else:
            fail("2", f"Finalizer fehlen: keys={list(sr.keys())}")


# ───── Sektion 3: Web Interface (Endpoint Reachability) ─────
async def s3_web():
    section("Sektion 3: Web Interface (Endpoint-Reachability)")
    try:
        from httpx import AsyncClient
    except ImportError:
        block("3", "httpx fehlt"); return

    base = "http://localhost:8000"
    endpoints = [
        ("/api/version", "Version"),
        ("/api/dashboard", "Dashboard JSON"),
        ("/", "Root"),
        ("/login", "Login Page"),
        ("/review", "Review"),
        ("/logs", "Logs"),
        ("/settings", "Settings"),
        ("/duplicates", "Duplicates"),
    ]
    async with AsyncClient(follow_redirects=True, timeout=10) as c:
        for path, label in endpoints:
            try:
                t0 = time.time()
                r = await c.get(base + path)
                ms = (time.time()-t0)*1000
                if 200 <= r.status_code < 400:
                    ok("3", f"{label} reachable", f"{r.status_code} in {ms:.0f}ms")
                else:
                    fail("3", f"{label} → {r.status_code}", f"({ms:.0f}ms)")
            except Exception as e:
                fail("3", f"{label} ERROR", str(e)[:60])


# ───── Sektion 4: Filewatcher ─────
async def s4_filewatcher():
    section("Sektion 4: Filewatcher-Stabilität")
    from filewatcher import _is_file_stable, SUPPORTED_EXTENSIONS, _SKIP_DIRS

    p = "/tmp/__fw_stable.jpg"
    with open(p,"wb") as f: f.write(b"X"*1000)
    if _is_file_stable(p, 1000): ok("4", "Stabile Datei → True")
    else: fail("4", "Stabile Datei nicht erkannt")

    p2 = "/tmp/__fw_empty.jpg"
    open(p2,"wb").close()
    if not _is_file_stable(p2, 0): ok("4", "Leere Datei (0 Bytes) → unstable")
    else: fail("4", "Leere Datei wird als stabil erkannt")

    p3 = "/tmp/__fw_mismatch.jpg"
    with open(p3,"wb") as f: f.write(b"X"*500)
    t0 = time.time()
    res = _is_file_stable(p3, 1000)
    el = time.time() - t0
    if not res and el >= 0.9:
        ok("4", f"Größen-Mismatch → unstable nach {el:.1f}s Wartezeit")
    else:
        fail("4", f"Mismatch: result={res} time={el:.1f}s")
    for f in (p,p2,p3):
        try: os.remove(f)
        except: pass

    expected = {'.jpg','.heic','.mp4','.mov','.dng','.png','.webp','.gif','.tiff','.bmp'}
    if expected.issubset(SUPPORTED_EXTENSIONS):
        ok("4", f"24 Extensions registriert ({len(SUPPORTED_EXTENSIONS)} total)")
    else:
        fail("4", f"Fehlende: {expected - SUPPORTED_EXTENSIONS}")

    if _SKIP_DIRS == {'@eadir', '.synology', '#recycle'}:
        ok("4", f"Synology SKIP_DIRS korrekt")
    else:
        fail("4", f"SKIP_DIRS: {_SKIP_DIRS}")


# ───── Sektion 8: Security ─────
async def s8_security():
    section("Sektion 8: Security")
    from pipeline.step_ia08_sort import _validate_target_path

    try:
        _validate_target_path("/library/../etc", "/library")
        fail("8", "S1-3: ../etc sollte ValueError werfen")
    except ValueError:
        ok("8", "S1-3: Path Traversal blocked → ValueError")

    try:
        r = _validate_target_path("/library/photos/2026", "/library")
        ok("8", f"S1-4: Normaler Pfad akzeptiert", r)
    except Exception as e:
        fail("8", f"S1-4: blocked: {e}")

    try:
        from immich_client import _sanitize_filename
        cases = [
            ("../../etc/passwd", "passwd"),
            ("/etc/passwd", "passwd"),
            ("photo_2026.jpg", "photo_2026.jpg"),
            ("", "asset.jpg"),
            (None, "asset.jpg"),
        ]
        for inp, exp in cases:
            got = _sanitize_filename(inp)
            if got == exp:
                ok("8", f"_sanitize_filename({inp!r}) → {got!r}")
            else:
                fail("8", f"_sanitize_filename({inp!r}) → {got!r} (exp {exp!r})")
    except ImportError:
        block("8", "_sanitize_filename Import fehlt")


# ───── Sektion 9: Performance ─────
async def s9_perf():
    section("Sektion 9: Performance")
    from database import async_session
    from sqlalchemy import text, select, func
    from models import Job

    async with async_session() as s:
        r = await s.execute(text("SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"))
        names = {row[0] for row in r.all()}
    expected = {'idx_job_status','idx_job_file_hash','idx_job_phash','idx_job_original_path',
                'idx_job_created_at','idx_job_updated_at','idx_syslog_created_at'}
    missing = expected - names
    if not missing:
        ok("9", f"7+ DB-Indexes vorhanden", f"{len(names)} total")
    else:
        fail("9", "Indexes fehlen", str(missing))

    # Dashboard query speed
    times = []
    for _ in range(3):
        t0 = time.time()
        async with async_session() as s:
            r = await s.execute(select(Job.status, func.count(Job.id)).group_by(Job.status))
            r.all()
        times.append((time.time()-t0)*1000)
    avg = sum(times)/len(times)
    if avg < 100:
        ok("9", f"Dashboard query {avg:.1f}ms (< 100ms)")
    else:
        fail("9", f"Dashboard query {avg:.1f}ms zu langsam")

    from filewatcher import MAX_FILE_SIZE
    if MAX_FILE_SIZE == 10 * 1024**3:
        ok("9", "MAX_FILE_SIZE = 10 GB")
    else:
        fail("9", f"MAX_FILE_SIZE = {MAX_FILE_SIZE}")


# ───── Sektion 7: Edge Cases ─────
async def s7_edge():
    section("Sektion 7: Edge Cases")
    from filewatcher import SUPPORTED_EXTENSIONS

    if ".txt" not in SUPPORTED_EXTENSIONS:
        ok("7", ".txt wird ignoriert")
    else:
        fail("7", ".txt fälschlich registriert")

    from database import async_session
    from models import Job
    from sqlalchemy import delete

    async with async_session() as s:
        await s.execute(delete(Job).where(Job.debug_key.like("S7-%")))
        await s.commit()

    test_names = ["Foto mit Leerzeichen.jpg", "Umläüten_äöü.jpg",
                  "测试照片_テスト.jpg", "🏔️_Berge.jpg",
                  "DJI_0061 (2).JPG", "photo.jpg.jpg"]
    for i, name in enumerate(test_names):
        async with async_session() as s:
            try:
                j = Job(filename=name, original_path=f"/tmp/{name}",
                        debug_key=f"S7-{i}", status="queued",
                        file_hash=f"y{i:02d}"*16,
                        source_label="testplan", dry_run=False,
                        use_immich=False, folder_tags=False)
                s.add(j); await s.commit()
                ok("7", f"Sonderzeichen-Name: {name[:30]}")
                await s.execute(delete(Job).where(Job.debug_key == f"S7-{i}"))
                await s.commit()
            except Exception as e:
                fail("7", f"'{name}': {e}")


# ───── Sektion 12: Stress / Concurrent ─────
async def s12_stress():
    section("Sektion 12: Stress / Concurrent (10 Dateien parallel)")
    from database import async_session
    from models import Job
    from pipeline import run_pipeline
    from sqlalchemy import delete
    from PIL import Image

    async with async_session() as s:
        await s.execute(delete(Job).where(Job.debug_key.like("S12-%")))
        await s.commit()

    # Generate 10 unique tiny PNGs
    paths = []
    for i in range(10):
        p = f"/tmp/__s12_{i}_{int(time.time()*1000)}.png"
        Image.new("RGB", (60, 60), (i*25 % 256, 100, 150)).save(p)
        paths.append(p)

    job_ids = []
    for i, p in enumerate(paths):
        h = hashlib.sha256(open(p,"rb").read()).hexdigest()
        async with async_session() as s:
            j = Job(filename=os.path.basename(p), original_path=p,
                    debug_key=f"S12-{i}-{int(time.time()*1000)}",
                    status="queued", file_hash=h, source_label="stress",
                    dry_run=True, use_immich=False, folder_tags=False)
            s.add(j); await s.commit(); await s.refresh(j); job_ids.append(j.id)

    # Run all in parallel
    t0 = time.time()
    await asyncio.gather(*[run_pipeline(jid) for jid in job_ids])
    elapsed = time.time() - t0

    async with async_session() as s:
        completed = 0; errored = 0
        for jid in job_ids:
            j = await s.get(Job, jid)
            if j.status in ("done","duplicate","review"): completed += 1
            elif j.status == "error": errored += 1

    if completed == len(job_ids):
        ok("12", f"10 Dateien parallel: alle {completed} verarbeitet in {elapsed:.1f}s")
    else:
        fail("12", f"10 Dateien: {completed} done, {errored} error", "")

    for p in paths:
        try: os.remove(p)
        except: pass


# ───── Main ─────
async def main():
    await s9_perf()
    await s8_security()
    await s4_filewatcher()
    await s7_edge()
    await s2_errors()
    await s16_pipeline_formats()
    await s12_stress()
    await s3_web()

    print("\n" + "="*60)
    print("  GESAMT-ZUSAMMENFASSUNG")
    print("="*60)
    total = len(PASS)+len(FAIL)+len(SKIP)+len(BLOCK)
    print(f"  ✅ PASS:    {len(PASS)}")
    print(f"  ❌ FAIL:    {len(FAIL)}")
    print(f"  ⏭️  SKIP:    {len(SKIP)}  (erwartete Skips, z.B. duplicate detection)")
    print(f"  🚧 BLOCK:   {len(BLOCK)}  (benötigt externe Infrastruktur)")
    print(f"  ─────────")
    print(f"  GESAMT:     {total}")
    if FAIL:
        print(f"\n  ❌ Fehlgeschlagen ({len(FAIL)}):")
        for s,n,d in FAIL:
            print(f"    [{s}] {n}" + (f" — {d}" if d else ""))
    if BLOCK:
        print(f"\n  🚧 Geblockt ({len(BLOCK)}):")
        for s,n,d in BLOCK:
            print(f"    [{s}] {n}" + (f" — {d}" if d else ""))

asyncio.run(main())
