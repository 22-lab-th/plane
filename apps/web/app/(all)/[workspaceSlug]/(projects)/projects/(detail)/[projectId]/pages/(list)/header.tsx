/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useRef, useState } from "react";
import { observer } from "mobx-react";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { FileUp, Folder, FolderInput, FolderPlus } from "lucide-react";
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
import { FolderNameModal } from "@/components/pages/modals/folder-name-modal";
import { MarkdownImportReportModal } from "@/components/pages/modals/markdown-import-report-modal";
import { importMarkdownPages, type TMarkdownPageImportReport } from "@/helpers/markdown-page-import";
// hooks
import { useProject } from "@/hooks/store/use-project";
// plane web imports
import { CommonProjectBreadcrumbs } from "@/components/breadcrumbs/common";
import { EPageStoreType, usePageStore } from "@/hooks/store";

export const PagesListHeader = observer(function PagesListHeader() {
  // states
  const [isCreatingPage, setIsCreatingPage] = useState(false);
  const [isCreatingFolder, setIsCreatingFolder] = useState(false);
  const [isImportingMarkdown, setIsImportingMarkdown] = useState(false);
  const [folderModalOpen, setFolderModalOpen] = useState(false);
  const [importReport, setImportReport] = useState<TMarkdownPageImportReport | null>(null);
  const markdownInputRef = useRef<HTMLInputElement>(null);
  const markdownFolderInputRef = useRef<HTMLInputElement>(null);
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
        return;
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

  const handleCreateFolder = async (name: string) => {
    setIsCreatingFolder(true);
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
        return;
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

  const handleMarkdownImport = async (files: File[]) => {
    if (!files.length || !currentProjectDetails?.id) return;
    setIsImportingMarkdown(true);
    try {
      const report = await importMarkdownPages({
        access: pageType === "private" ? EPageAccess.PRIVATE : EPageAccess.PUBLIC,
        createFolder,
        createPage,
        files,
        parentId: folderId,
        projectId: currentProjectDetails.id,
        workspaceSlug: workspaceSlug.toString(),
      });

      setToast({
        type: TOAST_TYPE.SUCCESS,
        title: "Markdown imported",
        message: `${report.pagesImported} page${report.pagesImported === 1 ? "" : "s"} and ${report.assetsUploaded} image${report.assetsUploaded === 1 ? "" : "s"} imported${report.warnings.length ? ` with ${report.warnings.length} warning${report.warnings.length === 1 ? "" : "s"}` : ""}.`,
      });
      setImportReport(report);
    } catch (err: any) {
      setToast({
        type: TOAST_TYPE.ERROR,
        title: "Import failed",
        message: err?.error || err?.data?.error || err?.message || "Markdown could not be imported. Please try again.",
      });
    } finally {
      setIsImportingMarkdown(false);
    }
  };

  const handleMarkdownFileChange = (event: React.ChangeEvent<HTMLInputElement>) => {
    const files = [...(event.target.files || [])];
    event.target.value = "";
    if (files.length) void handleMarkdownImport(files);
  };

  return (
    <Header>
      <FolderNameModal
        isOpen={folderModalOpen}
        onClose={() => setFolderModalOpen(false)}
        onSubmit={handleCreateFolder}
        title="Create folder"
        submitLabel="Create"
        submittingLabel="Creating"
      />
      <MarkdownImportReportModal
        report={importReport}
        onClose={() => setImportReport(null)}
        onOpenPage={(pageId) => {
          setImportReport(null);
          router.push(`/${workspaceSlug}/projects/${currentProjectDetails?.id}/pages/${pageId}`);
        }}
      />
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
          <input
            ref={markdownInputRef}
            type="file"
            accept=".md,.markdown,.zip,text/markdown,application/zip"
            multiple
            className="hidden"
            onChange={handleMarkdownFileChange}
          />
          <input
            ref={markdownFolderInputRef}
            type="file"
            multiple
            className="hidden"
            onChange={handleMarkdownFileChange}
            {...({ directory: "", webkitdirectory: "" } as React.InputHTMLAttributes<HTMLInputElement>)}
          />
          <Button
            variant="secondary"
            size="lg"
            onClick={() => setFolderModalOpen(true)}
            loading={isCreatingFolder}
            className="flex items-center gap-1"
          >
            <FolderPlus className="h-4 w-4" />
            {isCreatingFolder ? "Adding" : "Add folder"}
          </Button>
          <Button
            variant="secondary"
            size="lg"
            onClick={() => markdownInputRef.current?.click()}
            loading={isImportingMarkdown}
            disabled={isCreatingPage || isCreatingFolder}
            className="flex items-center gap-1"
          >
            <FileUp className="h-4 w-4" />
            {isImportingMarkdown ? "Importing" : "Import Markdown"}
          </Button>
          <Button
            variant="secondary"
            size="lg"
            onClick={() => markdownFolderInputRef.current?.click()}
            loading={isImportingMarkdown}
            disabled={isCreatingPage || isCreatingFolder}
            className="flex items-center gap-1"
          >
            <FolderInput className="h-4 w-4" />
            Import folder
          </Button>
          <Button variant="primary" size="lg" onClick={handleCreatePage} loading={isCreatingPage}>
            {isCreatingPage ? "Adding" : "Add page"}
          </Button>
        </Header.RightItem>
      )}
    </Header>
  );
});
