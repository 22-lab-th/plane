/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import type { KeyboardEvent as ReactKeyboardEvent } from "react";
// plane imports
import { FavoriteFolderIcon } from "@plane/propel/icons";
import { cn, renderFormattedDate } from "@plane/utils";
// helpers
import { FILES_FOCUS_RING, fileKind, formatFileSize, type TFilesRow } from "./helpers";

type Props = {
  rows: TFilesRow[];
  onOpenFolder: (folderId: string) => void;
  onOpenFile: (fileId: string) => void;
  registerRow: (rowKey: string, element: HTMLElement | null) => void;
  onRowKeyDown: (event: ReactKeyboardEvent<HTMLElement>, rowKey: string) => void;
};

const CARD_CLASS_NAME =
  "flex w-full flex-col gap-1.5 rounded-md border border-subtle bg-layer-2 p-3 text-left hover:bg-layer-2-hover";

/** The grid view. One column below 768px, two columns from 768px, more where there is room. */
export function FilesGrid(props: Props) {
  const { rows, onOpenFolder, onOpenFile, registerRow, onRowKeyDown } = props;

  return (
    <div data-testid="files-view-grid" className="grid grid-cols-1 gap-3 p-4 md:grid-cols-2 xl:grid-cols-3">
      {rows.map((row) =>
        row.kind === "folder" ? (
          <button
            key={row.key}
            ref={(element) => registerRow(row.key, element)}
            type="button"
            data-testid={`files-folder-${row.folder.id}`}
            data-name={row.folder.name}
            aria-label={`Open folder ${row.folder.name}`}
            className={cn(CARD_CLASS_NAME, FILES_FOCUS_RING)}
            onClick={() => onOpenFolder(row.folder.id)}
            onKeyDown={(event) => onRowKeyDown(event, row.key)}
          >
            <div className="flex items-center gap-2">
              <FavoriteFolderIcon className="size-4 shrink-0 text-tertiary" color="currentColor" />
              <span className="truncate text-body-xs-medium text-primary">{row.folder.name}</span>
            </div>
            <span className="text-caption-md-regular text-tertiary">Folder</span>
          </button>
        ) : (
          <button
            key={row.key}
            ref={(element) => registerRow(row.key, element)}
            type="button"
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
            className={cn(CARD_CLASS_NAME, FILES_FOCUS_RING)}
            onClick={() => onOpenFile(row.file.id)}
            onKeyDown={(event) => onRowKeyDown(event, row.key)}
          >
            <div className="flex items-center gap-2">
              <span className="shrink-0 rounded-sm bg-layer-3 px-1.5 py-0.5 text-caption-md-medium text-secondary uppercase">
                {row.file.extension}
              </span>
              <span className="truncate text-body-xs-medium text-primary">{row.file.name_display}</span>
            </div>
            <span className="text-caption-md-regular text-tertiary">
              {formatFileSize(row.file.size_bytes)}
              {row.file.uploader?.display_name ? ` · ${row.file.uploader.display_name}` : ""}
            </span>
            <span className="text-caption-md-regular text-tertiary">
              Updated {renderFormattedDate(row.file.updated_at)}
            </span>
          </button>
        )
      )}
    </div>
  );
}
