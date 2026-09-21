/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useCallback } from "react";
// plane imports
import type { TFileHandler } from "@plane/editor";
import { getEditorAssetDownloadSrc, getEditorAssetSrc } from "@plane/utils";
// hooks
import { useEditorAsset } from "@/hooks/store/use-editor-asset";
// plane web hooks
import { useExtendedEditorConfig } from "@/hooks/editor/use-extended-editor-config";
import { useFileSize } from "@/hooks/use-file-size";
// services
import { FileService } from "@/services/file.service";
import { ProjectFileService, PROJECT_FILE_REF_PREFIX, isProjectFileRef } from "@/services/project-file.service";
const fileService = new FileService();
const projectFileService = new ProjectFileService();

type TArgs = {
  projectId?: string;
  uploadFile: TFileHandler["upload"];
  duplicateFile: TFileHandler["duplicate"];
  workspaceId: string;
  workspaceSlug: string;
};

export const useEditorConfig = () => {
  // store hooks
  const { assetsUploadPercentage } = useEditorAsset();
  // file size
  const { maxFileSize } = useFileSize();
  const { getExtendedEditorFileHandlers } = useExtendedEditorConfig();

  const getEditorFileHandlers = useCallback(
    (args: TArgs): TFileHandler => {
      const { projectId, uploadFile, duplicateFile, workspaceId, workspaceSlug } = args;

      return {
        assetsUploadStatus: assetsUploadPercentage,
        cancel: fileService.cancelUpload,
        checkIfAssetExists: async (assetId: string) => {
          // A page's embed is a project file, so its existence is the file row's:
          // the detail endpoint answers 404 once the row is gone.
          if (isProjectFileRef(assetId)) {
            if (!projectId) return false;
            try {
              await projectFileService.getProjectFile(
                workspaceSlug,
                projectId,
                assetId.slice(PROJECT_FILE_REF_PREFIX.length)
              );
              return true;
            } catch {
              return false;
            }
          }
          const res = await fileService.checkIfAssetExists(workspaceSlug, assetId);
          return res?.exists ?? false;
        },
        delete: async (src: string) => {
          // Removing an embed from a page does not delete the file: the document
          // drops the reference, and the project file stays in the project's Files
          // view with its links (the unlink-not-delete rule, AC-21).
          if (isProjectFileRef(src)) return;
          if (src?.startsWith("http")) {
            await fileService.deleteOldWorkspaceAsset(workspaceId, src);
          } else {
            await fileService.deleteNewAsset(
              getEditorAssetSrc({
                assetId: src,
                projectId,
                workspaceSlug,
              }) ?? ""
            );
          }
        },
        getAssetDownloadSrc: async (path) => {
          if (!path) return "";
          if (isProjectFileRef(path)) {
            if (!projectId) return "";
            const { url } = await projectFileService.getProjectFileDownloadUrl(
              workspaceSlug,
              projectId,
              path.slice(PROJECT_FILE_REF_PREFIX.length)
            );
            return url;
          }
          if (path?.startsWith("http")) {
            return path;
          } else {
            return (
              getEditorAssetDownloadSrc({
                assetId: path,
                projectId,
                workspaceSlug,
              }) ?? ""
            );
          }
        },
        getAssetSrc: async (path) => {
          if (!path) return "";
          // A project file is resolved through the project-scoped preview endpoint on
          // every render: the URL is short-lived, and the document stores the file's
          // reference rather than a signed URL (R-LINK-3, DESIGN §8).
          if (isProjectFileRef(path)) {
            if (!projectId) throw new Error("A project file needs a project to be resolved in.");
            const { url } = await projectFileService.getProjectFilePreviewUrl(
              workspaceSlug,
              projectId,
              path.slice(PROJECT_FILE_REF_PREFIX.length)
            );
            return url;
          }
          if (path?.startsWith("http")) {
            return path;
          } else {
            return (
              getEditorAssetSrc({
                assetId: path,
                projectId,
                workspaceSlug,
              }) ?? ""
            );
          }
        },
        restore: async (src: string) => {
          if (isProjectFileRef(src)) return;
          if (src?.startsWith("http")) {
            await fileService.restoreOldEditorAsset(workspaceId, src);
          } else {
            await fileService.restoreNewAsset(workspaceSlug, src);
          }
        },
        upload: uploadFile,
        duplicate: duplicateFile,
        validation: {
          maxFileSize,
        },
        ...getExtendedEditorFileHandlers({ projectId, workspaceSlug }),
      };
    },
    [assetsUploadPercentage, getExtendedEditorFileHandlers, maxFileSize]
  );

  return {
    getEditorFileHandlers,
  };
};
