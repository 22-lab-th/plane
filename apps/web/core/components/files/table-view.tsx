/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import type { KeyboardEvent as ReactKeyboardEvent } from "react";
// plane imports
import { ChevronDownIcon } from "@plane/propel/icons";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@plane/propel/table";
import { cn, renderFormattedDate } from "@plane/utils";
import type { IProjectFolder, TProjectFileOrdering } from "@/services/project-file.service";
// helpers
import {
  FILES_FOCUS_RING,
  FILES_ROW_FOCUS_RING,
  FILES_SORT_LABELS,
  fileKind,
  formatFileSize,
  sortDirection,
  type TFilesRow,
  type TFileSortColumn,
} from "./helpers";
import { FolderActions } from "./folder-management";

type Props = {
  rows: TFilesRow[];
  ordering: TProjectFileOrdering;
  onOrderingChange: (ordering: TProjectFileOrdering) => void;
  onOpenFolder: (folderId: string) => void;
  onOpenFile: (fileId: string) => void;
  registerRow: (rowKey: string, element: HTMLElement | null) => void;
  onRowKeyDown: (event: ReactKeyboardEvent<HTMLElement>, rowKey: string) => void;
  onRenameFolder: (folder: IProjectFolder) => void;
  onMoveFolder: (folder: IProjectFolder) => void;
  onDeleteFolder: (folder: IProjectFolder) => void;
  canManageFolders: boolean;
};
/** Owner and Type are the two columns that fold away on a narrow screen. */
const FILES_TABLE_HEADERS: { key: string; column: TFileSortColumn | null; className?: string }[] = [
  { key: "name", column: "name", className: "w-full" },
  { key: "type", column: null, className: "hidden md:table-cell" },
  { key: "size", column: "size" },
  { key: "owner", column: null, className: "hidden md:table-cell" },
  { key: "updated", column: "updated", className: "hidden md:table-cell" },
  { key: "actions", column: null, className: "w-10" },
];

const STATIC_HEADER_LABELS: Record<string, string> = { type: "Type", owner: "Owner", actions: "" };

const ROW_CLASS_NAME = "cursor-pointer border-b border-subtle hover:bg-layer-1";

/** The table view: folders first, then the files, exactly in response order. */
export function FilesTable(props: Props) {
  const {
    rows,
    ordering,
    onOrderingChange,
    onOpenFolder,
    onOpenFile,
    registerRow,
    onRowKeyDown,
    onRenameFolder,
    onMoveFolder,
    onDeleteFolder,
    canManageFolders,
  } = props;

  return (
    <div data-testid="files-view-table" className="flex h-full flex-col">
      <Table>
        <TableHeader>
          <TableRow>
            {FILES_TABLE_HEADERS.map((header) => {
              const sortColumn = header.column;
              if (sortColumn === null)
                return (
                  <TableHead key={header.key} className={header.className}>
                    {STATIC_HEADER_LABELS[header.key]}
                  </TableHead>
                );

              const direction = sortDirection(ordering, sortColumn);

              return (
                <TableHead key={header.key} className={header.className} aria-sort={direction}>
                  <button
                    type="button"
                    data-testid={`files-sort-${sortColumn}`}
                    aria-label={`Sort by ${FILES_SORT_LABELS[sortColumn].toLowerCase()}`}
                    className={cn(
                      "flex items-center gap-1 rounded px-1 py-0.5 text-caption-md-medium",
                      FILES_FOCUS_RING
                    )}
                    onClick={() => onOrderingChange(ordering === sortColumn ? `-${sortColumn}` : sortColumn)}
                  >
                    {FILES_SORT_LABELS[sortColumn]}
                    <ChevronDownIcon
                      className={cn("size-3", {
                        "rotate-180": direction === "ascending",
                        invisible: direction === "none",
                      })}
                      aria-hidden="true"
                    />
                  </button>
                </TableHead>
              );
            })}
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((row) =>
            row.kind === "folder" ? (
              <TableRow
                key={row.key}
                ref={(element) => registerRow(row.key, element)}
                tabIndex={0}
                data-testid={`files-folder-${row.folder.id}`}
                data-name={row.folder.name}
                aria-label={`Open folder ${row.folder.name}`}
                className={cn(ROW_CLASS_NAME, FILES_ROW_FOCUS_RING)}
                onClick={() => onOpenFolder(row.folder.id)}
                onKeyDown={(event) => onRowKeyDown(event, row.key)}
              >
                <TableCell className="text-body-xs-medium text-primary">{row.folder.name}</TableCell>
                <TableCell className="hidden text-caption-md-regular text-tertiary md:table-cell">Folder</TableCell>
                <TableCell className="text-caption-md-regular text-tertiary">—</TableCell>
                <TableCell className="w-10">
                  {canManageFolders && (
                    <FolderActions
                      folder={row.folder}
                      onRename={onRenameFolder}
                      onMove={onMoveFolder}
                      onDelete={onDeleteFolder}
                    />
                  )}
                </TableCell>
              </TableRow>
            ) : (
              <TableRow
                key={row.key}
                ref={(element) => registerRow(row.key, element)}
                tabIndex={0}
                data-testid={`files-row-${row.file.id}`}
                data-name={row.file.name_display}
                data-size={String(row.file.size_bytes)}
                data-kind={fileKind(row.file)}
                data-owner={row.file.uploader?.display_name ?? ""}
                data-updated={row.file.updated_at}
                data-file-id={row.file.id}
                data-pinned={row.file.is_pinned ? "true" : "false"}
                data-extension={row.file.extension}
                aria-label={`Open ${row.file.name_display}`}
                className={cn(ROW_CLASS_NAME, FILES_ROW_FOCUS_RING)}
                onClick={() => onOpenFile(row.file.id)}
                onKeyDown={(event) => onRowKeyDown(event, row.key)}
              >
                <TableCell className="text-body-xs-regular text-primary">{row.file.name_display}</TableCell>
                <TableCell className="hidden text-caption-md-regular text-tertiary uppercase md:table-cell">
                  {row.file.extension}
                </TableCell>
                <TableCell className="text-caption-md-regular text-tertiary">
                  {formatFileSize(row.file.size_bytes)}
                </TableCell>
                <TableCell className="hidden text-caption-md-regular text-tertiary md:table-cell">
                  {row.file.uploader?.display_name ?? "—"}
                </TableCell>
                <TableCell className="hidden text-caption-md-regular text-tertiary md:table-cell">
                  {renderFormattedDate(row.file.updated_at)}
                </TableCell>
                <TableCell className="w-10" />
              </TableRow>
            )
          )}
        </TableBody>
      </Table>
    </div>
  );
}
