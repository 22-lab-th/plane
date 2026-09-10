/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { observer } from "mobx-react";
import { useSearchParams } from "next/navigation";
import useSWR from "swr";
import type { TPageNavigationTabs } from "@plane/types";
// hooks
import type { EPageStoreType } from "@/hooks/store";
import { usePageStore } from "@/hooks/store";
// local imports
import { PagesListHeaderRoot } from "./header";
import { PagesListMainContent } from "./pages-list-main-content";

type TPageView = {
  children: React.ReactNode;
  pageType: TPageNavigationTabs;
  projectId: string;
  storeType: EPageStoreType;
  workspaceSlug: string;
};

export const PagesListView = observer(function PagesListView(props: TPageView) {
  const { children, pageType, projectId, storeType, workspaceSlug } = props;
  const searchParams = useSearchParams();
  const folderId = searchParams.get("folder");
  // store hooks
  const { isAnyPageAvailable, fetchPagesList, fetchPageDetails, getPageById } = usePageStore(storeType);

  // Ensure deep-linked folder exists in store for breadcrumbs.
  useSWR(
    workspaceSlug && projectId && folderId ? `PROJECT_PAGE_FOLDER_${folderId}` : null,
    workspaceSlug && projectId && folderId && !getPageById(folderId)
      ? () => fetchPageDetails(workspaceSlug, projectId, folderId, { trackVisit: false })
      : null
  );

  // fetching pages list for current folder level
  useSWR(
    workspaceSlug && projectId && pageType ? `PROJECT_PAGES_${projectId}_${folderId || "root"}` : null,
    workspaceSlug && projectId && pageType ? () => fetchPagesList(workspaceSlug, projectId, pageType, folderId) : null
  );

  // pages loader
  return (
    <div className="relative flex h-full w-full flex-col overflow-hidden">
      {/* tab header */}
      {isAnyPageAvailable && (
        <PagesListHeaderRoot
          pageType={pageType}
          projectId={projectId}
          storeType={storeType}
          workspaceSlug={workspaceSlug}
        />
      )}
      <PagesListMainContent pageType={pageType} storeType={storeType}>
        {children}
      </PagesListMainContent>
    </div>
  );
});
