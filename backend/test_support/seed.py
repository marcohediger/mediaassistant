"""Pre-filled module results for test jobs, written through runs.py.

``jobs.step_result`` is a derived view with one writer, runs.py
(``test_no_direct_step_result.py`` guards that). A test that needs a job in
a given state (modules that already ran, a run parameter, a duplicate plan
in progress) describes the state as the dict the view would show and hands
it to ``seed_results``:

- each module result ("IA-01" … "IA-11") becomes a finished run with
  trigger 'migrated', the rule of the one-time startup migration
  (``database._migrate_module_runs``): no fingerprint, so never stale;
- every other key becomes a job value: keys starting with "_" are run
  parameters (``_force_reupload`` …), others are e.g. ``resolving`` or
  ``dup_merge``.

The state replaces what the job had (``runs.apply``): a finished run of a
module missing from it is superseded, the job values are replaced. A
module whose current run already has the same result is left alone.

Like every write of runs.py it takes SQLite's write lock (it touches the job
row): commit before calling fileops, prepare_job_for_reprocess or anything
else that writes in sessions of its own.

Example::

    async with async_session() as s:
        job = Job(filename=..., original_path=..., status="queued")
        s.add(job)
        await seed_results(s, job, {"IA-01": {"file_type": "JPEG"}, "_force_reupload": True})
        await s.commit()
"""
from __future__ import annotations


async def seed_results(session, job, state: dict | None) -> list[str]:
    """Give `job` the module results and job values of `state` (see the
    module docstring). A new job is flushed first (runs need its id).
    Does not commit. Returns the modules that changed."""
    import runs
    if job.id is None:
        await session.flush()
    return await runs.apply(session, job, dict(state or {}), trigger="migrated")
