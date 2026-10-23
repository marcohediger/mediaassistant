from sqlalchemy import Column, Integer, Text, Boolean, DateTime, JSON, UniqueConstraint, Index, text
from sqlalchemy.orm import DeclarativeBase, synonym
from datetime import datetime, timezone


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "jobs"
    # Never hand out the id of a deleted job again: register, reference and
    # ledger rows are keyed by job id (R-26). Existing databases are rebuilt
    # once in database._jobs_autoincrement.
    __table_args__ = {"sqlite_autoincrement": True}

    id = Column(Integer, primary_key=True)
    filename = Column(Text, nullable=False)
    original_path = Column(Text, nullable=False)
    target_path = Column(Text)
    debug_key = Column(Text, unique=True)
    status = Column(Text, default="queued")  # queued/processing/done/error/duplicate/review/orphan/skipped/deleted
    current_step = Column(Text)  # IA-01 bis IA-11
    step_result = Column(JSON, default=dict)
    error_message = Column(Text)
    source_label = Column(Text)  # which inbox this came from
    source_inbox_path = Column(Text)  # inbox base path (always set for cleanup)
    folder_tags = Column(Boolean, default=False)  # generate tags/albums from folder structure
    dry_run = Column(Boolean, default=False)  # dry-run mode (don't move/write)
    use_immich = Column(Boolean, default=False)  # upload to Immich instead of target directory
    immich_asset_id = Column(Text)  # source asset ID when processing via Immich webhook
    immich_user_id = Column(Integer)  # FK to immich_users.id (for per-user API key)
    set_id = Column(Integer)  # FK to processing_sets.id — rules, categories and base path come from here
    file_hash = Column(Text)  # SHA256
    phash = Column(Text)  # perceptual hash
    retry_count = Column(Integer, default=0)  # number of pipeline restart attempts
    created_at = Column(DateTime, default=lambda: datetime.now())
    started_at = Column(DateTime)  # pipeline processing start
    updated_at = Column(DateTime, default=lambda: datetime.now(), onupdate=lambda: datetime.now())
    completed_at = Column(DateTime)


class Config(Base):
    __tablename__ = "config"

    key = Column(Text, primary_key=True)
    value = Column(Text)  # JSON-encoded
    encrypted = Column(Boolean, default=False)
    updated_at = Column(DateTime, default=lambda: datetime.now(), onupdate=lambda: datetime.now())


class Module(Base):
    __tablename__ = "modules"

    name = Column(Text, primary_key=True)
    enabled = Column(Boolean, default=True)
    updated_at = Column(DateTime, default=lambda: datetime.now(), onupdate=lambda: datetime.now())


class SystemLog(Base):
    __tablename__ = "system_logs"

    id = Column(Integer, primary_key=True)
    level = Column(Text, nullable=False)  # INFO / WARNING / ERROR
    source = Column(Text, nullable=False)  # module name or system component
    message = Column(Text, nullable=False)
    detail = Column(Text)
    created_at = Column(DateTime, default=lambda: datetime.now())


class SortingRule(Base):
    __tablename__ = "sorting_rules"

    id = Column(Integer, primary_key=True)
    position = Column(Integer, nullable=False, default=0)  # order of evaluation
    condition = Column(Text, nullable=False)  # filename_contains / exif_empty / exif_contains / extension
    value = Column(Text, nullable=False)  # match value (e.g. "-WA", "Screenshot", ".png")
    target_category = Column(Text, nullable=False)  # photo / screenshot / sourceless / video / unknown
    media_type = Column(Text, nullable=True)  # "image" / "video" / None (= all)
    set_id = Column(Integer)  # FK to processing_sets.id
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=lambda: datetime.now())
    updated_at = Column(DateTime, default=lambda: datetime.now(), onupdate=lambda: datetime.now())


class LibraryCategory(Base):
    __tablename__ = "library_categories"
    # Every set carries its own categories, so a key is only unique within it.
    __table_args__ = (UniqueConstraint("set_id", "key", name="uq_category_set_key"),)

    id = Column(Integer, primary_key=True)
    set_id = Column(Integer)  # FK to processing_sets.id
    key = Column(Text, nullable=False)  # internal key (photo, video, screenshot, ...)
    label = Column(Text, nullable=False)  # display name
    path_template = Column(Text, nullable=False)  # e.g. "photos/{YYYY}/{YYYY-MM}/"
    fixed = Column(Boolean, default=False)  # fixed categories cannot be deleted (unknown, error, duplicate)
    immich_archive = Column(Boolean, default=False)  # archive asset in Immich (hide from timeline)
    position = Column(Integer, default=0)
    created_at = Column(DateTime, default=lambda: datetime.now())
    updated_at = Column(DateTime, default=lambda: datetime.now(), onupdate=lambda: datetime.now())


class ImmichUser(Base):
    __tablename__ = "immich_users"

    id = Column(Integer, primary_key=True)
    label = Column(Text, nullable=False)
    api_key = Column(Text, nullable=False)  # encrypted
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=lambda: datetime.now())
    updated_at = Column(DateTime, default=lambda: datetime.now(), onupdate=lambda: datetime.now())


class InboxDirectory(Base):
    __tablename__ = "inbox_directories"

    id = Column(Integer, primary_key=True)
    path = Column(Text, nullable=False, unique=True)  # one directory belongs to exactly one set
    label = Column(Text, nullable=False)
    set_id = Column(Integer)  # FK to processing_sets.id
    # folder_tags / dry_run / use_immich / immich_user_id still exist as columns
    # in older databases. They moved to the set and are read only once, by the
    # sets migration.
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=lambda: datetime.now())
    updated_at = Column(DateTime, default=lambda: datetime.now(), onupdate=lambda: datetime.now())


class ProcessingSet(Base):
    """One bundle of input, schedule, rules, folder tags and target storage."""
    __tablename__ = "processing_sets"

    id = Column(Integer, primary_key=True)
    name = Column(Text, nullable=False)
    description = Column(Text, default="")
    active = Column(Boolean, default=True)
    position = Column(Integer, default=0)

    # Schedule — same modes and value formats the global filewatcher had
    schedule_mode = Column(Text, default="continuous")  # continuous / manual / window / scheduled
    interval_s = Column(Integer, default=5)
    window_start = Column(Text, default="22:00")
    window_end = Column(Text, default="06:00")
    scheduled_days = Column(Text, default="0,1,2,3,4")
    scheduled_time = Column(Text, default="23:00")

    # Processing
    folder_tags = Column(Boolean, default=False)
    dry_run = Column(Boolean, default=False)
    ocr_enabled = Column(Boolean, default=False)
    ocr_mode = Column(Text, default="smart")  # smart = screenshots/documents only, always = every image
    metadata_write_mode = Column(Text, default="direct")  # direct = into the file, sidecar = .xmp next to it
    google_json = Column(Boolean, default=False)  # read Google Takeout JSON sidecars

    # Target storage
    target_mode = Column(Text, default="library")  # library / immich
    library_base_path = Column(Text, default="/library")  # also hosts unknown/error/duplicate folders
    immich_user_id = Column(Integer)  # access used for uploads; NULL = global key

    # Immich auto-tagging. immich_input = the set HAS an Immich input,
    # immich_poll = that input is active. Kept apart so an input switched
    # off stays in the list, like a directory does.
    immich_input = Column(Boolean, default=False)
    immich_poll = Column(Boolean, default=False)
    immich_poll_user_id = Column(Integer)  # access polled; NULL = global key
    immich_last_poll = Column(Text)  # ISO timestamp cursor, per set

    created_at = Column(DateTime, default=lambda: datetime.now())
    updated_at = Column(DateTime, default=lambda: datetime.now(), onupdate=lambda: datetime.now())


# ---------------------------------------------------------------------------
# Data safety: ledger, copy register, references (used by fileops.py only)
# ---------------------------------------------------------------------------

class FileOp(Base):
    """Append-only ledger of every file operation.

    A row is written as 'intent' before the disk is touched and set exactly
    once to 'done', 'failed' or 'reconciled' afterwards. While a row is
    open, fileops may fill in what it learns on the way (final destination,
    hashes). Rows are never deleted; the triggers in FILE_OPS_GUARDS enforce
    both rules in the database itself.
    """
    __tablename__ = "file_ops"
    __table_args__ = (
        Index("idx_file_ops_job", "job_id", "id"),
        Index("idx_file_ops_state", "state", "created_at"),
        Index("idx_file_ops_copy", "copy_id"),
        # At most one open operation that changes or removes a given source:
        # a second one fails on insert instead of racing the first.
        Index("uq_file_ops_open_src", "src_kind", "src", unique=True,
              sqlite_where=text("state = 'intent' AND op IN "
                                "('move', 'trash', 'restore', 'purge', 'write_metadata', 'replace')")),
    )

    id = Column(Integer, primary_key=True)
    job_id = Column(Integer)  # owner job (jobs.id); NULL only for system-wide operations
    run_id = Column(Integer)  # module run that caused it (module_runs.id, later)
    op = Column(Text, nullable=False)  # move/copy/download/upload/replace/write_metadata/trash/restore/purge
    src_kind = Column(Text)  # local/work/temp/sidecar/takeout_json/immich/trash
    src = Column(Text)  # path or asset id
    dst_kind = Column(Text)
    dst = Column(Text)  # final destination; NULL until the content is placed and verified
    copy_id = Column(Integer)  # file_copies row the operation acts on
    dst_copy_id = Column(Integer)  # file_copies row that holds the result
    sha256_before = Column(Text)
    sha256_after = Column(Text)
    size = Column(Integer)
    actor = Column(Text, nullable=False)  # module code (IA-08), user action (user:keep) or system:<task>
    reason = Column(Text)  # why (trash, purge)
    state = Column(Text, nullable=False, default="intent")  # intent/done/failed/reconciled
    error = Column(Text)
    detail = Column(JSON)  # plan (dst_dir, filename, part file) and notes
    created_at = Column(DateTime, default=lambda: datetime.now())
    finished_at = Column(DateTime)


# Executed at startup (database._migrate_columns). Kept here so the schema of
# the ledger lives in one place and tests can recreate them after cleanup.
FILE_OPS_GUARDS = [
    "CREATE TRIGGER IF NOT EXISTS trg_file_ops_no_delete BEFORE DELETE ON file_ops "
    "BEGIN SELECT RAISE(ABORT, 'file_ops is append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_file_ops_finished BEFORE UPDATE ON file_ops "
    "WHEN OLD.state <> 'intent' "
    "BEGIN SELECT RAISE(ABORT, 'file_ops row is already finished'); END",
]


class FileCopy(Base):
    """One physical copy of a job's content: a file on disk or an Immich asset.

    A location (path or asset id) holds at most one 'present' row, whatever
    its kind — two jobs cannot both own the same file unnoticed. A job that
    uses a copy it does not own is listed in asset_refs.
    """
    __tablename__ = "file_copies"
    __table_args__ = (
        Index("uq_file_copies_present", "location", unique=True,
              sqlite_where=text("state = 'present'")),
        Index("idx_file_copies_job", "job_id"),
        Index("idx_file_copies_sha", "sha256"),
        Index("idx_file_copies_trash", "state", "trashed_at"),
    )

    id = Column(Integer, primary_key=True)
    job_id = Column(Integer)  # owner (jobs.id)
    kind = Column(Text, nullable=False)  # local/immich/work/temp/sidecar/takeout_json
    location = Column(Text, nullable=False)  # path, or asset id for kind immich
    sha256 = Column(Text)
    size = Column(Integer)
    checksum = Column(Text)  # Immich's own checksum (SHA-1, base64) for kind immich
    state = Column(Text, nullable=False, default="present")  # present/trashed/moved/deleted/missing
    verified_at = Column(DateTime)  # content last confirmed (hash or Immich check)
    trash_location = Column(Text)  # where a trashed copy lies (local trash)
    trashed_at = Column(DateTime)
    created_at = Column(DateTime, default=lambda: datetime.now())
    updated_at = Column(DateTime, default=lambda: datetime.now(), onupdate=lambda: datetime.now())


class AssetRef(Base):
    """A job that uses a copy it does not own.

    Mostly shared Immich assets (kind 'immich', location = asset id); also
    local files whose identical content another job already had in place.
    A copy with references from other jobs is never trashed or replaced.
    """
    __tablename__ = "asset_refs"
    __table_args__ = (
        UniqueConstraint("kind", "location", "job_id", name="uq_asset_refs"),
        Index("idx_asset_refs_job", "job_id"),
    )

    id = Column(Integer, primary_key=True)
    kind = Column(Text, nullable=False, default="immich")
    location = Column(Text, nullable=False)
    job_id = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now())

    asset_id = synonym("location")
