# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Request and response serialisation for project files (ARCH-001 §4.1)."""

# Python imports
import re

# Django imports
from django.conf import settings

# Third party imports
from rest_framework import serializers

# Module imports
from plane.db.models import FileFolder, FileLink, FileObject, FileVersion
from plane.utils.magic_bytes import normalize_mime_type
from plane.utils.object_key import OBJECT_KEY_CATEGORIES

CHECKSUM_SHA256_PATTERN = re.compile(r"\A[0-9a-fA-F]{64}\Z")


def user_payload(user):
    """Return the user snapshot returned with files and versions.

    Two different people can be involved: a file's *creator* (its first uploader,
    stored on the file row and the value the list ``uploader`` filter matches) and
    a *version's* uploader (stored per version, so a later "my uploads" view can
    tell them apart).
    """
    if user is None:
        return None

    return {
        "id": str(user.id),
        "display_name": user.display_name or user.email or user.username or "",
        "email": user.email,
    }


class FileUploadInitiateSerializer(serializers.Serializer):
    """`POST files/initiate-upload/` payload."""

    file_name = serializers.CharField(max_length=255, trim_whitespace=True)
    size_bytes = serializers.IntegerField(min_value=1)
    mime_type = serializers.CharField(max_length=127)
    folder_id = serializers.UUIDField(required=False, allow_null=True, default=None)
    category = serializers.ChoiceField(choices=sorted(OBJECT_KEY_CATEGORIES), required=False, default=None)
    file_id = serializers.UUIDField(required=False, allow_null=True, default=None)
    checksum_sha256 = serializers.CharField(
        max_length=64, required=False, allow_null=True, allow_blank=True, default=None
    )
    link = serializers.DictField(required=False, allow_null=True, default=None)

    def validate_size_bytes(self, value):
        if value > settings.PROJECT_FILE_MAX_BYTES:
            raise serializers.ValidationError(
                f"File exceeds the {settings.PROJECT_FILE_MAX_BYTES} byte limit for project files."
            )
        return value

    def validate_mime_type(self, value):
        mime_type = normalize_mime_type(value)
        if mime_type not in settings.PROJECT_FILE_MIME_TYPES:
            raise serializers.ValidationError(f"{value} is not an accepted project file type.")
        return mime_type

    def validate_checksum_sha256(self, value):
        if value in (None, ""):
            return None
        if not CHECKSUM_SHA256_PATTERN.match(value):
            raise serializers.ValidationError("checksum_sha256 must be a 64 character hex digest.")
        return value.lower()

    def validate_link(self, value):
        if value in (None, {}):
            return None

        entity_type = value.get("entity_type")
        entity_id = value.get("entity_id")

        if entity_type not in FileLink.EntityType.values:
            raise serializers.ValidationError("link.entity_type must be one of the supported entity types.")
        if not entity_id:
            raise serializers.ValidationError("link.entity_id is required.")

        try:
            entity_id = str(serializers.UUIDField().to_internal_value(entity_id))
        except serializers.ValidationError:
            raise serializers.ValidationError("link.entity_id must be a UUID.")

        return {"entity_type": entity_type, "entity_id": entity_id}


class FileUploadCompleteSerializer(serializers.Serializer):
    """`POST files/{file_id}/complete-upload/` payload."""

    version_no = serializers.IntegerField(min_value=1)
    size_bytes = serializers.IntegerField(min_value=0)
    checksum_sha256 = serializers.CharField(
        max_length=64, required=False, allow_null=True, allow_blank=True, default=None
    )

    def validate_checksum_sha256(self, value):
        if value in (None, ""):
            return None
        if not CHECKSUM_SHA256_PATTERN.match(value):
            raise serializers.ValidationError("checksum_sha256 must be a 64 character hex digest.")
        return value.lower()


class FileUploadAbortSerializer(serializers.Serializer):
    """`POST files/{file_id}/abort-upload/` payload."""

    version_no = serializers.IntegerField(min_value=1)


class FileFolderSerializer(serializers.ModelSerializer):
    """A folder in the listing response (folders never appear in object keys)."""

    class Meta:
        model = FileFolder
        fields = [
            "id",
            "name",
            "parent_id",
            "depth",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class FileVersionSerializer(serializers.ModelSerializer):
    """One stored version of a file, including the evidence finalize recorded.

    ``uploaded_by`` is the person who uploaded *this version*, which is not
    necessarily the file's creator (``file.uploader``).
    """

    uploaded_by = serializers.SerializerMethodField()

    class Meta:
        model = FileVersion
        fields = [
            "id",
            "version_no",
            "status",
            "is_active",
            "size_bytes",
            "mime_type",
            "client_checksum_sha256",
            "etag",
            "magic_bytes_checked_at",
            "uploaded_by",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_uploaded_by(self, obj):
        return user_payload(obj.uploaded_by)


class FileLinkSerializer(serializers.ModelSerializer):
    """A binding between a file and the entity that surfaces it."""

    class Meta:
        model = FileLink
        fields = [
            "id",
            "entity_type",
            "entity_id",
            "entity_identifier",
            "created_at",
        ]
        read_only_fields = fields


class FileObjectSerializer(serializers.ModelSerializer):
    """A file as the list and detail endpoints return it.

    ``link_count`` is annotated by the queryset so a listing never issues one
    query per file (R-NFR-1).

    ``uploader`` is the file's **creator** — the person who started the file —
    which is what the list endpoint's ``uploader`` filter matches. A version's
    uploader is a separate field on the version payload.
    """

    link_count = serializers.IntegerField(read_only=True)
    trashed = serializers.SerializerMethodField()
    uploader = serializers.SerializerMethodField()

    class Meta:
        model = FileObject
        fields = [
            "id",
            "name_display",
            "name_original",
            "category",
            "status",
            "visibility",
            "mime_type",
            "extension",
            "size_bytes",
            "folder_id",
            "current_version_no",
            "object_key",
            "bucket",
            "checksum_sha256",
            "is_pinned",
            "last_accessed_at",
            "link_count",
            "trashed",
            "uploader",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_trashed(self, obj):
        """Explicit markup so a client can tell a trashed file from a live one."""
        return obj.status == FileObject.Status.TRASHED

    def get_uploader(self, obj):
        return user_payload(obj.created_by)


class FileFolderWriteSerializer(serializers.Serializer):
    """`POST files/folders/` and `PATCH files/folders/{id}/` payload."""

    name = serializers.CharField(max_length=255, trim_whitespace=True, required=False)
    parent_id = serializers.UUIDField(required=False, allow_null=True, default=None)

    def validate_name(self, value):
        name = value.strip()
        if not name:
            raise serializers.ValidationError("name must not be empty.")
        return name

    def validate(self, attrs):
        if self.partial is False and not attrs.get("name"):
            raise serializers.ValidationError({"name": "name is required."})
        return attrs


class FileOperationSerializer(serializers.Serializer):
    """`PATCH files/{file_id}/` payload: rename, move and pin."""

    name_display = serializers.CharField(max_length=255, trim_whitespace=True, required=False)
    folder_id = serializers.UUIDField(required=False, allow_null=True, default=None)
    is_pinned = serializers.BooleanField(required=False)

    def validate_name_display(self, value):
        name = value.strip()
        if not name:
            raise serializers.ValidationError("name_display must not be empty.")
        return name

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError("Provide name_display, folder_id or is_pinned.")
        return attrs


class FileCopySerializer(serializers.Serializer):
    """`POST files/{file_id}/copy/` payload."""

    folder_id = serializers.UUIDField(required=False, allow_null=True, default=None)
    name_display = serializers.CharField(max_length=255, trim_whitespace=True, required=False)
    #: Cross-project copy is T-122; the field exists so the refusal is explicit.
    target_project_id = serializers.UUIDField(required=False, allow_null=True, default=None)

    def validate_name_display(self, value):
        name = value.strip()
        if not name:
            raise serializers.ValidationError("name_display must not be empty.")
        return name
