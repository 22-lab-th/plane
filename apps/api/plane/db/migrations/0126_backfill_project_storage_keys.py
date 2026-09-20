# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Backfill ``Project.storage_key`` for projects that predate project files.

``storage_key`` is the readable, immutable project prefix of every project-file
object key (DEC-001), so it has to exist before the first object is written. The
derivation is imported from :mod:`plane.utils.object_key` — the same pure
function the model uses on first use — so the backfill and the runtime path can
never drift apart.
"""

# Django imports
from django.db import migrations

# Module imports
from plane.utils.object_key import build_project_storage_key


def backfill_project_storage_keys(apps, schema_editor):
    Project = apps.get_model("db", "Project")

    # Soft-deleted projects keep their keys: their objects may still exist in the
    # bucket, and the unique constraint spans every row.
    taken = set(Project.objects.exclude(storage_key__isnull=True).values_list("storage_key", flat=True))

    for project in Project.objects.filter(storage_key__isnull=True).order_by("created_at").iterator():
        storage_key = build_project_storage_key(project.identifier, project.name, taken)
        taken.add(storage_key)
        Project.objects.filter(pk=project.pk).update(storage_key=storage_key)


class Migration(migrations.Migration):
    dependencies = [
        ("db", "0125_project_file_storage_foundation"),
    ]

    operations = [
        # No reverse: the key is derivable and the schema migration drops the
        # column anyway, so reversing this would only discard information.
        migrations.RunPython(backfill_project_storage_keys, reverse_code=migrations.RunPython.noop),
    ]
