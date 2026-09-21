# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only

import uuid

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("db", "0129_alter_fileaccesslog_action")]

    operations = [
        migrations.CreateModel(
            name="FileCopyCleanup",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("object_key", models.CharField(max_length=1024, unique=True)),
                ("bucket", models.CharField(max_length=63)),
                ("provider", models.CharField(max_length=16)),
                ("endpoint_url", models.TextField(blank=True, default="")),
                ("next_cleanup_at", models.DateTimeField(db_index=True)),
                ("last_deleted_at", models.DateTimeField(null=True)),
            ],
            options={"db_table": "file_copy_cleanup"},
        ),
    ]
