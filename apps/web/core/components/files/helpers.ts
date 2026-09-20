/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import type {
  IProjectFile,
  IProjectFileStorage,
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
 * Client-side pre-validation rules for uploads (R-UPL-7).
 *
 * The server stays authoritative; these two mirror the settings the server validates
 * against, `PROJECT_FILE_MAX_BYTES` and `PROJECT_FILE_MIME_TYPES` in
 * `apps/api/plane/settings/common.py`, so a file the user is told to fix here is a file
 * the server would have refused with the same rule. The list is that setting verbatim:
 * nothing may be refused on the client that the server would accept, and the server
 * refuses every type outside this list.
 */
export const FILES_UPLOAD_MAX_BYTES = 26_214_400;

export const FILES_UPLOAD_MIME_TYPES: readonly string[] = [
  "image/jpeg",
  "image/png",
  "image/gif",
  "image/svg+xml",
  "image/webp",
  "image/tiff",
  "image/bmp",
  "application/pdf",
  "application/msword",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  "application/vnd.ms-excel",
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  "application/vnd.ms-powerpoint",
  "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  "text/plain",
  "text/markdown",
  "application/rtf",
  "application/vnd.oasis.opendocument.spreadsheet",
  "application/vnd.oasis.opendocument.text",
  "application/vnd.oasis.opendocument.presentation",
  "application/vnd.oasis.opendocument.graphics",
  "application/vnd.visio",
  "image/x-portable-graymap",
  "image/x-portable-bitmap",
  "image/x-portable-pixmap",
  "application/vnd.oasis.opendocument.database",
  "audio/mpeg",
  "audio/wav",
  "audio/ogg",
  "audio/midi",
  "audio/x-midi",
  "audio/aac",
  "audio/flac",
  "audio/x-m4a",
  "video/mp4",
  "video/mpeg",
  "video/ogg",
  "video/webm",
  "video/quicktime",
  "video/x-msvideo",
  "video/x-ms-wmv",
  "application/zip",
  "application/x-rar",
  "application/x-rar-compressed",
  "application/x-tar",
  "application/gzip",
  "application/x-zip",
  "application/x-zip-compressed",
  "application/x-7z-compressed",
  "application/x-compressed",
  "application/x-compressed-tar",
  "application/x-compressed-tar-gz",
  "application/x-compressed-tar-bz2",
  "application/x-compressed-tar-zip",
  "application/x-compressed-tar-7z",
  "application/x-compressed-tar-rar",
  "model/gltf-binary",
  "model/gltf+json",
  "application/octet-stream",
  "font/ttf",
  "font/otf",
  "font/woff",
  "font/woff2",
  "text/css",
  "text/javascript",
  "application/json",
  "text/xml",
  "text/csv",
  "application/xml",
  "application/x-sql",
  "application/x-gzip",
];

/**
 * The types a browser reports as an empty string for an extension it does not know
 * (`.log`, `.tsv`, `.sql`), mapped to the type the server's allowlist accepts.
 * The browser cannot determine MIME reliably (R-UPL-7), so the extension is the only
 * remaining source — and the resolved value is what the client *declares* to the
 * server, which is then checked against the same allowlist.
 */
const FILES_UPLOAD_MIME_BY_EXTENSION: Record<string, string> = {
  txt: "text/plain",
  log: "text/plain",
  md: "text/markdown",
  markdown: "text/markdown",
  csv: "text/csv",
  tsv: "text/csv",
  json: "application/json",
  xml: "application/xml",
  sql: "application/x-sql",
  pdf: "application/pdf",
  zip: "application/zip",
  tar: "application/x-tar",
  gz: "application/gzip",
  "7z": "application/x-7z-compressed",
  rar: "application/x-rar",
  woff2: "font/woff2",
  obj: "application/octet-stream",
};

/** The extension of a name, lowercased and without the dot (empty when there is none). */
export const uploadFileExtension = (fileName: string): string => {
  const [, ...rest] = fileName.toLowerCase().split(".");
  return rest.length > 0 ? (rest.at(-1) ?? "") : "";
};

/**
 * The content type an upload would declare: what the browser reported, or — for a
 * file the browser could not type — what the extension says, normalised as
 * `normalize_mime_type` normalises it (lowercased, parameters dropped).
 */
export const resolveUploadMimeType = (fileName: string, declaredType: string): string => {
  const reported = (declaredType || "").split(";")[0].trim().toLowerCase();
  if (reported) return reported;
  return FILES_UPLOAD_MIME_BY_EXTENSION[uploadFileExtension(fileName)] ?? "";
};

export type TUploadRejectionReason = "size" | "type" | "quota";

export type TUploadValidation =
  | { ok: true; mimeType: string }
  | { ok: false; reason: TUploadRejectionReason; mimeType: string };

/**
 * Pre-check one dropped or picked file, in the order the server checks it: size first
 * (a 30 MiB `.exe` is refused for its size), then the type. The resolved type always
 * comes back, so the row can name the type it refused.
 */
export const validateUploadCandidate = (candidate: {
  name: string;
  sizeBytes: number;
  declaredType: string;
}): TUploadValidation => {
  const mimeType = resolveUploadMimeType(candidate.name, candidate.declaredType);

  if (candidate.sizeBytes > FILES_UPLOAD_MAX_BYTES) return { ok: false, reason: "size", mimeType };
  if (!FILES_UPLOAD_MIME_TYPES.includes(mimeType)) return { ok: false, reason: "type", mimeType };

  return { ok: true, mimeType };
};

// --- preview and version status (T-114) -------------------------------------

/**
 * The types the drawer must never render inline, whatever a response says.
 *
 * SVG is XML that can carry script and HTML is script; serving either inline on the
 * application's origin is the documented token-theft class R-LEG-3 exists for. The
 * server already signs them `attachment` (AC-08); naming them here means the drawer
 * cannot start inlining them if the allowlist below is ever widened.
 */
const FILES_NEVER_INLINE_MIME_TYPES = new Set([
  "image/svg+xml",
  "text/html",
  "application/xhtml+xml",
  "text/xml",
  "application/xml",
  "text/javascript",
  "application/javascript",
]);

/**
 * Whether the drawer may render a live URL for this type inside the panel.
 *
 * Raster images only, and never SVG or HTML: an `<img>` cannot execute script, which
 * is the whole reason this is the one element the drawer renders uploaded bytes with.
 * The only other option for the types the server also signs `inline` - PDF and the
 * plain-text family - is an `<iframe>`, and that trade does not pay: Chrome's PDF
 * viewer renders nothing at all in a sandboxed frame (measured), while an unsandboxed
 * one would run uploaded PDF script inside this page. Those types get the type tile and
 * their Download action instead (DESIGN §8, AC-08).
 *
 * A stricter client than the server's `is_inline_safe` is deliberate - the server's
 * allowlist is a superset of what may be *rendered here* - and an unknown, empty or
 * script-capable type is refused (fail-closed).
 *
 * This is only half the decision: the render also requires the payload to report
 * `disposition: "inline"`, because the server is the one that knows whether the object
 * it verified is the type it claims.
 */
export const isInlineRenderableMime = (mimeType: string): boolean => {
  const mime = (mimeType || "").split(";")[0].trim().toLowerCase();
  if (!mime) return false;
  if (FILES_NEVER_INLINE_MIME_TYPES.has(mime)) return false;
  return mime.startsWith("image/");
};

/**
 * True for the types the drawer never renders inline, so the tile can say why: SVG and
 * HTML read differently from a type that simply has no preview (R-LEG-3, DESIGN §8).
 */
export const isAlwaysDownloadMime = (mimeType: string): boolean =>
  FILES_NEVER_INLINE_MIME_TYPES.has((mimeType || "").split(";")[0].trim().toLowerCase());

/** The version statuses the API records, as the history's chip names them. */
export const VERSION_STATUS_LABELS: Record<string, string> = {
  active: "Active",
  superseded: "Superseded",
  uploading: "Uploading",
  failed: "Failed",
  purged: "Purged",
  purge_failed: "Purge failed",
};

/**
 * The audit actions in the words the activity list uses (R-AUD-1, AC-18). The
 * spelling comes from `FileAccessLog.Action`; an action this map does not know is
 * shown as the API recorded it rather than hidden.
 */
export const FILE_ACTIVITY_LABELS: Record<string, string> = {
  upload_initiated: "started an upload",
  upload_completed: "completed an upload",
  upload_failed: "had an upload fail",
  version_created: "uploaded a version",
  version_activated: "made a version active",
  downloaded: "downloaded",
  previewed: "previewed",
  renamed: "renamed",
  moved: "moved",
  copied: "copied",
  linked: "linked",
  unlinked: "unlinked",
  trashed: "moved to trash",
  restored: "restored",
  purged: "purged",
  permission_denied: "was refused for lack of permission",
  quota_rejected: "was refused for quota",
  folder_created: "created a folder",
  folder_renamed: "renamed a folder",
  folder_moved: "moved a folder",
  folder_deleted: "deleted a folder",
};

/** One audit row as the activity list reads it, from the row the API returned. */
export const fileActivityCopy = (entry: {
  action: string;
  version_no: number | null;
  actor: { display_name: string | null } | null;
  actor_display: string | null;
}): { actor: string; action: string } => {
  const actor = entry.actor?.display_name || entry.actor_display || "Unknown actor";
  const label = FILE_ACTIVITY_LABELS[entry.action] ?? entry.action;
  return { actor, action: entry.version_no === null ? label : `${label} v${entry.version_no}` };
};

// --- the copy these surfaces show (DES-001 §7) ------------------------------

export const uploadTooLargeCopy = (limitBytes: number): string =>
  `Too large — the limit is ${formatFileSize(limitBytes)}.`;

export const uploadTypeNotAllowedCopy = (fileName: string, mimeType: string): string =>
  `File type not allowed: ${mimeType || `.${uploadFileExtension(fileName)}`}.`;

export const quotaWarningCopy = (usedPct: number): string =>
  `You are using ${usedPct}% of the workspace storage limit.`;

export const quotaExceededCopy = (usedBytes: number, limitBytes: number): string =>
  `Storage limit reached — ${formatFileSize(usedBytes)} of ${formatFileSize(limitBytes)}. Delete files or raise the limit.`;

/** The used share of the project's effective ceiling; 0 when no ceiling is set. */
export const storageUsedPct = (storage: Pick<IProjectFileStorage, "project_used_bytes" | "limit_bytes">): number =>
  storage.limit_bytes > 0 ? Math.round((storage.project_used_bytes / storage.limit_bytes) * 100) : 0;

/** The question a completed revision upload ends in (EXP-001 F-09 step 2, AC-43). */
export const versionActivationAskCopy = (versionNo: number): string =>
  `Make v${versionNo} the active version? The current version stays available in the history.`;

export const versionActivatedCopy = (versionNo: number): string => `Version ${versionNo} is now active.`;

/** A stored revision whose activation is still open. */
export const versionSavedCopy = (versionNo: number): string => `Version ${versionNo} saved.`;

/** Declining the question: the new revision stays, the pointer does not move (AC-43). */
export const versionDeclinedCopy = (versionNo: number, activeVersionNo: number): string =>
  `Version ${versionNo} saved — v${activeVersionNo} is still the active version.`;

/** A restore that could not return the file to its folder (DES-001 §7). */
export const restoredToRootCopy = "Restored to the project root — the original folder no longer exists.";

/** The purge confirmation's question (DES-001 §7); the body names the irreversibility. */
export const purgeConfirmCopy = (name: string): string => `Permanently delete ${name}? This cannot be undone.`;

/**
 * The folder anchor that means "the project root" (the API's ``folder_id=root``).
 *
 * It is not the same as omitting ``folder_id``: the endpoint reads an absent filter
 * as "every file in the project, at any depth", so a file placed in a folder used to
 * appear at the root as well as inside it (DEFECT-005). The tab always asks for one
 * folder - the root is a folder like any other - so the two can never be confused.
 */
export const FILES_ROOT_FOLDER = "root";

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
  // Only the live browse is folder-scoped. Trash, Pinned and Recent are project-wide by
  // purpose — they exist to find a file again — and a file trashed through its folder
  // keeps a `folder_id` whose folder no longer resolves, so scoping those views to one
  // folder would hide exactly the rows they are for (R-DEL-3's restore surface, T-118 F-1).
  if (state.quickView === "all") listQuery.folder_id = state.folderId ?? FILES_ROOT_FOLDER;
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
