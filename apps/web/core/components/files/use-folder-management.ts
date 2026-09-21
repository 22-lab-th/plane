/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useCallback, useState } from "react";
import type { IProjectFolder } from "@/services/project-file.service";
import { ProjectFileService } from "@/services/project-file.service";
import type { TFolderDialog } from "./folder-management";
import { readUploadFailure } from "./upload-queue";

const folderService = new ProjectFileService();

type Props = {
  workspaceSlug: string;
  projectId: string;
  currentFolderId: string | null;
  onRefresh: () => Promise<void>;
  updateParams: (updates: Record<string, string | null>, options?: { replace?: boolean }) => void;
};

/** Owns folder dialogs, tree loading and mutations so the Files view stays focused on browsing. */
export function useFolderManagement(props: Props) {
  const { workspaceSlug, projectId, currentFolderId, onRefresh, updateParams } = props;
  const [dialog, setDialog] = useState<TFolderDialog | null>(null);
  const [folderTree, setFolderTree] = useState<IProjectFolder[]>([]);
  const [isFolderTreeLoading, setIsFolderTreeLoading] = useState(false);
  const [folderTreeError, setFolderTreeError] = useState<string | null>(null);

  const loadFolderTree = useCallback(async () => {
    setIsFolderTreeLoading(true);
    setFolderTreeError(null);
    try {
      const folders: IProjectFolder[] = [];
      const seen = new Set<string>();

      const loadLevel = async (parentIds: (string | null)[]): Promise<void> => {
        if (parentIds.length === 0) return;

        const responses = await Promise.all(
          parentIds.map((parentId) =>
            folderService.listProjectFiles(workspaceSlug, projectId, {
              folder_id: parentId ?? "root",
              page_size: 1,
            })
          )
        );
        const nextParentIds: string[] = [];

        responses.forEach((response) => {
          response.folders.forEach((folder) => {
            if (seen.has(folder.id)) return;
            seen.add(folder.id);
            folders.push(folder);
            nextParentIds.push(folder.id);
          });
        });

        await loadLevel(nextParentIds);
      };

      await loadLevel([null]);

      setFolderTree(folders);
    } catch (failure) {
      setFolderTreeError(readUploadFailure(failure).message ?? "We could not load the folder tree.");
    } finally {
      setIsFolderTreeLoading(false);
    }
  }, [projectId, workspaceSlug]);

  const openCreateDialog = useCallback(() => {
    setDialog({ kind: "create", parentId: currentFolderId });
  }, [currentFolderId]);

  const openRenameDialog = useCallback((folder: IProjectFolder) => {
    setDialog({ kind: "rename", folder });
  }, []);

  const openMoveDialog = useCallback(
    (folder: IProjectFolder) => {
      setDialog({ kind: "move", folder });
      void loadFolderTree();
    },
    [loadFolderTree]
  );

  const openDeleteDialog = useCallback((folder: IProjectFolder) => {
    setDialog({ kind: "delete", folder });
  }, []);

  const createFolder = useCallback(
    async (name: string, parentId: string | null) => {
      await folderService.createProjectFolder(workspaceSlug, projectId, { name, parent_id: parentId });
      await onRefresh();
    },
    [onRefresh, projectId, workspaceSlug]
  );

  const renameFolder = useCallback(
    async (folderId: string, name: string) => {
      await folderService.updateProjectFolder(workspaceSlug, projectId, folderId, { name });
      await onRefresh();
    },
    [onRefresh, projectId, workspaceSlug]
  );

  const moveFolder = useCallback(
    async (folderId: string, parentId: string | null) => {
      await folderService.updateProjectFolder(workspaceSlug, projectId, folderId, { parent_id: parentId });
      await onRefresh();
    },
    [onRefresh, projectId, workspaceSlug]
  );

  const deleteFolder = useCallback(
    async (folderId: string) => {
      await folderService.deleteProjectFolder(workspaceSlug, projectId, folderId, { recursive: true });
      await onRefresh();
      if (currentFolderId === folderId) updateParams({ folder: null, file: null }, { replace: true });
    },
    [currentFolderId, onRefresh, projectId, updateParams, workspaceSlug]
  );

  return {
    dialog,
    folderTree,
    isFolderTreeLoading,
    folderTreeError,
    loadFolderTree,
    openCreateDialog,
    openRenameDialog,
    openMoveDialog,
    openDeleteDialog,
    createFolder,
    renameFolder,
    moveFolder,
    deleteFolder,
    closeDialog: () => setDialog(null),
  };
}
