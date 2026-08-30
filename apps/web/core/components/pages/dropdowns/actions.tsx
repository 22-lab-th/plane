/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useMemo, useState } from "react";
import { observer } from "mobx-react";
import { ArchiveRestoreIcon, FolderInput, FolderOutput, FolderPen, LockKeyhole, LockKeyholeOpen } from "lucide-react";
// constants
import { EPageAccess } from "@plane/constants";
// plane editor
import { LinkIcon, CopyIcon, LockIcon, NewTabIcon, ArchiveIcon, TrashIcon, GlobeIcon } from "@plane/propel/icons";
// plane ui
import type { TContextMenuItem } from "@plane/ui";
import { ContextMenu, CustomMenu } from "@plane/ui";
// components
import { cn } from "@plane/utils";
import { DeletePageModal } from "@/components/pages/modals/delete-page-modal";
import { FolderNameModal } from "@/components/pages/modals/folder-name-modal";
import { MoveToFolderModal } from "@/components/pages/modals/move-to-folder-modal";
import { MoveToProjectModal } from "@/components/pages/modals/move-to-project-modal";
// hooks
import { usePageOperations } from "@/hooks/use-page-operations";
// plane web hooks
import { EPageStoreType } from "@/hooks/store";
// store types
import type { TPageInstance } from "@/store/pages/base-page";

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
  const [moveToFolderModal, setMoveToFolderModal] = useState(false);
  const [moveToProjectModal, setMoveToProjectModal] = useState(false);
  const [renameFolderModal, setRenameFolderModal] = useState(false);
  // page operations
  const { pageOperations } = usePageOperations({
    page,
  });
  // derived values
  const {
    access,
    archived_at,
    is_locked,
    node_type,
    canCurrentUserArchivePage,
    canCurrentUserChangeAccess,
    canCurrentUserDeletePage,
    canCurrentUserDuplicatePage,
    canCurrentUserLockPage,
    canCurrentUserMovePage,
  } = page;
  const isFolder = node_type === "folder";

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
          action: () => setRenameFolderModal(true),
          title: "Rename folder",
          icon: FolderPen,
          shouldRender: isFolder && canCurrentUserChangeAccess && !archived_at,
        },
        {
          key: "move-to-folder",
          action: () => setMoveToFolderModal(true),
          title: "Move to folder",
          icon: FolderInput,
          shouldRender: canCurrentUserMovePage && !archived_at,
        },
        {
          key: "move",
          action: () => setMoveToProjectModal(true),
          title: "Move to project",
          icon: FolderOutput,
          shouldRender: storeType === EPageStoreType.PROJECT && canCurrentUserMovePage && !archived_at && !isFolder,
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
      pageOperations,
      isFolder,
      storeType,
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
      <MoveToFolderModal
        isOpen={moveToFolderModal}
        onClose={() => setMoveToFolderModal(false)}
        page={page}
        storeType={storeType}
      />
      <MoveToProjectModal isOpen={moveToProjectModal} onClose={() => setMoveToProjectModal(false)} page={page} />
      <FolderNameModal
        isOpen={renameFolderModal}
        onClose={() => setRenameFolderModal(false)}
        onSubmit={(name) => page.updateTitle(name)}
        title="Rename folder"
        submitLabel="Rename"
        submittingLabel="Renaming"
        initialName={page.name ?? ""}
      />
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
                item.action?.();
              }}
              className={cn("flex items-center gap-2", item.className)}
              disabled={item.disabled}
            >
              {item.icon && <item.icon className="size-3" />}
              {item.title}
            </CustomMenu.MenuItem>
          );
        })}
      </CustomMenu>
    </>
  );
});
