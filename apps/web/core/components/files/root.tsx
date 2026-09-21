/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import type { KeyboardEvent as ReactKeyboardEvent } from "react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { observer } from "mobx-react";
import { usePathname, useSearchParams } from "next/navigation";
import useSWR from "swr";
// plane imports
import { EUserPermissions } from "@plane/constants";
import type { IProjectFileListResponse, TProjectFileOrdering } from "@/services/project-file.service";
import {
  PROJECT_FILE_DEFAULT_ORDERING,
  ProjectFileService,
  isProjectFileOrdering,
} from "@/services/project-file.service";
// components
import { FilesBreadcrumbs } from "./breadcrumbs";
import { FileDetailDrawer } from "./detail-drawer";
import { FilesGrid } from "./grid-view";
// helpers
import {
  buildFilesRows,
  buildListQuery,
  isFilesQuickView,
  isFilesViewMode,
  type TFilesQuickView,
  type TFilesViewMode,
} from "./helpers";
import { FilesQuickViews } from "./quick-views";
import {
  FilesEmptyState,
  FilesErrorBanner,
  FilesErrorState,
  FilesLoadingState,
  FilesNoMatchState,
  FilesReadonlyNotice,
} from "./states";
import { FilesTable } from "./table-view";
import { FilesToolbar } from "./toolbar";
import { FilesUploadSurface, filesUploadTargetName, useFilesUpload } from "./upload";
import { FolderDialogs } from "./folder-management";
import { useFolderManagement } from "./use-folder-management";
// hooks
import { useUserPermissions } from "@/hooks/store/user";
import { useAppRouter } from "@/hooks/use-app-router";

const fileService = new ProjectFileService();

/** How long typing has to pause before the term is turned into a list request. */
const SEARCH_DEBOUNCE_MS = 300;

type Props = {
  workspaceSlug: string;
  projectId: string;
};

/**
 * The Files tab. The URL is the single source of truth for what is being
 * browsed (`folder`, `view`, `q`, `ordering`, `mode`) and for the open drawer
 * (`file`), so every surface here is linkable and the back button walks the
 * same path the user did.
 */
export const ProjectFilesRoot = observer(function ProjectFilesRoot(props: Props) {
  const { workspaceSlug, projectId } = props;

  // router
  const router = useAppRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  // what the URL currently says
  const folderParam = searchParams.get("folder");
  const folderId = folderParam && folderParam !== "root" ? folderParam : null;
  const viewParam = searchParams.get("view");
  const quickView: TFilesQuickView = isFilesQuickView(viewParam) ? viewParam : "all";
  const queryParam = searchParams.get("q") ?? "";
  const orderingParam = searchParams.get("ordering");
  const ordering = isProjectFileOrdering(orderingParam) ? orderingParam : PROJECT_FILE_DEFAULT_ORDERING;
  const modeParam = searchParams.get("mode");
  const fileId = searchParams.get("file");

  // store hooks
  const { getProjectRoleByWorkspaceSlugAndProjectId } = useUserPermissions();
  // the same permission source the navigation item is filtered by: a GUEST lists and reads, and may not mutate
  const projectRole = getProjectRoleByWorkspaceSlugAndProjectId(workspaceSlug, projectId);
  const isReadOnly = projectRole === EUserPermissions.GUEST;
  // purge is the one action the API keeps for project ADMINs (AC-27)
  const isProjectAdmin = projectRole === EUserPermissions.ADMIN;

  // view mode: an explicit `mode` wins, otherwise the breakpoint decides (DESIGN §5)
  const [responsiveViewMode, setResponsiveViewMode] = useState<TFilesViewMode>("table");
  useEffect(() => {
    const query = window.matchMedia("(min-width: 768px) and (max-width: 1023px)");
    const apply = () => setResponsiveViewMode(query.matches ? "grid" : "table");
    apply();
    query.addEventListener("change", apply);
    return () => query.removeEventListener("change", apply);
  }, []);
  const viewMode: TFilesViewMode = isFilesViewMode(modeParam) ? modeParam : responsiveViewMode;

  const updateParams = useCallback(
    (updates: Record<string, string | null>, options: { replace?: boolean } = {}) => {
      const next = new URLSearchParams(searchParams.toString());
      Object.entries(updates).forEach(([key, value]) => {
        if (value === null) next.delete(key);
        else next.set(key, value);
      });
      const queryString = next.toString();
      const target = queryString ? `${pathname}?${queryString}` : pathname;
      if (options.replace) router.replace(target);
      else router.push(target);
    },
    [pathname, router, searchParams]
  );

  // the search box holds its own value; the URL only learns about it once typing pauses
  const [searchInput, setSearchInput] = useState(queryParam);
  const lastPushedQuery = useRef(queryParam);
  const previousQueryParam = useRef(queryParam);

  // the URL changed underneath us (back button, a link): follow it, but never while typing
  useEffect(() => {
    if (previousQueryParam.current === queryParam) return;
    previousQueryParam.current = queryParam;
    if (lastPushedQuery.current === queryParam) return;
    lastPushedQuery.current = queryParam;
    setSearchInput(queryParam);
  }, [queryParam]);

  useEffect(() => {
    const handle = window.setTimeout(() => {
      if (searchInput === queryParam) return;
      lastPushedQuery.current = searchInput;
      updateParams({ q: searchInput ? searchInput : null, file: null });
    }, SEARCH_DEBOUNCE_MS);

    return () => window.clearTimeout(handle);
  }, [searchInput, queryParam, updateParams]);

  // the list request: folder, search, quick view and ordering all come from the URL
  const listQuery = useMemo(
    () => buildListQuery({ folderId, query: queryParam, quickView, ordering }),
    [folderId, ordering, queryParam, quickView]
  );

  const listKey = useMemo(
    () => ["PROJECT_FILES_LIST", workspaceSlug, projectId, listQuery] as const,
    [listQuery, projectId, workspaceSlug]
  );

  // The filter the rendered data belongs to, recorded where the request is made. SWR's
  // cache keys are hashed and its cache is its own concern; the question the view actually
  // asks is "did the request for the filter on screen produce this data?", which only the
  // fetcher can answer. With `keepPreviousData` a failed request for a new filter would
  // otherwise leave the previous filter's rows under the new filter's label.
  // The filter the rendered data belongs to travels *with* the data rather than in a ref:
  // this view remounts during a failed revalidation (observed in the browser), which wipes a
  // component ref and would make the decision false for the rest of the session. With
  // `keepPreviousData` the previous payload survives the failure, so the rows, the chip and
  // the banner all read one source of truth. A fresh mount with no payload is the cold-load
  // case and legitimately shows the error state.
  const currentQuery = JSON.stringify(listQuery);

  const fetchList = useCallback(async () => {
    const query = JSON.stringify(listQuery);
    const response = await fileService.listProjectFiles(workspaceSlug, projectId, listQuery);
    return { query, response };
  }, [listQuery, projectId, workspaceSlug]);

  const { data, error, isLoading, mutate } = useSWR<{ query: string; response: IProjectFileListResponse }>(
    listKey,
    fetchList,
    { keepPreviousData: true, revalidateOnFocus: false }
  );
  const refreshFiles = useCallback(async () => {
    await mutate();
  }, [mutate]);

  const {
    dialog: folderDialog,
    folderTree,
    isFolderTreeLoading,
    folderTreeError,
    loadFolderTree,
    openCreateDialog: handleCreateFolder,
    openRenameDialog: handleRenameFolder,
    openMoveDialog: handleMoveFolder,
    openDeleteDialog: handleDeleteFolder,
    createFolder: handleCreateFolderSubmit,
    renameFolder: handleRenameFolderSubmit,
    moveFolder: handleMoveFolderSubmit,
    deleteFolder: handleDeleteFolderSubmit,
    closeDialog: closeFolderDialog,
  } = useFolderManagement({
    workspaceSlug,
    projectId,
    currentFolderId: folderId,
    onRefresh: refreshFiles,
    updateParams,
  });

  // The rows and the storage chip on screen speak for the filter that produced them, not
  // for the filter the URL now asks for: with an error they are withheld unless the failed
  // request was for the filter on screen, in which case the rows stay and the banner
  // announces the failure.
  const hasError = Boolean(error);
  const dataIsCurrent = data?.query === currentQuery;
  const showErrorState = hasError && (!data || !dataIsCurrent);
  const visibleData = !hasError || dataIsCurrent ? data?.response : undefined;

  const rows = useMemo(() => buildFilesRows(visibleData?.folders ?? [], visibleData?.results ?? []), [visibleData]);
  const rowKeys = useMemo(() => rows.map((row) => row.key), [rows]);

  // keyboard walk over the rows, in the order they are rendered
  const rowRefs = useRef(new Map<string, HTMLElement>());
  const registerRow = useCallback((rowKey: string, element: HTMLElement | null) => {
    if (element) rowRefs.current.set(rowKey, element);
    else rowRefs.current.delete(rowKey);
  }, []);

  const lastOpenedRowKey = useRef<string | null>(null);

  const handleOpenFile = useCallback(
    (selectedFileId: string) => {
      lastOpenedRowKey.current = `file-${selectedFileId}`;
      updateParams({ file: selectedFileId });
    },
    [updateParams]
  );

  /** Both the breadcrumbs and a folder row browse the same way; `null` is the root. */
  const handleOpenFolder = useCallback(
    (selectedFolderId: string | null) => {
      updateParams({ folder: selectedFolderId, file: null });
    },
    [updateParams]
  );

  const openRow = useCallback(
    (rowKey: string) => {
      const row = rows.find((candidate) => candidate.key === rowKey);
      if (!row) return;
      if (row.kind === "folder") handleOpenFolder(row.folder.id);
      else handleOpenFile(row.file.id);
    },
    [handleOpenFile, handleOpenFolder, rows]
  );

  const handleCloseDrawer = useCallback(() => {
    const rowKey = lastOpenedRowKey.current;
    updateParams({ file: null }, { replace: true });
    // focus goes back to the row that opened the drawer (DESIGN §6)
    if (rowKey) window.requestAnimationFrame(() => rowRefs.current.get(rowKey)?.focus());
  }, [updateParams]);

  const handleRowKeyDown = useCallback(
    (event: ReactKeyboardEvent<HTMLElement>, rowKey: string) => {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        const index = rowKeys.indexOf(rowKey);
        if (index === -1) return;
        const nextKey = rowKeys[event.key === "ArrowDown" ? index + 1 : index - 1];
        if (nextKey) rowRefs.current.get(nextKey)?.focus();
        return;
      }

      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        openRow(rowKey);
      }
    },
    [openRow, rowKeys]
  );

  const handleQuickView = useCallback(
    (view: TFilesQuickView) => {
      updateParams({ view: view === "all" ? null : view, file: null });
    },
    [updateParams]
  );

  const handleOrderingChange = useCallback(
    (nextOrdering: TProjectFileOrdering) => {
      updateParams({
        ordering: nextOrdering === PROJECT_FILE_DEFAULT_ORDERING ? null : nextOrdering,
        file: null,
      });
    },
    [updateParams]
  );

  const handleViewModeChange = useCallback(
    (mode: TFilesViewMode) => {
      updateParams({ mode }, { replace: true });
    },
    [updateParams]
  );

  const handleClearFilters = useCallback(() => {
    lastPushedQuery.current = "";
    previousQueryParam.current = "";
    setSearchInput("");
    updateParams({ q: null, view: null, file: null }, { replace: true });
  }, [updateParams]);

  const hasFilters = queryParam.trim().length > 0 || quickView !== "all";
  const hasNoFiles = !!visibleData && visibleData.results.length === 0;
  const hasNoRows = hasNoFiles && visibleData.folders.length === 0;

  // The storage block the header chip shows, withheld from a failed request that was not
  // for the filter on screen — the upload surface reads the same one, so the quota
  // notices and the chip can never disagree about what the response said.
  const visibleStorage = hasError && !dataIsCurrent ? undefined : data?.response?.storage;
  const listedFiles = useMemo(() => visibleData?.results ?? [], [visibleData]);
  const handleStored = useCallback(() => {
    void mutate();
  }, [mutate]);
  const handleRestored = useCallback(() => {
    void mutate();
    // Trash no longer contains the restored row. Keep every unrelated browse filter.
    updateParams({ view: null, file: null }, { replace: true });
  }, [mutate, updateParams]);

  const upload = useFilesUpload({
    workspaceSlug,
    projectId,
    folderId,
    folderName: filesUploadTargetName(visibleData?.breadcrumbs ?? [], folderId),
    listedFiles,
    storage: visibleStorage,
    canUpload: !isReadOnly,
    onStored: handleStored,
  });

  return (
    <div data-testid="files-root" className="flex h-full w-full flex-col overflow-hidden" {...upload.dropHandlers}>
      <FilesToolbar
        storage={visibleStorage}
        searchValue={searchInput}
        onSearchChange={setSearchInput}
        viewMode={viewMode}
        onViewModeChange={handleViewModeChange}
        onUpload={isReadOnly ? undefined : upload.openPicker}
        onCreateFolder={isReadOnly ? undefined : handleCreateFolder}
        uploadDisabledReason={upload.isFull ? (upload.quota?.exceeded ?? undefined) : undefined}
      />
      <FilesBreadcrumbs
        breadcrumbs={dataIsCurrent || !hasError ? (data?.response?.breadcrumbs ?? []) : []}
        onNavigate={handleOpenFolder}
      />
      {isReadOnly && <FilesReadonlyNotice />}
      <FilesUploadSurface upload={upload} />
      {hasError && dataIsCurrent && <FilesErrorBanner onRetry={() => void mutate()} />}
      <div
        data-testid={isReadOnly ? undefined : "files-upload-dropzone"}
        className="grid min-h-0 flex-1 grid-cols-1 xl:grid-cols-[180px_minmax(0,1fr)]"
      >
        <FilesQuickViews
          activeView={quickView}
          onSelect={handleQuickView}
          className="border-b border-subtle xl:flex-col xl:items-stretch xl:border-r xl:border-b-0"
        />
        <div className="min-h-0 overflow-y-auto">
          {isLoading && !data ? (
            <FilesLoadingState />
          ) : showErrorState ? (
            <FilesErrorState onRetry={() => void mutate()} />
          ) : hasNoFiles && hasFilters ? (
            // `q` filters files only, so the folders the API still returns stay on
            // screen and only the file list reports the miss (DESIGN §7: "No files
            // match your filters." + Clear filters).
            <>
              {rows.length > 0 &&
                (viewMode === "grid" ? (
                  <FilesGrid
                    rows={rows}
                    onOpenFolder={handleOpenFolder}
                    onOpenFile={handleOpenFile}
                    registerRow={registerRow}
                    onRenameFolder={handleRenameFolder}
                    onMoveFolder={handleMoveFolder}
                    onDeleteFolder={handleDeleteFolder}
                    canManageFolders={!isReadOnly}
                    onRowKeyDown={handleRowKeyDown}
                  />
                ) : (
                  <FilesTable
                    rows={rows}
                    ordering={ordering}
                    onOrderingChange={handleOrderingChange}
                    onOpenFolder={handleOpenFolder}
                    onOpenFile={handleOpenFile}
                    registerRow={registerRow}
                    onRenameFolder={handleRenameFolder}
                    onMoveFolder={handleMoveFolder}
                    onDeleteFolder={handleDeleteFolder}
                    canManageFolders={!isReadOnly}
                    onRowKeyDown={handleRowKeyDown}
                  />
                ))}
              <FilesNoMatchState onClearFilters={handleClearFilters} />
            </>
          ) : hasNoRows ? (
            <FilesEmptyState
              variant={folderId ? "folder" : "project"}
              onUpload={isReadOnly ? undefined : upload.openPicker}
            />
          ) : viewMode === "grid" ? (
            <FilesGrid
              rows={rows}
              onRenameFolder={handleRenameFolder}
              onMoveFolder={handleMoveFolder}
              canManageFolders={!isReadOnly}
              onDeleteFolder={handleDeleteFolder}
              onOpenFolder={handleOpenFolder}
              onOpenFile={handleOpenFile}
              registerRow={registerRow}
              onRowKeyDown={handleRowKeyDown}
            />
          ) : (
            <FilesTable
              rows={rows}
              ordering={ordering}
              onOrderingChange={handleOrderingChange}
              onOpenFolder={handleOpenFolder}
              onOpenFile={handleOpenFile}
              onRenameFolder={handleRenameFolder}
              onMoveFolder={handleMoveFolder}
              onDeleteFolder={handleDeleteFolder}
              canManageFolders={!isReadOnly}
              registerRow={registerRow}
              onRowKeyDown={handleRowKeyDown}
            />
          )}
        </div>
      </div>
      {fileId && (
        <FileDetailDrawer
          // A different file is a different drawer: no preview URL, version choice or
          // open question may carry across (DESIGN §8).
          key={fileId}
          workspaceSlug={workspaceSlug}
          projectId={projectId}
          fileId={fileId}
          trashed={quickView === "trash"}
          isProjectAdmin={isProjectAdmin}
          onClose={handleCloseDrawer}
          onRestored={handleRestored}
          onFileMutated={handleStored}
        />
      )}
      <FolderDialogs
        dialog={folderDialog}
        folders={folderTree}
        isFolderTreeLoading={isFolderTreeLoading}
        folderTreeError={folderTreeError}
        onRetryFolderTree={() => void loadFolderTree()}
        onClose={closeFolderDialog}
        onCreate={handleCreateFolderSubmit}
        onRename={handleRenameFolderSubmit}
        onMove={handleMoveFolderSubmit}
        onDelete={handleDeleteFolderSubmit}
      />
    </div>
  );
});
