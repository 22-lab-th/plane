# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import json

import pytest
from django.utils import timezone

from plane.bgtasks.page_version_task import track_page_version
from plane.db.models import Page, PageVersion
from plane.tests.factories import UserFactory, WorkspaceFactory


@pytest.mark.unit
@pytest.mark.django_db
@pytest.mark.parametrize("has_recent_version", [False, True])
def test_page_edit_creates_or_updates_version_with_all_content(has_recent_version):
    owner = UserFactory()
    workspace = WorkspaceFactory(owner=owner)
    page = Page.objects.create(
        workspace=workspace,
        owned_by=owner,
        description_html="<p>Updated page</p>",
        description_json={"type": "doc", "content": [{"type": "paragraph"}]},
        description_binary=b"updated-yjs-state",
    )
    previous = None
    if has_recent_version:
        previous = PageVersion.objects.create(
            page=page,
            workspace=workspace,
            owned_by=owner,
            description_html="<p>Previous page</p>",
            last_saved_at=timezone.now(),
        )

    track_page_version(page.id, json.dumps({"description_html": "<p>Previous page</p>"}), owner.id)

    version = PageVersion.objects.get(page=page)
    if previous:
        assert version.id == previous.id
    assert version.description_json == page.description_json
    assert version.description_html == page.description_html
    assert bytes(version.description_binary) == bytes(page.description_binary)
    assert version.description_stripped == "Updated page"
    assert version.owned_by_id == owner.id


@pytest.mark.unit
@pytest.mark.django_db
def test_unchanged_page_does_not_create_version():
    owner = UserFactory()
    page = Page.objects.create(
        workspace=WorkspaceFactory(owner=owner), owned_by=owner, description_html="<p>Unchanged</p>"
    )

    track_page_version(page.id, json.dumps({"description_html": page.description_html}), owner.id)

    assert not PageVersion.objects.filter(page=page).exists()
