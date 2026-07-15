# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("db", "0121_alter_estimate_type"),
    ]

    operations = [
        migrations.AddField(
            model_name="page",
            name="node_type",
            field=models.CharField(
                choices=[("page", "Page"), ("folder", "Folder")],
                default="page",
                max_length=10,
            ),
        ),
    ]
