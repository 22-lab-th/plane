/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useState } from "react";
import { observer } from "mobx-react";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { Folder, FolderPlus } from "lucide-react";
// constants
import { EPageAccess } from "@plane/constants";
// plane types
import { Button } from "@plane/propel/button";
import { PageIcon } from "@plane/propel/icons";
import { TOAST_TYPE, setToast } from "@plane/propel/toast";
import type { TPage } from "@plane/types";
// plane ui
import { Breadcrumbs, Header } from "@plane/ui";
import { getPageName } from "@plane/utils";
// helpers
import { BreadcrumbLink } from "@/components/common/breadcrumb-link";
// hooks
import { useProject } from "@/hooks/store/use-project";
// plane web imports
import { CommonProjectBreadcrumbs } from "@/components/breadcrumbs/common";
import { EPageStoreType, usePageStore } from "@/hooks/store";

export const PagesListHeader = observer(function PagesListHeader() {
  // states
  const [isCreatingPage, setIsCreatingPage] = useState(false);
  const [isCreatingFolder, setIsCreatingFolder] = useState(false);
  // router
  const router = useRouter();
  const { workspaceSlug, projectId } = useParams();
  const searchParams = useSearchParams();
  const pageType = searchParams.get("type") || "public";
  const folderId = searchParams.get("folder");
  // store hooks
  const { currentProjectDetails, loader } = useProject();
  const { canCurrentUserCreatePage, createPage, createFolder, getFolderBreadcrumbs } = usePageStore(
    EPageStoreType.PROJECT
  );
  const folderCrumbs = getFolderBreadcrumbs(folderId);

  const pagesHref = (() => {
    const params = new URLSearchParams();
    params.set("type", pageType);
    return `/${workspaceSlug}/projects/${currentProjectDetails?.id}/pages?${params.toString()}`;
  })();

  const folderHref = (id: string) => {
    const params = new URLSearchParams();
    params.set("type", pageType);
    params.set("folder", id);
    return `/${workspaceSlug}/projects/${currentProjectDetails?.id}/pages?${params.toString()}`;
  };

  // handle page create
  const handleCreatePage = async () => {
    setIsCreatingPage(true);

    const payload: Partial<TPage> = {
      access: pageType === "private" ? EPageAccess.PRIVATE : EPageAccess.PUBLIC,
      parent: folderId,
      node_type: "page",
    };

    await createPage(payload)
      // oxlint-disable-next-line promise/always-return
      .then((res) => {
        const pageId = `/${workspaceSlug}/projects/${currentProjectDetails?.id}/pages/${res?.id}`;
        router.push(pageId);
      })
      .catch((err) => {
        setToast({
          type: TOAST_TYPE.ERROR,
          title: "Error!",
          message: err?.error || err?.data?.error || "Page could not be created. Please try again.",
        });
      })
      .finally(() => setIsCreatingPage(false));
  };

  const handleCreateFolder = async () => {
    setIsCreatingFolder(true);
    const name = window.prompt("Folder name");
    if (!name?.trim()) {
      setIsCreatingFolder(false);
      return;
    }

    await createFolder({
      name: name.trim(),
      access: pageType === "private" ? EPageAccess.PRIVATE : EPageAccess.PUBLIC,
      parent: folderId,
    })
      .then(() => {
        setToast({
          type: TOAST_TYPE.SUCCESS,
          title: "Success!",
          message: "Folder created.",
        });
      })
      .catch((err) => {
        setToast({
          type: TOAST_TYPE.ERROR,
          title: "Error!",
          message: err?.error || err?.data?.error || "Folder could not be created. Please try again.",
        });
      })
      .finally(() => setIsCreatingFolder(false));
  };

  return (
    <Header>
      <Header.LeftItem>
        <Breadcrumbs isLoading={loader === "init-loader"}>
          <CommonProjectBreadcrumbs workspaceSlug={workspaceSlug?.toString()} projectId={projectId?.toString()} />
          <Breadcrumbs.Item
            component={
              <BreadcrumbLink
                label="Pages"
                href={pagesHref}
                icon={<PageIcon className="h-4 w-4 text-tertiary" />}
                isLast={!folderCrumbs.length}
              />
            }
            isLast={!folderCrumbs.length}
          />
          {folderCrumbs.map((crumb, index) => (
            <Breadcrumbs.Item
              key={crumb.id}
              component={
                <BreadcrumbLink
                  label={getPageName(crumb.name)}
                  href={crumb.id ? folderHref(crumb.id) : pagesHref}
                  icon={<Folder className="h-4 w-4 text-tertiary" />}
                  isLast={index === folderCrumbs.length - 1}
                />
              }
              isLast={index === folderCrumbs.length - 1}
            />
          ))}
        </Breadcrumbs>
      </Header.LeftItem>
      {canCurrentUserCreatePage && pageType !== "archived" && (
        <Header.RightItem>
          <Button
            variant="secondary"
            size="lg"
            onClick={handleCreateFolder}
            loading={isCreatingFolder}
            className="flex items-center gap-1"
          >
            <FolderPlus className="h-4 w-4" />
            {isCreatingFolder ? "Adding" : "Add folder"}
          </Button>
          <Button variant="primary" size="lg" onClick={handleCreatePage} loading={isCreatingPage}>
            {isCreatingPage ? "Adding" : "Add page"}
          </Button>
        </Header.RightItem>
      )}
    </Header>
  );
});
