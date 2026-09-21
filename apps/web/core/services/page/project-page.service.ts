import type { TPageDescriptionSnapshot } from "@plane/types";
/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// types
import { API_BASE_URL } from "@plane/constants";
import type { TDocumentPayload, TPage } from "@plane/types";
// helpers
// services
import { APIService } from "@/services/api.service";
import { FileUploadService } from "@/services/file-upload.service";

export class ProjectPageService extends APIService {
  private fileUploadService: FileUploadService;

  constructor() {
    super(API_BASE_URL);
    // upload service
    this.fileUploadService = new FileUploadService();
  }

  async fetchAll(
    workspaceSlug: string,
    projectId: string,
    parent?: string | null,
    options?: { foldersOnly?: boolean }
  ): Promise<TPage[]> {
    return this.get(`/api/workspaces/${workspaceSlug}/projects/${projectId}/pages/`, {
      params: {
        parent: options?.foldersOnly ? undefined : parent === undefined ? undefined : parent === null ? "root" : parent,
        folders_only: options?.foldersOnly ? "true" : undefined,
      },
    })
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  async fetchById(workspaceSlug: string, projectId: string, pageId: string, trackVisit: boolean): Promise<TPage> {
    return this.get(`/api/workspaces/${workspaceSlug}/projects/${projectId}/pages/${pageId}/`, {
      params: {
        track_visit: trackVisit,
      },
    })
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  async create(workspaceSlug: string, projectId: string, data: Partial<TPage>): Promise<TPage> {
    return this.post(`/api/workspaces/${workspaceSlug}/projects/${projectId}/pages/`, data)
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  async update(workspaceSlug: string, projectId: string, pageId: string, data: Partial<TPage>): Promise<TPage> {
    return this.patch(`/api/workspaces/${workspaceSlug}/projects/${projectId}/pages/${pageId}/`, data)
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  async updateAccess(
    workspaceSlug: string,
    projectId: string,
    pageId: string,
    data: Pick<TPage, "access">
  ): Promise<void> {
    return this.post(`/api/workspaces/${workspaceSlug}/projects/${projectId}/pages/${pageId}/access/`, data)
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  async remove(workspaceSlug: string, projectId: string, pageId: string): Promise<void> {
    return this.delete(`/api/workspaces/${workspaceSlug}/projects/${projectId}/pages/${pageId}/`)
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  async fetchFavorites(workspaceSlug: string, projectId: string): Promise<TPage[]> {
    return this.get(`/api/workspaces/${workspaceSlug}/projects/${projectId}/favorite-pages/`)
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  async addToFavorites(workspaceSlug: string, projectId: string, pageId: string): Promise<void> {
    return this.post(`/api/workspaces/${workspaceSlug}/projects/${projectId}/favorite-pages/${pageId}/`)
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  async removeFromFavorites(workspaceSlug: string, projectId: string, pageId: string): Promise<void> {
    return this.delete(`/api/workspaces/${workspaceSlug}/projects/${projectId}/favorite-pages/${pageId}/`)
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  async fetchArchived(workspaceSlug: string, projectId: string): Promise<TPage[]> {
    return this.get(`/api/workspaces/${workspaceSlug}/projects/${projectId}/archived-pages/`)
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  async archive(
    workspaceSlug: string,
    projectId: string,
    pageId: string
  ): Promise<{
    archived_at: string;
  }> {
    return this.post(`/api/workspaces/${workspaceSlug}/projects/${projectId}/pages/${pageId}/archive/`)
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  async restore(workspaceSlug: string, projectId: string, pageId: string): Promise<void> {
    return this.delete(`/api/workspaces/${workspaceSlug}/projects/${projectId}/pages/${pageId}/archive/`)
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  async lock(workspaceSlug: string, projectId: string, pageId: string): Promise<void> {
    return this.post(`/api/workspaces/${workspaceSlug}/projects/${projectId}/pages/${pageId}/lock/`)
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  async unlock(workspaceSlug: string, projectId: string, pageId: string): Promise<void> {
    return this.delete(`/api/workspaces/${workspaceSlug}/projects/${projectId}/pages/${pageId}/lock/`)
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  async fetchDescriptionSnapshot(
    workspaceSlug: string,
    projectId: string,
    pageId: string
  ): Promise<TPageDescriptionSnapshot> {
    const response = await this.get(
      `/api/workspaces/${workspaceSlug}/projects/${projectId}/pages/${pageId}/description/`,
      {
        responseType: "arraybuffer",
      }
    );
    const etag = response.headers["x-plane-document-version"];
    if (typeof etag !== "string" || !etag.startsWith('"')) throw new Error("Page snapshot is missing its revision");
    const binary = response.data as ArrayBuffer;
    const page = binary.byteLength === 0 ? await this.fetchById(workspaceSlug, projectId, pageId, false) : undefined;
    return {
      binary,
      etag,
      initialContent: page
        ? { name: page.name ?? "", description_html: page.description_html ?? "<p></p>" }
        : undefined,
    };
  }

  async updateDescription(
    workspaceSlug: string,
    projectId: string,
    pageId: string,
    data: TDocumentPayload,
    etag: string
  ): Promise<any> {
    return this.patch(`/api/workspaces/${workspaceSlug}/projects/${projectId}/pages/${pageId}/description/`, data, {
      headers: { "If-Match": etag },
    })
      .then((response) => response?.data)
      .catch((error) => {
        throw error;
      });
  }

  async duplicate(workspaceSlug: string, projectId: string, pageId: string): Promise<TPage> {
    return this.post(`/api/workspaces/${workspaceSlug}/projects/${projectId}/pages/${pageId}/duplicate/`)
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  async move(workspaceSlug: string, projectId: string, pageId: string, newProjectId: string): Promise<void> {
    return this.post(`/api/workspaces/${workspaceSlug}/projects/${projectId}/pages/${pageId}/move/`, {
      new_project_id: newProjectId,
    })
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }
}
