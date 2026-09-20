/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { observer } from "mobx-react";
// plane imports
import { GridLayoutIcon, ListLayoutIcon, SearchIcon } from "@plane/propel/icons";
import { IconButton } from "@plane/propel/icon-button";
import { cn } from "@plane/utils";
import type { IProjectFileStorage } from "@/services/project-file.service";
// helpers
import { FILES_FOCUS_RING, formatFileSize, type TFilesViewMode } from "./helpers";

type Props = {
  storage: IProjectFileStorage | undefined;
  searchValue: string;
  onSearchChange: (value: string) => void;
  viewMode: TFilesViewMode;
  onViewModeChange: (mode: TFilesViewMode) => void;
};

/** The empty storage block a first paint uses before the listing answers. */
const EMPTY_STORAGE: IProjectFileStorage = {
  project_used_bytes: 0,
  workspace_used_bytes: 0,
  limit_bytes: 0,
  warn_threshold_pct: 0,
  file_count: 0,
  version_count: 0,
};

/**
 * The view's own header: the title, the debounced search box, the view-mode
 * toggle and the storage chip. Every number in the chip comes from the same
 * listing response the rows are rendered from — the view never asks for storage
 * separately and never recomputes the counts.
 */
export const FilesToolbar = observer(function FilesToolbar(props: Props) {
  const { storage, searchValue, onSearchChange, viewMode, onViewModeChange } = props;

  const resolvedStorage = storage ?? EMPTY_STORAGE;
  // the used share of the ceiling, for the bar and `data-pct`; 0 when no ceiling is set
  const usedPct =
    resolvedStorage.limit_bytes > 0
      ? Math.round((resolvedStorage.project_used_bytes / resolvedStorage.limit_bytes) * 100)
      : 0;

  return (
    <div className="flex flex-col gap-3 border-b border-subtle bg-surface-1 px-4 py-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-body-md-medium text-primary">Files</h1>
        <div className="flex items-center gap-3">
          <div
            data-testid="files-storage"
            data-used={String(resolvedStorage.project_used_bytes)}
            data-limit={String(resolvedStorage.limit_bytes)}
            data-pct={String(usedPct)}
            className="flex items-center gap-2"
          >
            <div className="h-1.5 w-24 overflow-hidden rounded-full bg-layer-2" aria-hidden="true">
              <div className="h-full rounded-full bg-accent-primary" style={{ width: `${Math.min(usedPct, 100)}%` }} />
            </div>
            <span data-testid="files-storage-text" className="text-caption-md-regular text-tertiary">
              {formatFileSize(resolvedStorage.project_used_bytes)} of {formatFileSize(resolvedStorage.limit_bytes)} used
            </span>
          </div>
          <div className="flex items-center gap-1" role="group" aria-label="View mode">
            <IconButton
              variant={viewMode === "table" ? "secondary" : "ghost"}
              size="base"
              icon={ListLayoutIcon}
              aria-label="Table view"
              aria-pressed={viewMode === "table"}
              data-testid="files-view-toggle-table"
              className={cn(FILES_FOCUS_RING, "rounded-md")}
              onClick={() => onViewModeChange("table")}
            />
            <IconButton
              variant={viewMode === "grid" ? "secondary" : "ghost"}
              size="base"
              icon={GridLayoutIcon}
              aria-label="Grid view"
              aria-pressed={viewMode === "grid"}
              data-testid="files-view-toggle-grid"
              className={cn(FILES_FOCUS_RING, "rounded-md")}
              onClick={() => onViewModeChange("grid")}
            />
          </div>
        </div>
      </div>
      <div className="flex w-full max-w-md items-center gap-2 rounded-md border border-strong bg-layer-2 px-2 py-1">
        <SearchIcon className="size-3.5 shrink-0 text-tertiary" aria-hidden="true" />
        <input
          data-testid="files-search"
          type="search"
          value={searchValue}
          onChange={(event) => onSearchChange(event.target.value)}
          placeholder="Search files"
          aria-label="Search files"
          className={cn(
            "w-full border-0 bg-transparent text-body-xs-regular text-primary placeholder:text-placeholder focus:outline-none",
            FILES_FOCUS_RING
          )}
        />
      </div>
    </div>
  );
});
