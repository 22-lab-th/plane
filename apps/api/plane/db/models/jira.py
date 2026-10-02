"""Stable Jira identities and durable per-item import outcomes; tokens live in God Mode."""

import uuid
from django.conf import settings
from django.db import models


class JiraSource(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey("db.Project", on_delete=models.CASCADE)
    site_url = models.URLField(max_length=255)
    remote_project_id = models.CharField(max_length=100)
    project_key = models.CharField(max_length=100)
    project_name = models.CharField(max_length=255)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    user_mapping = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "jira_sources"
        constraints = [
            models.UniqueConstraint(fields=["project", "site_url", "remote_project_id"], name="jira_source_unique")
        ]


class JiraItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source = models.ForeignKey(JiraSource, on_delete=models.CASCADE, related_name="items")
    kind = models.CharField(max_length=16)  # issue / comment / attachment / sprint / inventory
    remote_id = models.CharField(max_length=100)
    category = models.CharField(max_length=100)
    title = models.TextField(blank=True)
    remote = models.JSONField(default=dict)
    imported_revision = models.CharField(max_length=64, blank=True)
    issue = models.ForeignKey("db.Issue", null=True, on_delete=models.SET_NULL)
    comment = models.ForeignKey("db.IssueComment", null=True, on_delete=models.SET_NULL)
    cycle = models.ForeignKey("db.Cycle", null=True, on_delete=models.SET_NULL)
    file = models.ForeignKey("db.FileObject", null=True, on_delete=models.SET_NULL)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "jira_items"
        constraints = [models.UniqueConstraint(fields=["source", "kind", "remote_id"], name="jira_item_unique")]


class JiraRun(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source = models.ForeignKey(JiraSource, on_delete=models.CASCADE, related_name="runs")
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
        db_table = "jira_runs"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["source"],
                condition=models.Q(status__in=["queued", "discovering", "running"]),
                name="jira_one_active_run",
            )
        ]


class JiraRunItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(JiraRun, on_delete=models.CASCADE, related_name="results")
    item = models.ForeignKey(JiraItem, on_delete=models.CASCADE, related_name="results")
    status = models.CharField(max_length=16, default="pending")
    revision = models.CharField(max_length=64)
    error_code = models.CharField(max_length=100, blank=True)
    error_message = models.TextField(blank=True)
    warning = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "jira_run_items"
        constraints = [models.UniqueConstraint(fields=["run", "item"], name="jira_run_item_unique")]
