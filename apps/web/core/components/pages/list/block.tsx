/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useEffect, useRef, useState } from "react";
import { combine } from "@atlaskit/pragmatic-drag-and-drop/combine";
import { draggable, dropTargetForElements } from "@atlaskit/pragmatic-drag-and-drop/element/adapter";
import { observer } from "mobx-react";
import { useParams, useSearchParams } from "next/navigation";
import { Folder } from "lucide-react";
import { Logo } from "@plane/propel/emoji-icon-picker";
import { PageIcon } from "@plane/propel/icons";
import { TOAST_TYPE, setToast } from "@plane/propel/toast";
// plane imports
import { cn, getPageName } from "@plane/utils";
// components
import { ListItem } from "@/components/core/list";
import { BlockItemAction } from "@/components/pages/list/block-item-action";
// hooks
import { usePlatformOS } from "@/hooks/use-platform-os";
// plane web hooks
import type { EPageStoreType } from "@/hooks/store";
import { usePage, usePageStore } from "@/hooks/store";

/** Drag payload type for page/folder list DnD. */
export const PAGE_LIST_DND_TYPE = "PAGE_LIST_NODE";

type TPageListBlock = {
  pageId: string;
  storeType: EPageStoreType;
};

export type TPageListDragData = {
  type: typeof PAGE_LIST_DND_TYPE;
  pageId: string;
  nodeType: "page" | "folder";
};

/**
 * Type guard for page-list drag payloads.
 * @param data - Unknown drag data from atlaskit
 * @returns Whether the payload is a page-list drag item
 */
export function isPageListDragData(data: Record<string | symbol, unknown>): data is TPageListDragData {
  return data.type === PAGE_LIST_DND_TYPE && typeof data.pageId === "string";
}

export const PageListBlock = observer(function PageListBlock(props: TPageListBlock) {
  const { pageId, storeType } = props;
  // refs
  const parentRef = useRef<HTMLDivElement>(null);
  // state
  const [isDragging, setIsDragging] = useState(false);
  const [isDropTarget, setIsDropTarget] = useState(false);
  // hooks
  const page = usePage({
    pageId,
    storeType,
  });
  const { getMoveTargetFolders, getPageById, moveToFolder } = usePageStore(storeType);
  const { isMobile } = usePlatformOS();
  const { workspaceSlug, projectId } = useParams();
  const searchParams = useSearchParams();
  const pageType = searchParams.get("type") || "public";

  const isFolder = page?.node_type === "folder";
  const isArchivedView = pageType === "archived" || !!page?.archived_at;
  const canDrag = !!page && page.canCurrentUserMovePage && !isArchivedView;

  useEffect(() => {
    const element = parentRef.current;
    if (!element || !page || isArchivedView) return;

    const dragData: TPageListDragData = {
      type: PAGE_LIST_DND_TYPE,
      pageId,
      nodeType: isFolder ? "folder" : "page",
    };

    const behaviors = [];

    if (canDrag) {
      behaviors.push(
        draggable({
          element,
          getInitialData: () => dragData,
          onDragStart: () => setIsDragging(true),
          onDrop: () => setIsDragging(false),
        })
      );
    }

    if (isFolder) {
      behaviors.push(
        dropTargetForElements({
          element,
          canDrop: ({ source }) => {
            if (!isPageListDragData(source.data)) return false;
            if (source.data.pageId === pageId) return false;
            const sourcePage = getPageById(source.data.pageId);
            if (!sourcePage) return false;
            return getMoveTargetFolders(projectId?.toString() || "", sourcePage.access, source.data.pageId).some(
              (folder) => folder.id === pageId
            );
          },
          getData: () => dragData,
          onDragEnter: () => setIsDropTarget(true),
          onDragLeave: () => setIsDropTarget(false),
          onDrop: ({ source }) => {
            setIsDropTarget(false);
            if (!isPageListDragData(source.data)) return;
            const sourceId = source.data.pageId;
            if (sourceId === pageId) return;
            void moveToFolder(sourceId, pageId)
              .then(() => {
                setToast({
                  type: TOAST_TYPE.SUCCESS,
                  title: "Success!",
                  message: `Moved into ${getPageName(page.name)}.`,
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
        })
      );
    }

    if (!behaviors.length) return;
    return combine(...behaviors);
  }, [canDrag, getMoveTargetFolders, getPageById, isArchivedView, isFolder, moveToFolder, page, pageId, projectId]);

  // handle page check
  if (!page) return null;
  // derived values
  const { name, logo_props, getRedirectionLink, node_type } = page;
  const folderLink = (() => {
    const params = new URLSearchParams();
    params.set("type", pageType);
    params.set("folder", pageId);
    return `/${workspaceSlug}/projects/${projectId}/pages?${params.toString()}`;
  })();

  return (
    <ListItem
      prependTitleElement={
        <>
          {node_type === "folder" ? (
            <Folder className="h-4 w-4 text-tertiary" />
          ) : logo_props?.in_use ? (
            <Logo logo={logo_props} size={16} type="lucide" />
          ) : (
            <PageIcon className="h-4 w-4 text-tertiary" />
          )}
        </>
      }
      title={getPageName(name)}
      itemLink={node_type === "folder" ? folderLink : getRedirectionLink()}
      actionableItems={<BlockItemAction page={page} parentRef={parentRef} storeType={storeType} />}
      isMobile={isMobile}
      parentRef={parentRef}
      className={cn({
        "opacity-50": isDragging,
        "bg-accent-primary/10 ring-1 ring-accent-strong ring-inset": isDropTarget,
        "cursor-grab": canDrag && !isDragging,
      })}
      disableLink={isDragging}
    />
  );
});
