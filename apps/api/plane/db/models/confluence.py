"""Durable Confluence mappings and per-run outcomes; credentials live in God Mode."""

import uuid
from django.conf import settings
from django.db import models


class ConfluenceSource(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey("db.Project", on_delete=models.CASCADE)
    site_url = models.URLField(max_length=255)
    space_id = models.CharField(max_length=100)
    space_name = models.CharField(max_length=255, blank=True)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    access = models.PositiveSmallIntegerField(default=0)
    parent = models.ForeignKey("db.Page", null=True, on_delete=models.SET_NULL, related_name="confluence_destinations")
    root = models.ForeignKey("db.Page", null=True, on_delete=models.SET_NULL, related_name="confluence_sources")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "confluence_sources"
        constraints = [
            models.UniqueConstraint(fields=["project", "site_url", "space_id"], name="confluence_source_unique")
        ]


class ConfluenceItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source = models.ForeignKey(ConfluenceSource, on_delete=models.CASCADE, related_name="items")
    kind = models.CharField(max_length=16)  # page / attachment
    remote_id = models.CharField(max_length=100)
    category = models.CharField(max_length=100, default="page")
    title = models.TextField(blank=True)
    remote = models.JSONField(default=dict)
    imported_version = models.PositiveIntegerField(default=0)
    page = models.ForeignKey("db.Page", null=True, on_delete=models.SET_NULL, related_name="confluence_items")
    folder = models.ForeignKey("db.Page", null=True, on_delete=models.SET_NULL, related_name="confluence_item_folders")
    file = models.ForeignKey("db.FileObject", null=True, on_delete=models.SET_NULL)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "confluence_items"
        constraints = [models.UniqueConstraint(fields=["source", "kind", "remote_id"], name="confluence_item_unique")]


class ConfluenceRun(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source = models.ForeignKey(ConfluenceSource, on_delete=models.CASCADE, related_name="runs")
    initiated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    mode = models.CharField(max_length=16, default="changed")
    selection = models.JSONField(default=list)
    status = models.CharField(max_length=20, default="queued")
    phase = models.TextField(default="Waiting for worker")
    inventory_complete = models.BooleanField(default=False)
    error_code = models.CharField(max_length=100, blank=True)
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    finished_at = models.DateTimeField(null=True)

    class Meta:
        db_table = "confluence_runs"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["source"],
                condition=models.Q(status__in=["queued", "discovering", "running"]),
                name="confluence_one_active_run",
            )
        ]


class ConfluenceRunItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(ConfluenceRun, on_delete=models.CASCADE, related_name="results")
    item = models.ForeignKey(ConfluenceItem, on_delete=models.CASCADE, related_name="results")
    status = models.CharField(max_length=16, default="pending")
    version = models.PositiveIntegerField(default=0)
    error_code = models.CharField(max_length=100, blank=True)
    error_message = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "confluence_run_items"
        constraints = [models.UniqueConstraint(fields=["run", "item"], name="confluence_run_item_unique")]
