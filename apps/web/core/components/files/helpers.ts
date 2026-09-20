/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import type {
  IProjectFile,
  IProjectFolder,
  TProjectFileListQuery,
  TProjectFileOrdering,
} from "@/services/project-file.service";

/**
 * The visible focus ring every keyboard-reachable surface in this view carries
 * (DESIGN §6). Rows and controls share one string so the ring cannot drift.
 */
export const FILES_FOCUS_RING =
  "focus-visible:ring-2 focus-visible:ring-accent-strong focus-visible:ring-inset focus-visible:outline-none";

/**
 * The same ring for a focusable `<tr>`. A table row is `display: table-row`, where a
 * box-shadow (Tailwind's `ring`) is not painted, so rows carry an outline instead.
 */
export const FILES_ROW_FOCUS_RING =
  "focus-visible:outline-2 focus-visible:outline-solid focus-visible:outline-offset-[-2px] focus-visible:outline-accent-strong focus-visible:ring-0";

/**
 * The quick views the API can express. "Orphan" is deliberately absent: the
 * listing endpoint has no filter for it, so offering it would mean inventing a
 * result set on the client.
 */
export type TFilesQuickView = "all" | "recent" | "pinned" | "trash";

export const FILES_QUICK_VIEWS: { key: TFilesQuickView; label: string }[] = [
  { key: "all", label: "All" },
  { key: "recent", label: "Recent" },
  { key: "pinned", label: "Pinned" },
  { key: "trash", label: "Trash" },
];

export const isFilesQuickView = (value: string | null): value is TFilesQuickView =>
  value === "all" || value === "recent" || value === "pinned" || value === "trash";

/** How "Recent" bounds the listing: files created in the last 30 days. */
export const FILES_RECENT_WINDOW_DAYS = 30;

export type TFilesViewMode = "table" | "grid";

export const isFilesViewMode = (value: string | null): value is TFilesViewMode => value === "table" || value === "grid";

/** The columns the table exposes a sort header for. */
export type TFileSortColumn = "name" | "size" | "updated";

export const FILES_SORT_LABELS: Record<TFileSortColumn, string> = {
  name: "Name",
  size: "Size",
  updated: "Updated",
};

export const sortDirection = (
  ordering: TProjectFileOrdering,
  column: TFileSortColumn
): "ascending" | "descending" | "none" => {
  if (ordering === column) return "ascending";
  if (ordering === `-${column}`) return "descending";
  return "none";
};

/** The coarse display kind a row advertises; the API's `category` is a storage class, not a kind. */
export type TFileKind =
  | "image"
  | "video"
  | "audio"
  | "pdf"
  | "document"
  | "spreadsheet"
  | "presentation"
  | "archive"
  | "code"
  | "text"
  | "other";

const KIND_BY_EXTENSION: Record<string, TFileKind> = {
  png: "image",
  jpg: "image",
  jpeg: "image",
  gif: "image",
  webp: "image",
  bmp: "image",
  svg: "image",
  ico: "image",
  avif: "image",
  heic: "image",
  mp4: "video",
  mov: "video",
  webm: "video",
  avi: "video",
  mkv: "video",
  m4v: "video",
  mp3: "audio",
  wav: "audio",
  ogg: "audio",
  m4a: "audio",
  flac: "audio",
  aac: "audio",
  pdf: "pdf",
  doc: "document",
  docx: "document",
  odt: "document",
  rtf: "document",
  xls: "spreadsheet",
  xlsx: "spreadsheet",
  csv: "spreadsheet",
  ods: "spreadsheet",
  ppt: "presentation",
  pptx: "presentation",
  odp: "presentation",
  key: "presentation",
  zip: "archive",
  rar: "archive",
  "7z": "archive",
  tar: "archive",
  gz: "archive",
  bz2: "archive",
  xz: "archive",
  js: "code",
  jsx: "code",
  ts: "code",
  tsx: "code",
  json: "code",
  yml: "code",
  yaml: "code",
  html: "code",
  css: "code",
  scss: "code",
  py: "code",
  rb: "code",
  go: "code",
  rs: "code",
  java: "code",
  php: "code",
  sql: "code",
  sh: "code",
  toml: "code",
  xml: "code",
  txt: "text",
  md: "text",
  markdown: "text",
  log: "text",
};

const kindFromMimeType = (mimeType: string): TFileKind | undefined => {
  if (mimeType.startsWith("image/")) return "image";
  if (mimeType.startsWith("video/")) return "video";
  if (mimeType.startsWith("audio/")) return "audio";
  if (mimeType === "application/pdf") return "pdf";
  if (mimeType.includes("wordprocessingml") || mimeType === "application/msword" || mimeType === "application/rtf")
    return "document";
  if (mimeType.includes("spreadsheetml") || mimeType === "application/vnd.ms-excel") return "spreadsheet";
  if (mimeType.includes("presentationml") || mimeType === "application/vnd.ms-powerpoint") return "presentation";
  if (
    mimeType === "application/zip" ||
    mimeType === "application/gzip" ||
    mimeType === "application/x-tar" ||
    mimeType.endsWith("-compressed") ||
    mimeType.endsWith("+zip")
  )
    return "archive";
  if (
    mimeType.includes("javascript") ||
    mimeType.includes("json") ||
    mimeType.includes("xml") ||
    mimeType.includes("yaml")
  )
    return "code";
  return undefined;
};

export const fileKind = (file: Pick<IProjectFile, "mime_type" | "extension">): TFileKind => {
  const mimeType = (file.mime_type ?? "").toLowerCase();
  const fromMimeType = kindFromMimeType(mimeType);
  if (fromMimeType) return fromMimeType;

  const extension = (file.extension ?? "").toLowerCase().replace(/^\./, "");
  const fromExtension = KIND_BY_EXTENSION[extension];
  if (fromExtension) return fromExtension;

  return mimeType.startsWith("text/") ? "text" : "other";
};

const FILE_SIZE_UNITS = ["B", "KB", "MB", "GB", "TB"] as const;

/** A human size for display; the exact bytes stay available as `size_bytes`. */
export const formatFileSize = (bytes: number): string => {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";

  let value = bytes;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < FILE_SIZE_UNITS.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }

  const rounded = Number.isInteger(value) || value >= 10 ? Math.round(value).toString() : value.toFixed(1);
  return `${rounded} ${FILE_SIZE_UNITS[unitIndex]}`;
};

/**
 * One rendered row. Folders come first because the listing reports them
 * separately from the files; within each group the response order is kept
 * untouched. Both views and the keyboard walk read this single list, so the
 * order on screen and the order focus moves in cannot disagree.
 */
export type TFilesRow =
  | { key: string; kind: "folder"; folder: IProjectFolder }
  | { key: string; kind: "file"; file: IProjectFile };

export const buildFilesRows = (folders: IProjectFolder[], files: IProjectFile[]): TFilesRow[] => [
  ...folders.map((folder) => ({ key: `folder-${folder.id}`, kind: "folder" as const, folder })),
  ...files.map((file) => ({ key: `file-${file.id}`, kind: "file" as const, file })),
];

/**
 * What the URL asks the API for. The URL is the only place the browse state
 * lives, so this mapping is the single definition of what each folder, search
 * term, quick view and ordering means as a request.
 */
export const buildListQuery = (state: {
  folderId: string | null;
  query: string;
  quickView: TFilesQuickView;
  ordering: TProjectFileOrdering;
}): TProjectFileListQuery => {
  const listQuery: TProjectFileListQuery = { ordering: state.ordering };
  if (state.folderId) listQuery.folder_id = state.folderId;
  if (state.query) listQuery.q = state.query;
  if (state.quickView === "pinned") listQuery.pinned = true;
  if (state.quickView === "trash") listQuery.trashed = true;
  if (state.quickView === "recent") listQuery.created_from = recentWindowStart();
  return listQuery;
};

/** The lower bound the "Recent" quick view sends, as an ISO date. */
export const recentWindowStart = (now: Date = new Date()): string => {
  const start = new Date(now.getTime());
  start.setDate(start.getDate() - FILES_RECENT_WINDOW_DAYS);
  return start.toISOString().slice(0, 10);
};
