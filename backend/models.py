from sqlalchemy import Column, Integer, Text, Boolean, DateTime, JSON
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
    status = Column(Text, default="queued")  # queued/processing/done/error/duplicate/review
    current_step = Column(Text)  # IA-01 bis IA-11
    step_result = Column(JSON, default=dict)
    error_message = Column(Text)
    source_label = Column(Text)  # which inbox this came from
    source_inbox_path = Column(Text)  # inbox base path (for folder_tags)
    dry_run = Column(Boolean, default=False)  # dry-run mode (don't move/write)
    use_immich = Column(Boolean, default=False)  # upload to Immich instead of target directory
    immich_asset_id = Column(Text)  # source asset ID when processing via Immich webhook
    file_hash = Column(Text)  # SHA256
    phash = Column(Text)  # perceptual hash
    created_at = Column(DateTime, default=lambda: datetime.now())
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
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=lambda: datetime.now())
    updated_at = Column(DateTime, default=lambda: datetime.now(), onupdate=lambda: datetime.now())


class LibraryCategory(Base):
    __tablename__ = "library_categories"

    id = Column(Integer, primary_key=True)
    key = Column(Text, nullable=False, unique=True)  # internal key (photo, video, screenshot, ...)
    label = Column(Text, nullable=False)  # display name
    path_template = Column(Text, nullable=False)  # e.g. "photos/{YYYY}/{YYYY-MM}/"
    fixed = Column(Boolean, default=False)  # fixed categories cannot be deleted (unknown, error, duplicate)
    immich_archive = Column(Boolean, default=False)  # archive asset in Immich (hide from timeline)
    position = Column(Integer, default=0)
    created_at = Column(DateTime, default=lambda: datetime.now())
    updated_at = Column(DateTime, default=lambda: datetime.now(), onupdate=lambda: datetime.now())


class InboxDirectory(Base):
    __tablename__ = "inbox_directories"

    id = Column(Integer, primary_key=True)
    path = Column(Text, nullable=False, unique=True)
    label = Column(Text, nullable=False)
    folder_tags = Column(Boolean, default=False)
    dry_run = Column(Boolean, default=False)
    use_immich = Column(Boolean, default=False)
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=lambda: datetime.now())
    updated_at = Column(DateTime, default=lambda: datetime.now(), onupdate=lambda: datetime.now())
