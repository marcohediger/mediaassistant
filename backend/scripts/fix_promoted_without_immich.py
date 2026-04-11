#!/usr/bin/env python3
"""Fix 147 promoted jobs that were never uploaded to Immich.

These jobs were promoted by Batch-Clean (better quality duplicate) but
the Immich asset ID from the deleted counterpart was not transferred.
As a result, the better file stayed in /library/error/duplicates/ and
Immich kept the lower-quality version.

This script:
1. Finds promoted jobs without immich_asset_id
2. Locates the deleted counterpart's Immich asset ID (via phash match)
3. Sets immich_asset_id on the promoted job
4. Moves the file to /reprocess/ and requeues for pipeline processing
5. The pipeline will then do Upload→Copy→Delete to replace the asset

Run inside the container:
    python3 /app/scripts/fix_promoted_without_immich.py [--dry-run]
"""

import asyncio
import json
import os
import sys

# Add backend to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def main():
    dry_run = "--dry-run" in sys.argv

    from database import init_db, async_session
    from models import Job
    from sqlalchemy import select
    from sqlalchemy.orm.attributes import flag_modified

    await init_db()

    fixed = 0
    skipped_no_file = 0
    skipped_no_counterpart = 0

    async with async_session() as session:
        # Find promoted jobs without Immich asset
        result = await session.execute(
            select(Job).where(
                Job.error_message.like("%Promoted%"),
                Job.immich_asset_id.is_(None),
                ~Job.target_path.like("immich:%"),
            )
        )
        promoted_jobs = result.scalars().all()
        print(f"Found {len(promoted_jobs)} promoted jobs without Immich asset")

        for job in promoted_jobs:
            filepath = job.target_path or job.original_path
            file_exists = filepath and os.path.exists(filepath)

            # Find deleted counterpart with Immich asset (same phash)
            if not job.phash:
                print(f"  SKIP {job.id} {job.filename}: no phash")
                skipped_no_counterpart += 1
                continue

            counterpart = await session.execute(
                select(Job).where(
                    Job.phash == job.phash,
                    Job.id != job.id,
                    Job.target_path.like("immich:%"),
                )
            )
            counterpart_job = counterpart.scalars().first()

            immich_asset_id = None
            if counterpart_job:
                immich_asset_id = counterpart_job.immich_asset_id
                if not immich_asset_id and counterpart_job.target_path:
                    immich_asset_id = counterpart_job.target_path[len("immich:"):]

            if not immich_asset_id:
                print(f"  SKIP {job.id} {job.filename}: no Immich counterpart found")
                skipped_no_counterpart += 1
                continue

            if not file_exists:
                print(f"  SKIP {job.id} {job.filename}: file missing at {filepath}")
                skipped_no_file += 1
                continue

            print(f"  FIX  {job.id} {job.filename}: immich_asset={immich_asset_id} file={filepath}")

            if dry_run:
                fixed += 1
                continue

            # Set immich_asset_id so IA-08 does replace workflow
            job.immich_asset_id = immich_asset_id

            # Move file to reprocess dir and reset steps
            from pipeline.reprocess import prepare_job_for_reprocess
            await prepare_job_for_reprocess(
                session,
                job,
                keep_steps={"IA-01"},
                inject_steps={
                    "IA-02": {"status": "skipped", "reason": "kept via batch-clean fix"},
                },
                move_file=True,
                commit=False,
            )
            job.status = "queued"
            job.error_message = f"Re-queued: replacing Immich asset with better quality version"
            fixed += 1

        if not dry_run:
            await session.commit()

    print()
    print(f"{'[DRY RUN] ' if dry_run else ''}Results:")
    print(f"  Fixed:                {fixed}")
    print(f"  Skipped (no file):    {skipped_no_file}")
    print(f"  Skipped (no Immich):  {skipped_no_counterpart}")


if __name__ == "__main__":
    asyncio.run(main())
