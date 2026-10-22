"""Inputs — the ways files get into the job queue.

Every input only *enqueues*. Processing happens in exactly one place, the
worker (`worker.py`), so slot limits, pause and stale detection apply to
every job no matter where it came from.
"""
