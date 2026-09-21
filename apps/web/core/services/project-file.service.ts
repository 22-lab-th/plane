/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// plane imports
import { API_BASE_URL } from "@plane/constants";
// services
import { APIService } from "@/services/api.service";

/**
 * Sort keys the listing endpoint accepts. Mirrors `ORDERINGS` in
 * `apps/api/plane/app/views/file/listing.py` — a value outside this list is
 * refused by the API with a 400, so the view must only ever send these.
 */
export const PROJECT_FILE_ORDERINGS = [
  "name",
  "-name",
  "size",
  "-size",
  "created",
  "-created",
  "updated",
  "-updated",
] as const;

export type TProjectFileOrdering = (typeof PROJECT_FILE_ORDERINGS)[number];

/** The sort key the listing applies when the caller does not ask for one. */
export const PROJECT_FILE_DEFAULT_ORDERING: TProjectFileOrdering = "-created";

export const isProjectFileOrdering = (value: string | null): value is TProjectFileOrdering =>
  value !== null && (PROJECT_FILE_ORDERINGS as readonly string[]).includes(value);

/** The user snapshot the file and version payloads carry. */
export interface IProjectFileUser {
  id: string | null;
  display_name: string | null;
}

/** A file as the list endpoint returns it. */
export interface IProjectFile {
  id: string;
  name_display: string;
  name_original: string;
  category: string;
  status: string;
  visibility: string;
  mime_type: string;
  extension: string;
  size_bytes: number;
  folder_id: string | null;
  current_version_no: number | null;
  object_key: string;
  bucket: string;
  checksum_sha256: string;
  is_pinned: boolean;
  last_accessed_at: string | null;
  link_count: number;
  trashed: boolean;
  uploader: IProjectFileUser;
  created_at: string;
  updated_at: string;
}

/** A folder in the listing response. */
export interface IProjectFolder {
  id: string;
  name: string;
  parent_id: string | null;
  depth: number;
  created_at: string;
  updated_at: string;
}

/** One step of the path from the project root down to the browsed folder. */
export interface IProjectFileBreadcrumb {
  id: string;
  name: string;
  depth: number;
}

/** The storage block the listing carries, sourced from the same response as the rows. */
export interface IProjectFileStorage {
  project_used_bytes: number;
  workspace_used_bytes: number;
  limit_bytes: number;
  warn_threshold_pct: number;
  file_count: number;
  version_count: number;
}

/** The cursor page block of the listing response. */
export interface IProjectFilePage {
  next_cursor: string | null;
  prev_cursor: string | null;
  cursor: string;
  page_count: number;
  total_results: number;
  total_pages: number;
  next_page_results: number;
  prev_page_results: number;
}

export interface IProjectFileListResponse {
  results: IProjectFile[];
  folders: IProjectFolder[];
  breadcrumbs: IProjectFileBreadcrumb[];
  page: IProjectFilePage;
  storage: IProjectFileStorage;
}

/** The caller's affordances for one file, as the detail endpoint reports them. */
export interface IProjectFilePermissions {
  can_edit: boolean;
  can_delete: boolean;
  can_download: boolean;
}

export interface IProjectFileVersion {
  id: string;
  version_no: number;
  status: string;
  is_active: boolean;
  can_activate: boolean;
  size_bytes: number;
  mime_type: string;
  client_checksum_sha256: string;
  etag: string;
  magic_bytes_checked_at: string | null;
  uploaded_by: IProjectFileUser;
  created_at: string;
  updated_at: string;
}

export interface IProjectFileLink {
  id: string;
  entity_type: string;
  entity_id: string;
  entity_identifier: string;
  created_at: string;
}

export interface IProjectFileActivity {
  id: string;
  action: string;
  actor: IProjectFileUser;
  actor_display: string | null;
  file_id: string;
  file_name_snapshot: string | null;
  version_no: number | null;
  created_at: string;
}

/** The detail payload: the file, its versions, links, permissions and own history. */
export interface IProjectFileDetail {
  file: IProjectFile;
  version: IProjectFileVersion | null;
  versions: IProjectFileVersion[];
  links: IProjectFileLink[];
  link_count: number;
  permissions: IProjectFilePermissions;
  activity: IProjectFileActivity[];
}

/** One file an entity surfaces, with the link a surface unlinks by (R-LINK-2, AC-16). */
export interface IProjectFileEntityLink {
  link: IProjectFileLink;
  file: IProjectFile;
}

/** `GET files/links/` response: the entity -> file direction, newest link first. */
export interface IProjectFileEntityLinksResponse {
  results: IProjectFileEntityLink[];
}

/** The entity an upload or a lookup is attached to. */
export type TProjectFileEntityRef = {
  entity_type: "issue" | "page" | "comment" | "milestone";
  entity_id: string;
};

/**
 * How an editor document refers to a project file (R-LINK-3).
 *
 * The editor stores whatever `upload` returns as the image node's `src` and resolves
 * it later through `getAssetSrc`, so the value has to be distinguishable from a
 * legacy asset id (a bare UUID) without being a URL: a presigned URL expires, and
 * DESIGN §8 forbids leaving one in markup that outlives its TTL. The prefix is that
 * marker, and the id after it is the same file id the Files view lists.
 */
export const PROJECT_FILE_REF_PREFIX = "project-file:";

/** A stored reference to a project file, as an editor document holds it. */
export type TProjectFileRef = `${typeof PROJECT_FILE_REF_PREFIX}${string}`;

export const isProjectFileRef = (value: string): value is TProjectFileRef => value.startsWith(PROJECT_FILE_REF_PREFIX);

/** The file summary the upload endpoints return (ARCH-001 §4.1). */
export interface IProjectFileUploadFile {
  id: string;
  name_display: string;
  category: string;
  object_key: string;
  folder_id: string | null;
}

/** The presigned PUT the initiate endpoint signs for the exact key and content type. */
export interface IProjectFilePresignedUpload {
  url: string;
  method: string;
  headers: Record<string, string>;
  expires_at: string;
}

/** `POST files/initiate-upload/` response: the pending file, its version and the URL. */
export interface IProjectFileUploadInitiation {
  file: IProjectFileUploadFile;
  version_no: number;
  upload: IProjectFilePresignedUpload;
}

/** The version summary finalize returns; the checksum stays advisory. */
export interface IProjectFileUploadVersion {
  version_no: number;
  size_bytes: number;
  checksum_sha256: string | null;
  etag: string | null;
  status: string;
}

/**
 * `POST files/{id}/complete-upload/` response. `activation_required` is true when
 * the stored version is not active — a revision never becomes active without the
 * user confirming it (AD-18, AC-43).
 */
export interface IProjectFileUploadCompletion {
  file: IProjectFileUploadFile;
  version: IProjectFileUploadVersion;
  activation_required: boolean;
  storage_usage: { project_used_bytes: number; limit_bytes: number };
}

/**
 * A presigned GET as the delivery endpoints return it (ARCH-001 §4.3).
 *
 * `disposition` is the decision the server signed, not a suggestion: it is
 * `inline` only for the inert allowlist, and every script-capable type - SVG and
 * HTML above all - is signed `attachment` (AD-07, AC-08). The URL is short-lived,
 * so it is never cached across an open.
 */
export interface IProjectFileAccessUrl {
  url: string;
  expires_at: string;
  disposition: string;
  file_name: string;
  version_no: number;
}

/** `POST files/{id}/versions/{n}/activate/` response: the swap and what it demoted. */
export interface IProjectFileVersionActivation {
  file: IProjectFile;
  version: IProjectFileVersion;
  previous_version_no: number | null;
  /** False for a repeated activation: the end state was already the requested one. */
  activated: boolean;
  /** True when a version the rows called purged was found in the store and put back. */
  repaired: boolean;
}

/** `POST files/{id}/restore/` response, including where the file landed. */
export interface IProjectFileRestoreResult {
  file: IProjectFile;
  restore: {
    restored_links: number;
    /** True when the original folder was gone and the file landed at the project root. */
    folder_fallback: boolean;
  };
}

/** The list query parameters the endpoint documents. */
export type TProjectFileListQuery = {
  /**
   * The folder to browse: a folder UUID, or `"root"` for the project root's own
   * files. An *absent* `folder_id` is not the root - the endpoint then returns every
   * file in the project at any depth, which is why `buildListQuery` always sends one.
   */
  folder_id?: string;
  q?: string;
  ordering?: TProjectFileOrdering;
  cursor?: string;
  page_size?: number;
  pinned?: boolean;
  trashed?: boolean;
  /** ISO date or datetime lower bound on `created_at`. */
  created_from?: string;
};

export class ProjectFileService extends APIService {
  constructor() {
    super(API_BASE_URL);
  }

  private listQueryString(query: TProjectFileListQuery): string {
    const params = new URLSearchParams();
    if (query.folder_id) params.set("folder_id", query.folder_id);
    if (query.q) params.set("q", query.q);
    if (query.ordering) params.set("ordering", query.ordering);
    if (query.cursor) params.set("cursor", query.cursor);
    if (query.page_size !== undefined) params.set("page_size", String(query.page_size));
    if (query.pinned !== undefined) params.set("pinned", String(query.pinned));
    if (query.trashed !== undefined) params.set("trashed", String(query.trashed));
    if (query.created_from) params.set("created_from", query.created_from);
    const serialised = params.toString();
    return serialised ? `?${serialised}` : "";
  }

  async listProjectFiles(
    workspaceSlug: string,
    projectId: string,
    query: TProjectFileListQuery = {}
  ): Promise<IProjectFileListResponse> {
    return this.get(`/api/workspaces/${workspaceSlug}/projects/${projectId}/files/${this.listQueryString(query)}`)
      .then((response) => response?.data)
      .catch((error) => {
        // A transport-level failure has no `response`, and the view keys its error
        // surface off the thrown value, so the raw error is the fallback.
        throw error?.response?.data ?? error;
      });
  }

  async getProjectStorage(workspaceSlug: string, projectId: string): Promise<IProjectFileStorage> {
    return this.get(`/api/workspaces/${workspaceSlug}/projects/${projectId}/files/storage/`)
      .then((response) => response?.data)
      .catch((error) => {
        // A transport-level failure has no `response`, and the view keys its error
        // surface off the thrown value, so the raw error is the fallback.
        throw error?.response?.data ?? error;
      });
  }

  async getProjectFile(
    workspaceSlug: string,
    projectId: string,
    fileId: string,
    options: { trashed?: boolean } = {}
  ): Promise<IProjectFileDetail> {
    const query = options.trashed ? "?trashed=true" : "";
    return this.get(`/api/workspaces/${workspaceSlug}/projects/${projectId}/files/${fileId}/${query}`)
      .then((response) => response?.data)
      .catch((error) => {
        // A transport-level failure has no `response`, and the view keys its error
        // surface off the thrown value, so the raw error is the fallback.
        throw error?.response?.data ?? error;
      });
  }

  /**
   * Sign a short-lived GET for the preview area (R-DL-2, AC-08).
   *
   * Called on every open rather than reusing a stored URL: the URL expires, and a
   * cached one would either break the preview or outlive its TTL in the DOM
   * (DESIGN §8). `versionNo` previews a version without moving the active pointer.
   */
  async getProjectFilePreviewUrl(
    workspaceSlug: string,
    projectId: string,
    fileId: string,
    options: { versionNo?: number | null } = {}
  ): Promise<IProjectFileAccessUrl> {
    return this.get(
      `/api/workspaces/${workspaceSlug}/projects/${projectId}/files/${fileId}/preview/${this.versionQuery(options)}`
    )
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data ?? error;
      });
  }

  /** Sign a short-lived GET forced to `attachment` for the Download action (R-DL-1). */
  async getProjectFileDownloadUrl(
    workspaceSlug: string,
    projectId: string,
    fileId: string,
    options: { versionNo?: number | null } = {}
  ): Promise<IProjectFileAccessUrl> {
    return this.get(
      `/api/workspaces/${workspaceSlug}/projects/${projectId}/files/${fileId}/download/${this.versionQuery(options)}`
    )
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data ?? error;
      });
  }

  /**
   * Rename, move within the project, or pin the file (AC-06, AC-10).
   *
   * Metadata only: `name_original`, the object key and every stored version stay as
   * they are. A name the destination folder already holds is refused with a 409
   * rather than suffixed, because here the user chose the name.
   */
  async updateProjectFile(
    workspaceSlug: string,
    projectId: string,
    fileId: string,
    payload: { name_display?: string; folder_id?: string | null; is_pinned?: boolean }
  ): Promise<IProjectFile> {
    return this.patch(`/api/workspaces/${workspaceSlug}/projects/${projectId}/files/${fileId}/`, payload)
      .then((response) => response?.data?.file)
      .catch((error) => {
        throw error?.response?.data ?? error;
      });
  }

  /** Bring a trashed file back, reviving the links whose entities are still live (AC-11). */
  async restoreProjectFile(
    workspaceSlug: string,
    projectId: string,
    fileId: string
  ): Promise<IProjectFileRestoreResult> {
    return this.post(`/api/workspaces/${workspaceSlug}/projects/${projectId}/files/${fileId}/restore/`)
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data ?? error;
      });
  }

  /**
   * Delete the file and every version object for good (AC-12, AC-27).
   *
   * The API refuses the call unless it carries the explicit confirmation, so the
   * query parameter is the request's own proof that a human was asked.
   */
  async purgeProjectFile(workspaceSlug: string, projectId: string, fileId: string): Promise<void> {
    return this.delete(`/api/workspaces/${workspaceSlug}/projects/${projectId}/files/${fileId}/purge/?confirm=true`)
      .then(() => undefined)
      .catch((error) => {
        throw error?.response?.data ?? error;
      });
  }

  /**
   * Move the active pointer to one stored version (AD-18, AC-43).
   *
   * The only call that changes which version preview and download follow; a revision
   * upload never does this on its own.
   */
  async activateProjectFileVersion(
    workspaceSlug: string,
    projectId: string,
    fileId: string,
    versionNo: number
  ): Promise<IProjectFileVersionActivation> {
    return this.post(
      `/api/workspaces/${workspaceSlug}/projects/${projectId}/files/${fileId}/versions/${versionNo}/activate/`
    )
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data ?? error;
      });
  }

  /** `?version=` for the delivery endpoints; absent means the active version. */
  private versionQuery(options: { versionNo?: number | null }): string {
    return options.versionNo ? `?version=${options.versionNo}` : "";
  }

  /**
   * The files this project surfaces for one entity (R-LINK-2, AC-16).
   *
   * The entity → file direction: an issue's attachments and a page's embeds are the
   * same rows the Files view lists, so a surface reads them here rather than keeping
   * a second copy of the file. Each row carries its link, which is what an unlink
   * needs, and the file, which is what the row prints.
   */
  async listEntityFileLinks(
    workspaceSlug: string,
    projectId: string,
    entity: TProjectFileEntityRef
  ): Promise<IProjectFileEntityLink[]> {
    const params = new URLSearchParams({ entity_type: entity.entity_type, entity_id: entity.entity_id });
    return this.get(`/api/workspaces/${workspaceSlug}/projects/${projectId}/files/links/?${params.toString()}`)
      .then((response) => response?.data?.results ?? [])
      .catch((error) => {
        throw error?.response?.data ?? error;
      });
  }

  /**
   * Attach an existing file to an entity (R-LINK-1).
   *
   * A row, never a copy: the file keeps its id, its key and its bytes, and the
   * entity starts showing it in the place its surface renders (the issue's
   * attachments, the page's embeds).
   */
  async linkProjectFile(
    workspaceSlug: string,
    projectId: string,
    fileId: string,
    entity: TProjectFileEntityRef
  ): Promise<IProjectFileLink> {
    return this.post(`/api/workspaces/${workspaceSlug}/projects/${projectId}/files/${fileId}/links/`, entity)
      .then((response) => response?.data?.link)
      .catch((error) => {
        throw error?.response?.data ?? error;
      });
  }

  /**
   * Detach one entity from a file without touching the file (AC-21, AC-16).
   *
   * A row operation by design: the object is shared by every link, so removing the
   * last one leaves the file listed and downloadable rather than deleted.
   */
  async unlinkProjectFile(workspaceSlug: string, projectId: string, fileId: string, linkId: string): Promise<void> {
    return this.delete(`/api/workspaces/${workspaceSlug}/projects/${projectId}/files/${fileId}/links/${linkId}/`)
      .then(() => undefined)
      .catch((error) => {
        throw error?.response?.data ?? error;
      });
  }

  /**
   * Duplicate a file inside this project (AC-10).
   *
   * The copy gets a new file id and its own objects; the source is untouched. Used
   * when a page's embedded image is duplicated inside the editor, so the copy is a
   * project file like the original rather than a second reference to its bytes.
   */
  async copyProjectFile(
    workspaceSlug: string,
    projectId: string,
    fileId: string,
    payload: { name_display?: string; folder_id?: string | null } = {}
  ): Promise<IProjectFile> {
    return this.post(`/api/workspaces/${workspaceSlug}/projects/${projectId}/files/${fileId}/copy/`, payload)
      .then((response) => response?.data?.file)
      .catch((error) => {
        throw error?.response?.data ?? error;
      });
  }

  /**
   * Reserve quota and get the presigned PUT for one file (R-UPL-1).
   *
   * `file_id` is what makes an upload a **retry** (the same file row, a fresh
   * version) or a replacement (the id of the file the user chose to replace):
   * without it the server creates a new file row and suffixes the display name
   * when that name is taken in the folder.
   */
  async initiateFileUpload(
    workspaceSlug: string,
    projectId: string,
    payload: {
      file_name: string;
      size_bytes: number;
      mime_type: string;
      folder_id?: string | null;
      file_id?: string | null;
      checksum_sha256?: string | null;
      /**
       * The entity the new file is attached to (R-LINK-1). The server validates the
       * target inside the transaction that writes the link, and derives the file's
       * category from it, so an attachment is one upload rather than an upload plus
       * a second call that could fail on its own.
       */
      link?: TProjectFileEntityRef | null;
    }
  ): Promise<IProjectFileUploadInitiation> {
    return this.post(`/api/workspaces/${workspaceSlug}/projects/${projectId}/files/initiate-upload/`, payload)
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data ?? error;
      });
  }

  /** Verify the stored object and settle the attempt (R-UPL-3, R-UPL-4). */
  async completeFileUpload(
    workspaceSlug: string,
    projectId: string,
    fileId: string,
    payload: { version_no: number; size_bytes: number; checksum_sha256?: string | null }
  ): Promise<IProjectFileUploadCompletion> {
    return this.post(`/api/workspaces/${workspaceSlug}/projects/${projectId}/files/${fileId}/complete-upload/`, payload)
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data ?? error;
      });
  }

  /**
   * End a pending attempt and release its reservation exactly once.
   *
   * Called when an upload is cancelled or fails: a live reservation makes the next
   * presign for the same file a 409 `upload_in_progress`, so a retry has to give
   * the attempt up before it can ask for a new URL (ARCH-001 §2.4).
   */
  async abortFileUpload(workspaceSlug: string, projectId: string, fileId: string, versionNo: number): Promise<void> {
    return this.post(`/api/workspaces/${workspaceSlug}/projects/${projectId}/files/${fileId}/abort-upload/`, {
      version_no: versionNo,
    })
      .then(() => undefined)
      .catch((error) => {
        throw error?.response?.data ?? error;
      });
  }
}

// The `on*` assignments below are the idiomatic form for XHR progress and are the whole
// point of using XMLHttpRequest here; `addEventListener` reads no better for a handler this
// object never re-binds.
/* oxlint-disable prefer-add-event-listener */

/** A running presigned PUT, with the abort a cancel needs. */
export type TPresignedUploadHandle = {
  /** Resolves when the store accepted the bytes; rejects `{cancelled: true}` when aborted. */
  promise: Promise<void>;
  abort: () => void;
};

/**
 * Send the bytes straight to the object store with `XMLHttpRequest`.
 *
 * Not the axios client: this request goes to the storage host, not the API, and it
 * carries no session. It is an `XMLHttpRequest` because per-file progress and
 * cancellation are the two things the row needs and `fetch` reports neither. The
 * `Content-Type` the URL was signed for is set from the presigned headers, so the
 * signature matches whatever the browser would otherwise infer from the file.
 */
export const putPresignedFile = (params: {
  url: string;
  file: File;
  headers?: Record<string, string>;
  onProgress?: (percentage: number) => void;
}): TPresignedUploadHandle => {
  const { url, file, headers, onProgress } = params;
  const request = new XMLHttpRequest();

  const promise = new Promise<void>((resolve, reject) => {
    request.open("PUT", url, true);
    Object.entries(headers ?? {}).forEach(([key, value]) => request.setRequestHeader(key, value));

    request.upload.onprogress = (event) => {
      if (!onProgress || !event.lengthComputable || event.total <= 0) return;
      onProgress(Math.min(100, Math.round((event.loaded / event.total) * 100)));
    };
    request.onload = () => {
      if (request.status >= 200 && request.status < 300) {
        onProgress?.(100);
        resolve();
        return;
      }
      reject({ error: `The storage service refused this upload (${request.status}).` });
    };
    request.onerror = () => reject({ error: "The upload could not reach the storage service." });
    request.ontimeout = () => reject({ error: "The upload timed out." });
    request.onabort = () => reject({ cancelled: true });

    request.send(file);
  });

  return { promise, abort: () => request.abort() };
};

const uploadService = new ProjectFileService();

/**
 * Upload one file into a project and return the stored file (R-UPL-1..4).
 *
 * The same three steps the Files tab's queue runs per row - reserve and sign, PUT the
 * bytes straight to the store with progress, then have the server verify and store
 * them - without the queue's row bookkeeping: a queue also cancels, retries and
 * resolves name collisions, while an issue attachment or a page embed only needs the
 * file that came out of it. A failed attempt is given up here so its reservation is
 * released and the surface's retry is not refused with `upload_in_progress`
 * (ARCH-001 §2.4).
 */
export const uploadProjectFile = async (params: {
  workspaceSlug: string;
  projectId: string;
  file: File;
  folderId?: string | null;
  link?: TProjectFileEntityRef | null;
  onProgress?: (percentage: number) => void;
}): Promise<IProjectFileUploadCompletion> => {
  const { workspaceSlug, projectId, file, folderId, link, onProgress } = params;

  const initiation = await uploadService.initiateFileUpload(workspaceSlug, projectId, {
    file_name: file.name,
    size_bytes: file.size,
    mime_type: file.type,
    folder_id: folderId ?? null,
    link: link ?? null,
  });

  const handle = putPresignedFile({
    url: initiation.upload.url,
    file,
    headers: initiation.upload.headers,
    onProgress,
  });

  try {
    await handle.promise;
  } catch (error) {
    await uploadService
      .abortFileUpload(workspaceSlug, projectId, initiation.file.id, initiation.version_no)
      .catch(() => undefined);
    throw error;
  }

  return uploadService.completeFileUpload(workspaceSlug, projectId, initiation.file.id, {
    version_no: initiation.version_no,
    size_bytes: file.size,
  });
};
