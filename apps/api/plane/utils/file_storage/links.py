# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Link targets: which entities a project file may point at, and how they are checked.

A ``file_links`` row is intentionally **not** a polymorphic foreign key (ARCH-001
§2.5), so every writer has to validate the target itself. That validation lives
here, once, and is used by all three doors that create a link: the upload path
(``initiate-upload`` and ``{file_id}/versions/``), the links endpoint, and the
serializer that pre-checks a payload's shape.

Two rules come from the architecture:

* a link never crosses projects (AD-06): the target row is looked up **scoped to the
  project**, and a row from another project is refused rather than trusted;
* the readable ``entityRef`` segment a key carries is frozen at the file's creation
  (DEC-001), so nothing here rewrites a key — :func:`entity_ref_for` reads the
  segment a revision inherits.

**Product decision — what "milestone" means in this fork.** R-LINK-1 names a
milestone among the MVP link targets, and this distribution has no table called
``milestone``: it owns ``Module`` (``apps/api/plane/db/models/module.py``), which is
the milestone-shaped entity the UI works with. A ``milestone`` link is therefore
validated against a **Module row in the same project**, and the alternative spelling
``module`` is accepted as an input alias — both spellings store the model's single
value ``milestone`` on the row (``ENTITY_TYPE_ALIASES``), so no caller's vocabulary
leaks into the database and a later rename has one place to change.
``deliverable`` stays refused: it is not a target R-LINK-1 asks for and this
distribution has no row to validate it against, so accepting it would mean storing
an unverified pointer (T-101's reject-rather-than-fold posture). Do not "restore" a
hard refusal for ``milestone``/``module`` without a product decision that removes
the target from R-LINK-1 as well.
"""

# Module imports
from plane.db.models import FileLink, FileObject, Issue, IssueComment, Module, ProjectPage
from plane.utils.file_storage.errors import ProjectFileError

#: Entity types whose target row this repository can actually validate. ``milestone``
#: is validated against this fork's ``Module`` table (see the module docstring) and is
#: the only value stored for either of its spellings.
SUPPORTED_ENTITY_TYPES = (
    FileLink.EntityType.PROJECT,
    FileLink.EntityType.ISSUE,
    FileLink.EntityType.PAGE,
    FileLink.EntityType.COMMENT,
    FileLink.EntityType.MILESTONE,
)

#: Input spellings that mean an already-supported type. ``module`` is this fork's own
#: vocabulary for the milestone-shaped row; the stored value stays the model's.
ENTITY_TYPE_ALIASES = {
    "module": FileLink.EntityType.MILESTONE,
}

#: The accepted choice that cannot be validated here (documented so the refusal is
#: deliberate rather than an oversight).
UNVALIDATED_ENTITY_TYPES = (FileLink.EntityType.DELIVERABLE,)

#: The category an upload lands in when it arrives with a link (DEC-001's mapping).
CATEGORY_BY_ENTITY_TYPE = {
    FileLink.EntityType.ISSUE: FileObject.Category.ISSUES,
    FileLink.EntityType.PAGE: FileObject.Category.PAGES,
}


def normalize_entity_type(entity_type):
    """Return the value this link is *stored* as, for any accepted spelling.

    Every door that accepts a link calls this (and :func:`resolve_link` calls it
    again), so ``module`` and ``milestone`` cannot be accepted by one endpoint and
    refused by another - the shape check and the resolver share one vocabulary.
    """
    return ENTITY_TYPE_ALIASES.get(str(entity_type), entity_type)


def entity_ref_for_entity(entity_type, entity_id, entity_identifier):
    """Return the key segment a link of this type contributes (DEC-001).

    **One rule for creating and reading the segment**: a new file's key is built with
    this value and a revision inherits it through :func:`entity_ref_for`, so the
    segment is frozen at creation as DEC-001 requires.

    An issue contributes its human key (``CBUTR-11``) because that is what the
    segment is for; a page and a module contribute their id. The module's *name* is
    the row's ``entity_identifier`` (for search and display) and is deliberately not
    a segment: a module can be renamed, and a key must not change with it.
    """
    if entity_type in (FileLink.EntityType.PROJECT, FileLink.EntityType.COMMENT):
        return None

    if entity_type == FileLink.EntityType.ISSUE:
        return entity_identifier or str(entity_id)

    return str(entity_id)


def category_for_entity(entity_type):
    """Return the category a file linked to this entity type belongs in."""
    return CATEGORY_BY_ENTITY_TYPE.get(entity_type, FileObject.Category.ASSETS)


def resolve_link(project, entity_type, entity_id, *, field="link.entity_id"):
    """Validate a link target inside this project and return what a row needs.

    Returns ``{"entity_type", "entity_id", "entity_ref", "entity_identifier"}``:
    ``entity_ref`` is the readable segment DEC-001 freezes into a *new* file's key
    (``None`` for project and comment links, which carry no entity segment), and
    ``entity_identifier`` is the human snapshot the link row stores.

    Raises :class:`ProjectFileError` for a target that is not in this project, for an
    entity type this repository has no table for, or for a project link that names
    another project.
    """
    entity_type = normalize_entity_type(entity_type)

    if entity_type not in SUPPORTED_ENTITY_TYPES:
        raise ProjectFileError(
            f"{entity_type} links cannot be validated in this deployment.",
            code="unsupported_entity_type",
            status_code=400,
            field="link.entity_type",
        )

    resolved = {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "entity_ref": None,
        "entity_identifier": entity_id,
    }

    if entity_type == FileLink.EntityType.PROJECT:
        if str(project.id) != entity_id:
            raise ProjectFileError(
                "A project link must reference the project in the URL.",
                code="invalid_request",
                field=field,
            )
        resolved["entity_identifier"] = project.identifier
        return resolved

    if entity_type == FileLink.EntityType.ISSUE:
        issue = Issue.objects.filter(id=entity_id, project_id=project.id).first()
        if issue is None:
            raise ProjectFileError(
                "The linked issue does not belong to this project.",
                code="invalid_request",
                field=field,
            )
        identifier = f"{project.identifier}-{issue.sequence_id}"
        resolved["entity_identifier"] = identifier
        resolved["entity_ref"] = entity_ref_for_entity(entity_type, entity_id, identifier)
        return resolved

    if entity_type == FileLink.EntityType.PAGE:
        page = ProjectPage.objects.filter(page_id=entity_id, project_id=project.id).first()
        if page is None:
            raise ProjectFileError(
                "The linked page does not belong to this project.",
                code="invalid_request",
                field=field,
            )
        resolved["entity_ref"] = entity_ref_for_entity(entity_type, entity_id, entity_id)
        return resolved

    if entity_type == FileLink.EntityType.MILESTONE:
        module = Module.objects.filter(id=entity_id, project_id=project.id).first()
        if module is None:
            raise ProjectFileError(
                "The linked module does not belong to this project.",
                code="invalid_request",
                field=field,
            )
        resolved["entity_identifier"] = module.name[:64]
        resolved["entity_ref"] = entity_ref_for_entity(entity_type, entity_id, module.name)
        return resolved

    # The comment's own ``project_id`` is denormalised, so the parent issue is what
    # proves the comment belongs here (a row whose two disagree must not be linkable).
    comment = (
        IssueComment.objects.select_related("issue")
        .filter(id=entity_id, project_id=project.id, issue__project_id=project.id)
        .first()
    )
    if comment is None:
        raise ProjectFileError(
            "The linked comment does not belong to this project.",
            code="invalid_request",
            field=field,
        )

    return resolved


def entity_ref_for(file_object):
    """Return the entityRef a revision inherits from the file's first live link.

    The segment is frozen at the file's creation (DEC-001), so a revision keeps the
    readable entity segment of the version it succeeds, and adding or removing a link
    later never rewrites a key.
    """
    if file_object is None:
        return None

    link = FileLink.objects.filter(file_id=file_object.id).order_by("created_at").first()
    if link is None:
        return None

    # The same function that built the segment at creation reads it back, so a
    # revision cannot inherit a different segment than the version it succeeds.
    return entity_ref_for_entity(link.entity_type, link.entity_id, link.entity_identifier)
