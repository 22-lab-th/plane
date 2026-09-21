# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Project-scoped file storage models (ARCH-001 §2.2 – §2.8).

The database is the source of truth for this feature: every stored object is
named by a ``file_versions`` row, ``object_key`` is write-once and unique on
both the file and the version table (R2 offers no object versioning to recover
an overwrite), and quota counters live under one row per workspace and one row
per project so the ceiling is serialised at a single point.
"""

# Django imports
from django.db import models
from django.db.models import Q
from django.utils import timezone

# Module imports
from .base import BaseModel
from .project import ProjectBaseModel


class FileFolder(ProjectBaseModel):
    """A database-only folder. Folders never appear in an object key (AD-03)."""

    name = models.CharField(max_length=255)
    #: Case-folded, whitespace-collapsed name used for uniqueness and search.
    name_normalized = models.CharField(max_length=255)
    #: NULL marks the project root.
    parent = models.ForeignKey(
        "self",
        on_delete=models.CASCADE,
        related_name="children",
        null=True,
        blank=True,
    )
    #: Denormalised for breadcrumb limits (max 32).
    depth = models.PositiveSmallIntegerField(default=0)

    class Meta:
        verbose_name = "File Folder"
        verbose_name_plural = "File Folders"
        db_table = "file_folders"
        ordering = ("-created_at",)
        constraints = [
            # ``nulls_distinct=False`` makes the constraint cover the project root
            # (``parent_id IS NULL``), which PostgreSQL would otherwise treat as
            # always distinct (ARCH-001 §2.2; PostgreSQL 15+).
            models.UniqueConstraint(
                fields=["project", "parent", "name_normalized"],
                condition=Q(deleted_at__isnull=True),
                nulls_distinct=False,
                name="file_folder_unique_name_in_parent_when_deleted_at_null",
            ),
        ]
        indexes = [
            models.Index(fields=["project", "parent", "name_normalized"], name="file_folder_proj_parent_idx"),
        ]

    def __str__(self):
        return f"{self.name} <{self.project_id}>"


class FileObject(ProjectBaseModel):
    """A logical file. Its ``object_key`` names the active version's object."""

    class Category(models.TextChoices):
        SYSTEM = "_system", "System"
        DOCS = "docs", "Docs"
        ISSUES = "issues", "Issues"
        PAGES = "pages", "Pages"
        DELIVERABLES = "deliverables", "Deliverables"
        ASSETS = "assets", "Assets"
        IMPORTS = "imports", "Imports"
        EXPORTS = "exports", "Exports"
        ARCHIVE = "archive", "Archive"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        UPLOADED_UNVERIFIED = "uploaded_unverified", "Uploaded unverified"
        ACTIVE = "active", "Active"
        FAILED = "failed", "Failed"
        QUARANTINED = "quarantined", "Quarantined"
        ARCHIVED = "archived", "Archived"
        TRASHED = "trashed", "Trashed"
        PURGE_FAILED = "purge_failed", "Purge failed"
        PURGED = "purged", "Purged"

    class Visibility(models.TextChoices):
        #: MVP serves project members only; no public value exists yet.
        PROJECT = "project", "Project"

    #: NULL marks the project root.
    folder = models.ForeignKey(
        "db.FileFolder",
        on_delete=models.SET_NULL,
        related_name="files",
        null=True,
        blank=True,
    )
    #: Exactly as supplied by the uploader; the key uses a sanitised copy.
    name_original = models.CharField(max_length=255)
    #: The only field a rename changes.
    name_display = models.CharField(max_length=255)
    name_normalized = models.CharField(max_length=255)
    mime_type = models.CharField(max_length=127)
    extension = models.CharField(max_length=32, blank=True, default="")
    #: Active version size, denormalised for listing and quota reads.
    size_bytes = models.BigIntegerField(default=0)
    #: Client-declared and advisory only; never described as verified (AD-16).
    checksum_sha256 = models.CharField(max_length=64, null=True, blank=True)
    bucket = models.CharField(max_length=63)
    #: Canonical key of the active version; unique so an overwrite is impossible.
    object_key = models.CharField(max_length=1024, unique=True)
    category = models.CharField(max_length=32, choices=Category.choices, default=Category.ASSETS)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.PENDING)
    visibility = models.CharField(max_length=16, choices=Visibility.choices, default=Visibility.PROJECT)
    current_version_no = models.PositiveIntegerField(default=0)
    is_pinned = models.BooleanField(default=False)
    last_accessed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "File Object"
        verbose_name_plural = "File Objects"
        db_table = "file_objects"
        ordering = ("-created_at",)
        constraints = [
            # ``nulls_distinct=False`` extends this to the project root
            # (``folder_id IS NULL``), where PostgreSQL treats NULLs as distinct
            # by default (ARCH-001 §2.3; PostgreSQL 15+).
            models.UniqueConstraint(
                fields=["project", "folder", "name_normalized"],
                condition=Q(deleted_at__isnull=True) & ~Q(status="trashed"),
                nulls_distinct=False,
                name="file_object_unique_name_in_folder_when_live",
            ),
        ]
        indexes = [
            models.Index(fields=["project", "folder", "name_normalized"], name="file_obj_proj_folder_idx"),
            models.Index(fields=["project", "status", "created_at"], name="file_obj_proj_status_idx"),
            models.Index(fields=["project", "mime_type"], name="file_obj_proj_mime_idx"),
            models.Index(fields=["project", "created_by", "created_at"], name="file_obj_proj_author_idx"),
            models.Index(fields=["project", "size_bytes"], name="file_obj_proj_size_idx"),
            models.Index(fields=["workspace", "project"], name="file_obj_workspace_proj_idx"),
        ]

    def __str__(self):
        return f"{self.name_display} <{self.project_id}>"

    def reconcile_pointer(self, *, save=True):
        """Point ``object_key``/``current_version_no`` at what this file still has.

        **The contract these two columns hold** (ARCH-001 §2.3), which the quota
        recompute and the file UI both read:

        * ``object_key`` names a key **the recorded rows say is stored** - never one
          this method watched a purge or a repair remove. Reconciling is a database
          operation and never calls the store, so a deletion made outside this
          application still leaves a key named here until a job that talks to the
          store (the recheck) notices. The column is unique and not null, so it
          cannot be cleared, and it says nothing about servability.
        * ``current_version_no == 0`` means **no version is active**. That is the
          signal that ``object_key`` names a stored key rather than the active
          version's, and it is the same value a file that has never been finalised
          carries. Any non-zero value names the active version, and that version's
          row matches the key.

        A purge or a repair can leave the pair naming a version whose object was
        just deleted, so both callers reconcile it: the active version when there is
        one, otherwise the newest version whose object is still stored with
        ``current_version_no = 0``.
        """
        active = FileVersion.objects.filter(file_id=self.id, is_active=True).first()
        if active is not None:
            self.current_version_no = active.version_no
            self.object_key = active.object_key
            self.size_bytes = active.size_bytes
            self.mime_type = active.mime_type
        else:
            self.current_version_no = 0
            stored = (
                FileVersion.objects.filter(file_id=self.id, object_deleted_at__isnull=True)
                .order_by("-version_no")
                .first()
            )
            if stored is not None:
                self.object_key = stored.object_key
                self.size_bytes = stored.size_bytes
                self.mime_type = stored.mime_type

        if save:
            FileObject.all_objects.filter(pk=self.pk).update(
                current_version_no=self.current_version_no,
                object_key=self.object_key,
                size_bytes=self.size_bytes,
                mime_type=self.mime_type,
                updated_at=timezone.now(),
            )

        return active


class FileVersion(ProjectBaseModel):
    """One stored object of a file. This table names every object in the bucket."""

    class Status(models.TextChoices):
        UPLOADING = "uploading", "Uploading"
        ACTIVE = "active", "Active"
        SUPERSEDED = "superseded", "Superseded"
        FAILED = "failed", "Failed"
        PURGED = "purged", "Purged"
        PURGE_FAILED = "purge_failed", "Purge failed"

    file = models.ForeignKey("db.FileObject", on_delete=models.CASCADE, related_name="versions")
    #: 1-based; increments only after a verified upload.
    version_no = models.PositiveIntegerField()
    #: Immutable per version and unique: R2 cannot recover an overwrite.
    object_key = models.CharField(max_length=1024, unique=True)
    bucket = models.CharField(max_length=63)
    size_bytes = models.BigIntegerField(default=0)
    mime_type = models.CharField(max_length=127)
    #: Declared by the client, stored as advisory only (AD-16).
    client_checksum_sha256 = models.CharField(max_length=64, null=True, blank=True)
    #: Filled by the optional phase-2 full-verification job.
    server_checksum_sha256 = models.CharField(max_length=64, null=True, blank=True)
    #: Server-observed at finalize (HEAD).
    etag = models.CharField(max_length=255, null=True, blank=True)
    #: Records the ranged-GET magic-byte validation.
    magic_bytes_checked_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.UPLOADING)
    #: Exactly one active version per file, enforced by a partial unique index.
    is_active = models.BooleanField(default=False)
    #: HEAD response snapshot (existing repository pattern).
    storage_metadata = models.JSONField(default=dict)
    uploaded_by = models.ForeignKey(
        "db.User",
        on_delete=models.SET_NULL,
        related_name="uploaded_file_versions",
        null=True,
        blank=True,
    )
    #: The quota amount this attempt reserved, stored so settlement is exact.
    reserved_bytes = models.BigIntegerField(default=0)
    #: When the reservation stops being valid (upload URL TTL from creation).
    reservation_expires_at = models.DateTimeField(null=True, blank=True)
    #: Set when this version's object was deleted, so a row is never swept twice.
    object_deleted_at = models.DateTimeField(null=True, blank=True)
    #: Set at creation and moved by :meth:`mark_status` whenever ``status``
    #: changes; the sweep's age guard reads this field (ARCH-001 §2.4).
    status_changed_at = models.DateTimeField(default=timezone.now)
    #: Single-fire release guard: counters are decremented only by the actor
    #: whose conditional update matched this row.
    reservation_released_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "File Version"
        verbose_name_plural = "File Versions"
        db_table = "file_versions"
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(fields=["file", "version_no"], name="file_version_unique_file_version_no"),
            models.UniqueConstraint(
                fields=["file"],
                condition=Q(is_active=True),
                name="file_version_unique_active_per_file",
            ),
        ]
        indexes = [
            models.Index(fields=["file", "is_active"], name="file_version_file_active_idx"),
            models.Index(fields=["status", "created_at"], name="file_version_status_date_idx"),
        ]

    def __str__(self):
        return f"{self.file_id} v{self.version_no}"

    def mark_status(self, new_status, save=True):
        """Move this version to ``new_status`` and stamp ``status_changed_at``.

        The cleanup sweep's age guard reads ``status_changed_at``, so every
        status transition has to move it; this method is the single place that
        does both (ARCH-001 §2.4, R3-01). A bulk ``queryset.update()`` bypasses
        the model layer and therefore leaves the stamp stale — it must never be
        used to change ``status``. Which transitions are legal is T-102's job;
        here the value only has to be one of the model's choices.
        """
        if new_status not in self.Status.values:
            raise ValueError(f"unknown file version status: {new_status!r}")

        self.status = new_status
        self.status_changed_at = timezone.now()
        if save:
            self.save(update_fields=["status", "status_changed_at", "updated_at"])
        return self.status


class FileLink(ProjectBaseModel):
    """A binding between a file and the entity that surfaces it."""

    class EntityType(models.TextChoices):
        PROJECT = "project", "Project"
        ISSUE = "issue", "Issue"
        PAGE = "page", "Page"
        COMMENT = "comment", "Comment"
        MILESTONE = "milestone", "Milestone"
        DELIVERABLE = "deliverable", "Deliverable"

    file = models.ForeignKey("db.FileObject", on_delete=models.CASCADE, related_name="links")
    entity_type = models.CharField(max_length=24, choices=EntityType.choices)
    #: Intentionally not a polymorphic FK; every write validates the target row
    #: inside the same transaction (ARCH-001 §2.5).
    entity_id = models.UUIDField()
    #: Human snapshot (for example ``CBUTR-11``) used for search and the key's
    #: ``entityRef`` segment.
    entity_identifier = models.CharField(max_length=64, null=True, blank=True)

    class Meta:
        verbose_name = "File Link"
        verbose_name_plural = "File Links"
        db_table = "file_links"
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["file", "entity_type", "entity_id"],
                condition=Q(deleted_at__isnull=True),
                name="file_link_unique_file_entity_when_deleted_at_null",
            ),
        ]
        indexes = [
            models.Index(fields=["project", "entity_type", "entity_id"], name="file_link_proj_entity_idx"),
            models.Index(fields=["file"], name="file_link_file_idx"),
        ]

    def __str__(self):
        return f"{self.entity_type}:{self.entity_id} <{self.file_id}>"


class FileAccessLog(BaseModel):
    """Append-only audit row. Survives the file it describes (AD-08, R-NFR-9)."""

    class Action(models.TextChoices):
        UPLOAD_INITIATED = "upload_initiated", "Upload initiated"
        UPLOAD_COMPLETED = "upload_completed", "Upload completed"
        UPLOAD_FAILED = "upload_failed", "Upload failed"
        VERSION_CREATED = "version_created", "Version created"
        VERSION_ACTIVATED = "version_activated", "Version activated"
        DOWNLOADED = "downloaded", "Downloaded"
        PREVIEWED = "previewed", "Previewed"
        RENAMED = "renamed", "Renamed"
        MOVED = "moved", "Moved"
        COPIED = "copied", "Copied"
        LINKED = "linked", "Linked"
        UNLINKED = "unlinked", "Unlinked"
        TRASHED = "trashed", "Trashed"
        RESTORED = "restored", "Restored"
        PURGED = "purged", "Purged"
        #: Written by the scheduled masking run itself (R-NFR-13), never by a request.
        PII_MASKED = "pii_masked", "Personal data masked"
        PERMISSION_DENIED = "permission_denied", "Permission denied"
        QUOTA_REJECTED = "quota_rejected", "Quota rejected"
        FOLDER_CREATED = "folder_created", "Folder created"
        FOLDER_RENAMED = "folder_renamed", "Folder renamed"
        FOLDER_MOVED = "folder_moved", "Folder moved"
        FOLDER_DELETED = "folder_deleted", "Folder deleted"

    workspace = models.ForeignKey("db.Workspace", on_delete=models.CASCADE, related_name="file_access_logs")
    #: Nullable and SET_NULL: the audit row outlives its project.
    project = models.ForeignKey(
        "db.Project",
        on_delete=models.SET_NULL,
        related_name="project_file_access_logs",
        null=True,
        blank=True,
    )
    #: No cascade FK to the file: a purge must not delete its own audit trail.
    file_id = models.UUIDField(null=True, blank=True)
    #: Readable history after the file row is gone.
    file_name_snapshot = models.CharField(max_length=255)
    version_no = models.IntegerField(null=True, blank=True)
    action = models.CharField(max_length=32, choices=Action.choices)
    actor = models.ForeignKey(
        "db.User",
        on_delete=models.SET_NULL,
        related_name="file_access_logs",
        null=True,
        blank=True,
    )
    actor_display = models.CharField(max_length=255, blank=True, default="")
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=512, blank=True, default="")
    #: Extra context (folder, target entity, reason). Never a presigned URL.
    metadata = models.JSONField(default=dict)

    class Meta:
        verbose_name = "File Access Log"
        verbose_name_plural = "File Access Logs"
        db_table = "file_access_logs"
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["project", "created_at"], name="file_log_proj_created_idx"),
            models.Index(fields=["file_id", "created_at"], name="file_log_file_created_idx"),
            models.Index(fields=["actor", "created_at"], name="file_log_actor_created_idx"),
            models.Index(fields=["action", "created_at"], name="file_log_action_created_idx"),
        ]

    def __str__(self):
        return f"{self.action} <{self.file_id}>"


class FileJob(ProjectBaseModel):
    """One unit of asynchronous work for a file or a version."""

    class JobType(models.TextChoices):
        VERIFY = "verify", "Verify"
        THUMBNAIL = "thumbnail", "Thumbnail"
        SCAN = "scan", "Scan"
        EXTRACT = "extract", "Extract"
        PURGE = "purge", "Purge"
        USAGE_RECOMPUTE = "usage_recompute", "Usage recompute"

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped"

    file = models.ForeignKey("db.FileObject", on_delete=models.CASCADE, related_name="jobs")
    version = models.ForeignKey(
        "db.FileVersion",
        on_delete=models.CASCADE,
        related_name="jobs",
        null=True,
        blank=True,
    )
    job_type = models.CharField(max_length=24, choices=JobType.choices)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.QUEUED)
    #: Bounded retries.
    attempts = models.PositiveSmallIntegerField(default=0)
    celery_task_id = models.CharField(max_length=255, null=True, blank=True)
    result = models.JSONField(default=dict, null=True, blank=True)
    error = models.TextField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "File Job"
        verbose_name_plural = "File Jobs"
        db_table = "file_jobs"
        ordering = ("-created_at",)
        constraints = [
            # One verification job per version: a retried finalize must not queue
            # duplicate work (ARCH-001 §2.7). Scoped to ``verify`` so a failed
            # thumbnail/scan/extract job can be queued again.
            models.UniqueConstraint(
                fields=["version", "job_type"],
                condition=Q(job_type="verify"),
                name="file_job_unique_verify_per_version",
            ),
            # Purge is keyed by file.
            models.UniqueConstraint(
                fields=["file", "job_type"],
                condition=Q(job_type="purge"),
                name="file_job_unique_file_purge",
            ),
        ]

    def __str__(self):
        return f"{self.job_type} <{self.file_id}>"


class StorageQuota(BaseModel):
    """Workspace storage ceiling and counters.

    The workspace row carries the ceiling counters because the limit is
    workspace-level: a per-project lock could not serialise racers that arrive
    through two different projects (ARCH-001 §2.8, R-QUOTA-2).
    """

    workspace = models.OneToOneField("db.Workspace", on_delete=models.CASCADE, related_name="storage_quota")
    #: NULL means unlimited.
    limit_bytes = models.BigIntegerField(null=True, blank=True)
    used_bytes = models.BigIntegerField(default=0)
    reserved_bytes = models.BigIntegerField(default=0)
    warn_threshold_pct = models.PositiveSmallIntegerField(default=80)
    enforce = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Storage Quota"
        verbose_name_plural = "Storage Quotas"
        db_table = "storage_quotas"
        ordering = ("-created_at",)

    def __str__(self):
        return f"quota <{self.workspace_id}>"


class ProjectStorageUsage(BaseModel):
    """Per-project storage counters and an optional project-level ceiling."""

    project = models.OneToOneField("db.Project", on_delete=models.CASCADE, related_name="storage_usage")
    #: NULL by default, in which case only the workspace ceiling applies.
    limit_bytes = models.BigIntegerField(null=True, blank=True)
    used_bytes = models.BigIntegerField(default=0)
    reserved_bytes = models.BigIntegerField(default=0)
    version_count = models.PositiveIntegerField(default=0)
    file_count = models.PositiveIntegerField(default=0)
    recomputed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Project Storage Usage"
        verbose_name_plural = "Project Storage Usage"
        db_table = "project_storage_usage"
        ordering = ("-created_at",)

    def __str__(self):
        return f"usage <{self.project_id}>"
