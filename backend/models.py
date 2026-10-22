from sqlalchemy import Column, Integer, Text, Boolean, DateTime, JSON, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase
from datetime import datetime, timezone


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "jobs"

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
