/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useState } from "react";
import { observer } from "mobx-react";
import { useParams } from "next/navigation";
import { Check, Folder, FolderTree } from "lucide-react";
// plane ui
import { TOAST_TYPE, setToast } from "@plane/propel/toast";
import { EModalPosition, EModalWidth, ModalCore } from "@plane/ui";
import { cn, getPageName } from "@plane/utils";
// hooks
import type { EPageStoreType } from "@/hooks/store";
import { usePageStore } from "@/hooks/store";
// store types
import type { TPageInstance } from "@/store/pages/base-page";

type Props = {
  isOpen: boolean;
  onClose: () => void;
  page: TPageInstance;
  storeType: EPageStoreType;
};

/**
 * Modal that lists sibling-access folders and moves the given page/folder into
 * the selected folder (or back to root). Replaces the broken nested submenu.
 */
export const MoveToFolderModal = observer(function MoveToFolderModal(props: Props) {
  const { isOpen, onClose, page, storeType } = props;
  const { projectId } = useParams();
  const { getMoveTargetFolders, moveToFolder } = usePageStore(storeType);
  const [movingTo, setMovingTo] = useState<string | null | undefined>(undefined);

  const { id, access, parent } = page;
  const folders = getMoveTargetFolders(projectId?.toString() || "", access, id);

  const handleMove = async (targetParentId: string | null) => {
    if (!id || movingTo !== undefined) return;
    setMovingTo(targetParentId);
    try {
      await moveToFolder(id, targetParentId);
      setToast({
        type: TOAST_TYPE.SUCCESS,
        title: "Success!",
        message: targetParentId ? "Moved into folder." : "Moved to root.",
      });
      onClose();
    } catch (err: unknown) {
      const message =
        typeof err === "object" && err && "error" in err
          ? String((err as { error?: string }).error)
          : "Could not move item.";
      setToast({ type: TOAST_TYPE.ERROR, title: "Error!", message });
    } finally {
      setMovingTo(undefined);
    }
  };

  const rows: { key: string; label: string; targetId: string | null; icon: typeof Folder; disabled: boolean }[] = [
    {
      key: "__root__",
      label: "Root (no folder)",
      targetId: null,
      icon: FolderTree,
      disabled: !parent,
    },
    ...folders.map((folder) => ({
      key: folder.id ?? "",
      label: getPageName(folder.name),
      targetId: folder.id || null,
      icon: Folder,
      disabled: folder.id === parent,
    })),
  ];

  return (
    <ModalCore isOpen={isOpen} handleClose={onClose} position={EModalPosition.CENTER} width={EModalWidth.SM}>
      <div>
        <div className="p-5 pb-3">
          <h3 className="text-18 font-medium text-secondary">Move to folder</h3>
        </div>
        <div className="max-h-[40vh] overflow-y-auto px-2 pb-3">
          {rows.length === 1 && folders.length === 0 && (
            <p className="px-3 py-4 text-13 text-tertiary">No folders yet. Create one from the Pages list first.</p>
          )}
          {rows.map((row) => (
            <button
              key={row.key}
              type="button"
              disabled={row.disabled || movingTo !== undefined}
              onClick={() => void handleMove(row.targetId)}
              className={cn(
                "flex w-full items-center justify-between gap-2 rounded-md px-3 py-2 text-13 text-secondary",
                "hover:bg-surface-2 disabled:cursor-not-allowed disabled:opacity-50"
              )}
            >
              <span className="flex items-center gap-2 truncate">
                <row.icon className="size-4 flex-shrink-0 text-tertiary" />
                <span className="truncate">{row.label}</span>
              </span>
              {row.disabled && !movingTo && <Check className="size-4 flex-shrink-0 text-tertiary" />}
            </button>
          ))}
        </div>
      </div>
    </ModalCore>
  );
});
