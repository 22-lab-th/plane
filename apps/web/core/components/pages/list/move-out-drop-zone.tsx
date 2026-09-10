/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useEffect, useRef, useState } from "react";
import { dropTargetForElements } from "@atlaskit/pragmatic-drag-and-drop/element/adapter";
import { observer } from "mobx-react";
import { CornerLeftUp } from "lucide-react";
import { TOAST_TYPE, setToast } from "@plane/propel/toast";
import { cn } from "@plane/utils";
// plane web hooks
import type { EPageStoreType } from "@/hooks/store";
import { usePageStore } from "@/hooks/store";
// local
import { isPageListDragData } from "./block";

type Props = {
  storeType: EPageStoreType;
  /** Current folder being viewed; drop moves items to this folder's parent (or root). */
  folderId: string;
};

/**
 * Drop strip shown while browsing inside a folder. Dropping a page/folder here
 * moves it up one level (parent folder, or root when the current folder is top-level).
 */
export const PageMoveOutDropZone = observer(function PageMoveOutDropZone(props: Props) {
  const { storeType, folderId } = props;
  const elementRef = useRef<HTMLDivElement>(null);
  const [isDropTarget, setIsDropTarget] = useState(false);
  const { getPageById, moveToFolder } = usePageStore(storeType);
  const currentFolder = getPageById(folderId);
  const targetParentId = currentFolder?.parent ?? null;
  const label = targetParentId ? "Drop here to move up one level" : "Drop here to move to root";

  useEffect(() => {
    const element = elementRef.current;
    if (!element) return;

    return dropTargetForElements({
      element,
      canDrop: ({ source }) => isPageListDragData(source.data),
      getData: () => ({ type: "PAGE_LIST_MOVE_OUT", folderId }),
      onDragEnter: () => setIsDropTarget(true),
      onDragLeave: () => setIsDropTarget(false),
      onDrop: ({ source }) => {
        setIsDropTarget(false);
        if (!isPageListDragData(source.data)) return;
        // already at the target parent — no-op
        const sourcePage = getPageById(source.data.pageId);
        if ((sourcePage?.parent ?? null) === targetParentId) return;
        void moveToFolder(source.data.pageId, targetParentId)
          .then(() => {
            setToast({
              type: TOAST_TYPE.SUCCESS,
              title: "Success!",
              message: targetParentId ? "Moved up one level." : "Moved to root.",
            });
            return;
          })
          .catch((err: unknown) => {
            const message =
              typeof err === "object" && err && "error" in err
                ? String((err as { error?: string }).error)
                : "Could not move item.";
            setToast({ type: TOAST_TYPE.ERROR, title: "Error!", message });
          });
      },
    });
  }, [folderId, getPageById, moveToFolder, targetParentId]);

  return (
    <div
      ref={elementRef}
      className={cn(
        "mb-2 flex items-center justify-center gap-2 rounded-md border border-dashed border-subtle px-3 py-3 text-13 text-tertiary transition-colors",
        {
          "border-accent-strong bg-accent-primary/10 text-primary": isDropTarget,
        }
      )}
    >
      <CornerLeftUp className="size-4 shrink-0" />
      <span>{label}</span>
    </div>
  );
});
