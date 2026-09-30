"""Run imported attachments through the existing quota, verification and version flow."""

from django.http import HttpRequest
from plane.app.serializers.file import FileUploadInitiateSerializer
from plane.app.views.file.upload import initiate_upload, FileUploadCompleteEndpoint, FileUploadAbortEndpoint
from plane.app.views.file.versions import activate_version
from plane.db.models import FileObject, FileVersion
from plane.settings.storage import S3Storage
from plane.utils.confluence.client import ConfluenceError
from plane.utils.confluence.jobs import require_active_run


def worker_request(user, data):
    request = HttpRequest()
    request.user = user
    request.data = data
    request.META["HTTP_USER_AGENT"] = "Plane Confluence importer"
    request.META["SERVER_NAME"] = "localhost"
    request.META["SERVER_PORT"] = "80"
    return request


def response_or_error(response):
    if response.status_code >= 400:
        data = response.data
        raise ConfluenceError(data.get("code", "plane_upload_failed"), data.get("error", str(data)))
    return response.data


def store_attachment(run, item, stream, size):
    source = run.source
    existing = item.file
    if existing and (existing.deleted_at or existing.status == "trashed"):
        raise ConfluenceError(
            "destination_trashed", "The imported file is in the trash. Restore it in Files before syncing."
        )
    payload = {
        "file_name": item.title,
        "mime_type": item.remote.get("mediaType", "application/octet-stream"),
        "size_bytes": size,
        "file_id": existing.id if existing else None,
    }
    parent = source.items.filter(kind="page", remote_id=str(item.remote.get("pageId"))).first()
    if parent and parent.page_id:
        payload["link"] = {"entity_type": "page", "entity_id": str(parent.page_id)}
    serializer = FileUploadInitiateSerializer(data=payload)
    if not serializer.is_valid():
        raise ConfluenceError("upload_validation", str(serializer.errors))
    request = worker_request(run.initiated_by, serializer.validated_data)
    kwargs = {"slug": source.project.workspace.slug, "project_id": source.project_id}
    initiation = response_or_error(initiate_upload(request, **kwargs, payload=serializer.validated_data, presign=False))
    file_id = initiation["file"]["id"]
    version_no = initiation["version_no"]
    version = FileVersion.objects.get(file_id=file_id, version_no=version_no)
    # Keep the destination pointer even when storage fails, so a retry reuses this row.
    item.file_id = file_id
    item.save(update_fields=["file", "updated_at"])
    try:
        if not S3Storage().upload_file(stream, version.object_key, extra_args={}, content_type=payload["mime_type"]):
            raise ConfluenceError("storage_upload_failed", "Plane object storage refused the attachment upload.")
        require_active_run(run)
        request.data = {"version_no": version_no, "size_bytes": size}
        result = response_or_error(FileUploadCompleteEndpoint().post(request, **kwargs, file_id=file_id))
        if result["activation_required"]:
            require_active_run(run)
            version.refresh_from_db()
            # The user selected overwrite/sync. Preserve old versions while updating the same file ID.
            activate_version(
                request,
                source.project,
                FileObject.objects.get(pk=file_id),
                version,
            )
    except Exception:
        try:
            request.data = {"version_no": version_no}
            FileUploadAbortEndpoint().post(request, **kwargs, file_id=file_id)
        except Exception:
            pass  # Existing sweep handles storage objects left by failed attempts.
        raise
    item.file.refresh_from_db()
    if item.file.name_display != item.title:
        # Preserve established names: files are still the same object, even after a remote rename.
        from plane.app.views.file.base import available_display_name, stored_name
        from plane.utils.file_storage.naming import normalize_name

        display = available_display_name(
            source.project, item.file.folder, stored_name(item.title), exclude_file_id=file_id
        )
        FileObject.objects.filter(pk=file_id).update(name_display=display, name_normalized=normalize_name(display))
