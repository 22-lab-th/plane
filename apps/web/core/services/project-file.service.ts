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

/** The list query parameters the endpoint documents. */
export type TProjectFileListQuery = {
  /** Omit for the project root; a folder UUID browses that folder. */
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
}
