/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useMemo, useState } from "react";
import { observer } from "mobx-react";
import { useParams } from "next/navigation";
import { ArchiveRestoreIcon, FileOutput, FolderInput, LockKeyhole, LockKeyholeOpen } from "lucide-react";
// constants
import { EPageAccess } from "@plane/constants";
// plane editor
import { LinkIcon, CopyIcon, LockIcon, NewTabIcon, ArchiveIcon, TrashIcon, GlobeIcon } from "@plane/propel/icons";
// plane ui
import type { TContextMenuItem } from "@plane/ui";
import { ContextMenu, CustomMenu } from "@plane/ui";
import { getPageName } from "@plane/utils";
// components
import { cn } from "@plane/utils";
import { DeletePageModal } from "@/components/pages/modals/delete-page-modal";
// hooks
import { usePageOperations } from "@/hooks/use-page-operations";
// plane web hooks
import type { EPageStoreType } from "@/hooks/store";
import { usePageStore } from "@/hooks/store";
import { usePageFlag } from "@/hooks/use-page-flag";
// store types
import type { TPageInstance } from "@/store/pages/base-page";
import { TOAST_TYPE, setToast } from "@plane/propel/toast";

export type TPageActions =
  | "full-screen"
  | "sticky-toolbar"
  | "copy-markdown"
  | "toggle-lock"
  | "toggle-access"
  | "open-in-new-tab"
  | "copy-link"
  | "make-a-copy"
  | "archive-restore"
  | "delete"
  | "version-history"
  | "export"
  | "move"
  | "move-to-folder"
  | "rename-folder";

type Props = {
  extraOptions?: (TContextMenuItem & { key: TPageActions })[];
  optionsOrder: TPageActions[];
  page: TPageInstance;
  parentRef?: React.RefObject<HTMLElement>;
  storeType: EPageStoreType;
};

export const PageActions = observer(function PageActions(props: Props) {
  const { extraOptions, optionsOrder, page, parentRef, storeType } = props;
  // states
  const [deletePageModal, setDeletePageModal] = useState(false);
  const [movePageModal, setMovePageModal] = useState(false);
  // params
  const { workspaceSlug, projectId } = useParams();
  // page flag
  const { isMovePageEnabled } = usePageFlag({
    workspaceSlug: workspaceSlug?.toString() ?? "",
  });
  const { getMoveTargetFolders, moveToFolder } = usePageStore(storeType);
  // page operations
  const { pageOperations } = usePageOperations({
    page,
  });
  // derived values
  const {
    access,
    archived_at,
    is_locked,
    id,
    node_type,
    parent,
    canCurrentUserArchivePage,
    canCurrentUserChangeAccess,
    canCurrentUserDeletePage,
    canCurrentUserDuplicatePage,
    canCurrentUserLockPage,
    canCurrentUserMovePage,
  } = page;
  const isFolder = node_type === "folder";
  const moveFolders = getMoveTargetFolders(projectId?.toString() || "", access, id);

  const handleMoveToFolder = async (targetParentId: string | null) => {
    if (!id) return;
    try {
      await moveToFolder(id, targetParentId);
      setToast({
        type: TOAST_TYPE.SUCCESS,
        title: "Success!",
        message: targetParentId ? "Moved into folder." : "Moved to root.",
      });
    } catch (err: unknown) {
      const message =
        typeof err === "object" && err && "error" in err
          ? String((err as { error?: string }).error)
          : "Could not move item.";
      setToast({
        type: TOAST_TYPE.ERROR,
        title: "Error!",
        message,
      });
    }
  };

  const handleRenameFolder = () => {
    if (!isFolder) return;
    const nextName = window.prompt("Rename folder", page.name || "");
    if (!nextName?.trim() || nextName.trim() === page.name) return;
    page.updateTitle(nextName.trim());
  };

  // menu items
  const MENU_ITEMS = useMemo(
    function MENU_ITEMS() {
      const menuItems: (TContextMenuItem & { key: TPageActions })[] = [
        {
          key: "toggle-lock",
          action: () => {
            pageOperations.toggleLock();
          },
          title: is_locked ? "Unlock" : "Lock",
          icon: is_locked ? LockKeyholeOpen : LockKeyhole,
          shouldRender: canCurrentUserLockPage && !isFolder,
        },
        {
          key: "toggle-access",
          action: () => {
            pageOperations.toggleAccess();
          },
          title: access === EPageAccess.PUBLIC ? "Make private" : "Make public",
          icon: access === EPageAccess.PUBLIC ? LockIcon : GlobeIcon,
          shouldRender: canCurrentUserChangeAccess && !archived_at,
        },
        {
          key: "open-in-new-tab",
          action: pageOperations.openInNewTab,
          title: "Open in new tab",
          icon: NewTabIcon,
          shouldRender: !isFolder,
        },
        {
          key: "copy-link",
          action: pageOperations.copyLink,
          title: "Copy link",
          icon: LinkIcon,
          shouldRender: !isFolder,
        },
        {
          key: "make-a-copy",
          action: () => {
            pageOperations.duplicate();
          },
          title: "Make a copy",
          icon: CopyIcon,
          shouldRender: canCurrentUserDuplicatePage && !isFolder,
        },
        {
          key: "rename-folder",
          action: () => {
            handleRenameFolder();
          },
          title: "Rename folder",
          icon: FolderInput,
          shouldRender: isFolder && canCurrentUserChangeAccess && !archived_at,
        },
        {
          key: "move-to-folder",
          action: () => undefined,
          title: "Move to folder",
          icon: FolderInput,
          shouldRender: canCurrentUserMovePage && !archived_at,
          customContent: (
            <CustomMenu
              customButton={
                <span className="flex w-full items-center gap-2">
                  <FolderInput className="size-3" />
                  Move to folder
                </span>
              }
              placement="left-start"
              closeOnSelect
            >
              <CustomMenu.MenuItem onClick={() => void handleMoveToFolder(null)} disabled={!parent}>
                Root
              </CustomMenu.MenuItem>
              {moveFolders.map((folder) => (
                <CustomMenu.MenuItem
                  key={folder.id}
                  onClick={() => void handleMoveToFolder(folder.id || null)}
                  disabled={folder.id === parent}
                >
                  {getPageName(folder.name)}
                </CustomMenu.MenuItem>
              ))}
            </CustomMenu>
          ),
        },
        {
          key: "archive-restore",
          action: () => {
            pageOperations.toggleArchive();
          },
          title: archived_at ? "Restore" : "Archive",
          icon: archived_at ? ArchiveRestoreIcon : ArchiveIcon,
          shouldRender: canCurrentUserArchivePage,
        },
        {
          key: "delete",
          action: () => {
            setDeletePageModal(true);
          },
          title: "Delete",
          icon: TrashIcon,
          shouldRender: canCurrentUserDeletePage && !!archived_at,
        },
        {
          key: "move",
          action: () => setMovePageModal(true),
          title: "Move",
          icon: FileOutput,
          shouldRender: canCurrentUserMovePage && isMovePageEnabled && !isFolder,
        },
      ];
      if (extraOptions) {
        menuItems.push(...extraOptions);
      }
      return menuItems;
    },
    [
      extraOptions,
      is_locked,
      canCurrentUserLockPage,
      access,
      canCurrentUserChangeAccess,
      archived_at,
      canCurrentUserDuplicatePage,
      canCurrentUserArchivePage,
      canCurrentUserDeletePage,
      canCurrentUserMovePage,
      isMovePageEnabled,
      pageOperations,
      isFolder,
      moveFolders,
      parent,
    ]
  );
  // arrange options
  const arrangedOptions = useMemo<(TContextMenuItem & { key: TPageActions })[]>(
    () =>
      optionsOrder
        .map((key) => MENU_ITEMS.find((item) => item.key === key))
        .filter((item): item is TContextMenuItem & { key: TPageActions } => !!item),
    [optionsOrder, MENU_ITEMS]
  );

  return (
    <>
      <DeletePageModal
        isOpen={deletePageModal}
        onClose={() => setDeletePageModal(false)}
        page={page}
        storeType={storeType}
      />
      {parentRef && <ContextMenu parentRef={parentRef} items={arrangedOptions} />}
      <CustomMenu placement="bottom-end" optionsClassName="max-h-[90vh]" ellipsis closeOnSelect>
        {arrangedOptions.map((item) => {
          if (item.shouldRender === false) return null;
          return (
            <CustomMenu.MenuItem
              key={item.key}
              onClick={() => {
                if (item.key === "move-to-folder") return;
                item.action?.();
              }}
              className={cn("flex items-center gap-2", item.className)}
              disabled={item.disabled}
            >
              {item.customContent ?? (
                <>
                  {item.icon && <item.icon className="size-3" />}
                  {item.title}
                </>
              )}
            </CustomMenu.MenuItem>
          );
        })}
      </CustomMenu>
    </>
  );
});
