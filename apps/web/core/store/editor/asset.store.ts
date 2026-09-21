/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { debounce, set } from "lodash-es";
import { action, computed, makeObservable, observable, runInAction } from "mobx";
import { computedFn } from "mobx-utils";
import { v4 as uuidv4 } from "uuid";
// plane types
import { EFileAssetType } from "@plane/types";
import type { TFileEntityInfo } from "@plane/types";
// services
import { FileService } from "@/services/file.service";
import {
  ProjectFileService,
  PROJECT_FILE_REF_PREFIX,
  isProjectFileRef,
  uploadProjectFile,
} from "@/services/project-file.service";
import type { TAttachmentUploadStatus } from "../issue/issue-details/attachment.store";

export interface IEditorAssetStore {
  // computed
  assetsUploadPercentage: Record<string, number>;
  // helper methods
  getAssetUploadStatusByEditorBlockId: (blockId: string) => TAttachmentUploadStatus | undefined;
  // actions
  uploadEditorAsset: ({
    blockId,
    data,
    file,
    projectId,
    workspaceSlug,
  }: {
    blockId: string;
    data: TFileEntityInfo;
    file: File;
    projectId?: string;
    workspaceSlug: string;
  }) => Promise<{ asset_id: string }>;
  duplicateEditorAsset: ({
    assetId,
    entityId,
    entityType,
    projectId,
    workspaceSlug,
  }: {
    assetId: string;
    entityId?: string;
    entityType: EFileAssetType;
    projectId?: string;
    workspaceSlug: string;
  }) => Promise<{ asset_id: string }>;
}

export class EditorAssetStore implements IEditorAssetStore {
  // observables
  assetsUploadStatus: Record<string, TAttachmentUploadStatus> = {};
  // services
  fileService: FileService;
  projectFileService: ProjectFileService;

  constructor() {
    makeObservable(this, {
      // observables
      assetsUploadStatus: observable,
      // computed
      assetsUploadPercentage: computed,
      // actions
      uploadEditorAsset: action,
    });
    // services
    this.fileService = new FileService();
    this.projectFileService = new ProjectFileService();
  }

  get assetsUploadPercentage() {
    const assetsStatus = this.assetsUploadStatus;
    const assetsPercentage: Record<string, number> = {};
    Object.keys(assetsStatus).forEach((blockId) => {
      const asset = assetsStatus[blockId];
      if (asset) assetsPercentage[blockId] = asset.progress;
    });
    return assetsPercentage;
  }

  // helper methods
  getAssetUploadStatusByEditorBlockId: IEditorAssetStore["getAssetUploadStatusByEditorBlockId"] = computedFn(
    (blockId) => {
      const blockDetails = this.assetsUploadStatus[blockId];
      if (!blockDetails) return undefined;
      return blockDetails;
    }
  );

  // actions
  private debouncedUpdateProgress = debounce((blockId: string, progress: number) => {
    runInAction(() => {
      set(this.assetsUploadStatus, [blockId, "progress"], progress);
    });
  }, 16);

  uploadEditorAsset: IEditorAssetStore["uploadEditorAsset"] = async (args) => {
    const { blockId, data, file, projectId, workspaceSlug } = args;
    const tempId = uuidv4();

    try {
      // update attachment upload status
      runInAction(() => {
        set(this.assetsUploadStatus, [blockId], {
          id: tempId,
          name: file.name,
          progress: 0,
          size: file.size,
          type: file.type,
        });
      });
      // A **page**'s embeds are project files (R-LINK-3, AC-17): the upload runs
      // through the same pipeline the Files tab uses and carries a link to the page,
      // so the file appears once in the project's repository with the id the document
      // refers to. The other editor surfaces (issue/comment descriptions, drafts)
      // keep the legacy asset path - a draft has no row for the link validator to
      // check, and those surfaces are not what this ticket routes.
      if (projectId && data.entity_type === EFileAssetType.PAGE_DESCRIPTION && data.entity_identifier) {
        const completion = await uploadProjectFile({
          workspaceSlug,
          projectId,
          file,
          link: { entity_type: "page", entity_id: data.entity_identifier },
          onProgress: (percentage) => this.debouncedUpdateProgress(blockId, percentage),
        });
        return { asset_id: `${PROJECT_FILE_REF_PREFIX}${completion.file.id}` };
      }
      if (projectId) {
        const response = await this.fileService.uploadProjectAsset(
          workspaceSlug,
          projectId,
          data,
          file,
          (progressEvent) => {
            const progressPercentage = Math.round((progressEvent.progress ?? 0) * 100);
            this.debouncedUpdateProgress(blockId, progressPercentage);
          }
        );
        return { asset_id: response.asset_id };
      } else {
        const response = await this.fileService.uploadWorkspaceAsset(workspaceSlug, data, file, (progressEvent) => {
          const progressPercentage = Math.round((progressEvent.progress ?? 0) * 100);
          this.debouncedUpdateProgress(blockId, progressPercentage);
        });
        return { asset_id: response.asset_id };
      }
    } catch (error) {
      console.error("Error in uploading page asset:", error);
      throw error;
    } finally {
      runInAction(() => {
        delete this.assetsUploadStatus[blockId];
      });
    }
  };
  /**
   * Duplicate one embed (R-LINK-3's page-copy rule).
   *
   * A project file is copied **inside the project** - a new file id with its own
   * objects, charged to the same quota - and the copy is linked to the same entity,
   * so a duplicated page never shares an object key with the original. A legacy
   * asset keeps the service call it has always had.
   */
  duplicateEditorAsset: IEditorAssetStore["duplicateEditorAsset"] = async (args) => {
    const { assetId, entityId, entityType, projectId, workspaceSlug } = args;
    if (projectId && isProjectFileRef(assetId)) {
      const copy = await this.projectFileService.copyProjectFile(
        workspaceSlug,
        projectId,
        assetId.slice(PROJECT_FILE_REF_PREFIX.length)
      );
      if (entityId && entityType === EFileAssetType.PAGE_DESCRIPTION) {
        await this.projectFileService.linkProjectFile(workspaceSlug, projectId, copy.id, {
          entity_type: "page",
          entity_id: entityId,
        });
      }
      return { asset_id: `${PROJECT_FILE_REF_PREFIX}${copy.id}` };
    }
    const { asset_id } = await this.fileService.duplicateAsset(workspaceSlug, assetId, {
      entity_id: entityId,
      entity_type: entityType,
      project_id: projectId,
    });
    return { asset_id };
  };
}
