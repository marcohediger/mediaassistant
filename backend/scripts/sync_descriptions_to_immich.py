#!/usr/bin/env python3
"""Sync descriptions from step_result to Immich for all done jobs.

Writes the IA-07 description_written to Immich via PUT /api/assets/{id}
for every job that has a description and an Immich asset ID.

Run inside the container:
    python3 /app/scripts/sync_descriptions_to_immich.py [--dry-run]
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def main():
    dry_run = "--dry-run" in sys.argv

    from database import init_db, async_session
    from models import Job
    from sqlalchemy import select
    from immich_client import update_asset_description, get_user_api_key

    await init_db()

    updated = 0
    skipped = 0
    errors = 0

    async with async_session() as session:
        result = await session.execute(
            select(Job).where(
                Job.immich_asset_id.isnot(None),
                Job.immich_asset_id != "",
                Job.status == "done",
            )
        )
        jobs = result.scalars().all()
        print(f"Found {len(jobs)} done jobs with Immich asset")

        for job in jobs:
            sr = job.step_result or {}
            ia07 = sr.get("IA-07") or {}
            desc = ia07.get("description_written", "")

            if not desc:
                skipped += 1
                continue

            if dry_run:
                updated += 1
                continue

            try:
                api_key = None
                if job.immich_user_id:
                    api_key = await get_user_api_key(job.immich_user_id)
                await update_asset_description(
                    job.immich_asset_id, desc, api_key=api_key,
                )
                updated += 1
                if updated % 100 == 0:
                    print(f"  ... {updated} updated")
            except Exception as e:
                errors += 1
                if errors <= 10:
                    print(f"  ERROR {job.id} {job.filename}: {e}")

            # Small delay to not overwhelm Immich
            await asyncio.sleep(0.05)

    print()
    print(f"{'[DRY RUN] ' if dry_run else ''}Results:")
    print(f"  Updated:  {updated}")
    print(f"  Skipped:  {skipped} (no description)")
    print(f"  Errors:   {errors}")


if __name__ == "__main__":
    asyncio.run(main())
