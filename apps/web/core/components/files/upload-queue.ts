/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// services
import type {
  IProjectFile,
  IProjectFileUploadInitiation,
  TPresignedUploadHandle,
} from "@/services/project-file.service";
import { ProjectFileService, putPresignedFile } from "@/services/project-file.service";
// helpers
import {
  FILES_UPLOAD_MAX_BYTES,
  uploadTooLargeCopy,
  uploadTypeNotAllowedCopy,
  validateUploadCandidate,
} from "./helpers";

const fileService = new ProjectFileService();

/**
 * The codes a completion answers with when the object it found did not match the
 * declaration. These are terminal for the attempt: the bytes are already stored (or
 * absent), so retrying the same file would fail the same check and the row offers no
 * retry.
 */
const UNVERIFIED_CODES: Record<string, true> = {
  object_missing: true,
  size_mismatch: true,
  mime_mismatch: true,
  verification_failed: true,
};

/** What one row of the queue is doing (DES-001 §3, EXP-001 F-02). */
export type TUploadRowStatus =
  | "queued"
  | "uploading"
  | "finalizing"
  | "collision"
  | "rejected"
  | "quota"
  | "failed"
  | "unverified"
  | "cancelled"
  | "saved";

export const UPLOAD_QUEUED_COPY = "Waiting to upload…";
export const UPLOAD_VERIFYING_COPY = "Verifying the upload…";
export const UPLOAD_CANCELLED_COPY = "Cancelled.";
export const UPLOAD_UNVERIFIED_COPY = "This upload could not be verified and was discarded.";

/** One file in the upload queue. */
export type TUploadRow = {
  id: string;
  file: File;
  /**
   * The display name; the initiation response's `name_display` replaces it (R-FOLD-5).
   * Between the collision decision and that response the row carries the name the modal
   * promised, so the rest of the batch predicts from it (see `keepBothNameFor`).
   */
  name: string;
  sizeBytes: number;
  /** The type the row declares, resolved by `resolveUploadMimeType`. */
  mimeType: string;
  status: TUploadRowStatus;
  progress: number;
  message: string;
  /**
   * The file this attempt uploads into: the row the collision decision chose for a
   * "Replace as new version", or the file `initiate-upload` created (or resolved) for
   * every other attempt. A retry keeps it, so the attempt it restarts stays the same
   * file rather than becoming a second one.
   */
  fileId: string | null;
  versionNo: number | null;
};

/** The failure the server (or the transport) reported for one attempt. */
export type TUploadFailure = { code: string | null; message: string | null; cancelled: boolean };

/** Where the files would land, and what the view knows about it. */
export type TUploadTarget = {
  workspaceSlug: string;
  projectId: string;
  /** The folder uploads land in; `null` is the project root. */
  folderId: string | null;
  /** What that folder is called in the overlay, the messages and the modal. */
  folderName: string;
  /** The files this view lists for the destination folder, for the collision check. */
  listedFiles: IProjectFile[];
  /** The ceiling has no room left, per the listing's own storage block. */
  isFull: boolean;
  /** What the quota refusal says; it names the usage and the limit the server reported. */
  quotaMessage: string;
};

/**
 * The first field error in a DRF validation body, e.g.
 * `{"size_bytes": ["Ensure this value is greater than or equal to 1."]}`.
 *
 * A serializer refusal carries no `error`, `detail` or `message` string at all, so
 * without this the row could only report the generic failure for a case the server
 * explained exactly - a 0-byte file, refused by `size_bytes: min_value=1`
 * (DEFECT-003 §3.8). Only list-shaped values are read: a body that also carries
 * `code`/`field` strings must not surface one of those as the reason.
 */
const firstFieldError = (payload: Record<string, unknown>): string | null => {
  for (const value of Object.values(payload)) {
    if (!Array.isArray(value)) continue;
    const message = value.find((entry): entry is string => typeof entry === "string" && entry.length > 0);
    if (message) return message;
  }

  return null;
};

/**
 * The service throws `error.response.data` for an HTTP error and the raw error for a
 * transport failure, and the presigned PUT sender rejects its own shapes, so the code, the
 * message and the cancellation are read from whichever of them arrived.
 */
export const readUploadFailure = (error: unknown): TUploadFailure => {
  if (!error || typeof error !== "object" || error instanceof Error) {
    return { code: null, message: error instanceof Error ? error.message : null, cancelled: false };
  }

  const payload = error as Record<string, unknown>;
  const reported = [payload.error, payload.detail, payload.message].find(
    (value): value is string => typeof value === "string" && value.length > 0
  );

  return {
    code: typeof payload.code === "string" ? payload.code : null,
    message: reported ?? firstFieldError(payload),
    cancelled: payload.cancelled === true,
  };
};

export const uploadFailedCopy = (failure: TUploadFailure): string =>
  failure.message ? `Upload failed — ${failure.message}` : "Upload failed.";

/**
 * The name the server derives for a duplicate in the same folder, mirrored from
 * `available_display_name`: `Report.pdf` becomes `Report (2).pdf` (R-FOLD-5). Only the
 * modal's label needs it before the fact — the server's `name_display` is what the row
 * shows once it has answered.
 */
export const nextAvailableName = (fileName: string, taken: Set<string>): string => {
  const extensionStart = fileName.lastIndexOf(".");
  const hasExtension = extensionStart > 0 && extensionStart < fileName.length - 1;
  const stem = hasExtension ? fileName.slice(0, extensionStart) : fileName;
  const extension = hasExtension ? fileName.slice(extensionStart) : "";

  for (let index = 2; index < 1000; index += 1) {
    const candidate = `${stem} (${index})${extension}`;
    if (!taken.has(candidate.toLowerCase())) return candidate;
  }

  return fileName;
};

/**
 * The names the destination folder's namespace already holds, as the queue sees it: every
 * file the view lists for that folder, plus every queued row's name.
 *
 * A row's `name` is the name it owns there - the file's own name until the server answers,
 * and the server's derived name from then on - so a row that has just been resolved as
 * "Keep both" contributes the suffixed name it was promised rather than the original one.
 * That is what makes the *second* modal in a batch of same-named files predict the name the
 * server will actually derive (DEFECT-003 §3.4): with only the original names in the set,
 * two same-named picks predicted `collide (3).txt` twice while the server stored
 * `collide (3).txt` and then `collide (4).txt`.
 */
export const collisionNamespace = (
  listedFiles: IProjectFile[],
  rows: TUploadRow[],
  exceptRowId: string
): Set<string> => {
  const taken = new Set(listedFiles.map((file) => file.name_display.toLowerCase()));
  rows.forEach((row) => {
    if (row.id !== exceptRowId) taken.add(row.name.toLowerCase());
  });

  return taken;
};

/**
 * The name the keep-both choice promises for one colliding row, derived from the same
 * namespace `available_display_name` will derive it from.
 */
export const keepBothNameFor = (row: TUploadRow, listedFiles: IProjectFile[], rows: TUploadRow[]): string =>
  nextAvailableName(row.name, collisionNamespace(listedFiles, rows, row.id));

/** The statuses that still own a place in the destination folder's name space. */
const occupiesName = (status: TUploadRowStatus): boolean =>
  status === "queued" || status === "uploading" || status === "finalizing" || status === "collision";

/**
 * Decide what each dropped or picked file becomes, before any request is made: a
 * pre-validation refusal, a quota refusal, a collision waiting for the user, or an
 * upload. One file is one row and one attempt, so a file that is refused here never
 * starts and never disturbs the others (R-UPL-2, R-UPL-7).
 */
export const planUploadRows = (
  files: File[],
  target: TUploadTarget,
  liveRows: TUploadRow[],
  nextId: () => string
): { rows: TUploadRow[]; starting: string[] } => {
  // Collisions are judged against the rows this view lists for the destination folder.
  // The listing is a page, so a collider beyond it is not detected here — and then the
  // server suffixes the name, which is exactly what "Keep both" produces. Nothing is
  // ever overwritten either way.
  const taken = new Set(
    target.listedFiles
      .filter((file) => (file.folder_id ?? null) === target.folderId && !file.trashed)
      .map((file) => file.name_display.toLowerCase())
  );
  liveRows.forEach((row) => {
    if (occupiesName(row.status)) taken.add(row.name.toLowerCase());
  });

  const starting: string[] = [];
  const rows = files.map((file) => {
    const validation = validateUploadCandidate({
      name: file.name,
      sizeBytes: file.size,
      declaredType: file.type,
    });
    // One freshly built row per file: the branches below only settle the status and the
    // message it starts in, so nothing is copied to decide them.
    const row: TUploadRow = {
      id: nextId(),
      file,
      name: file.name,
      sizeBytes: file.size,
      mimeType: validation.mimeType,
      status: "queued",
      progress: 0,
      message: UPLOAD_QUEUED_COPY,
      fileId: null,
      versionNo: null,
    };

    if (!validation.ok) {
      // Pre-validation, in the server's order: size first, then the type (R-UPL-7).
      row.status = "rejected";
      row.message =
        validation.reason === "size"
          ? uploadTooLargeCopy(FILES_UPLOAD_MAX_BYTES)
          : uploadTypeNotAllowedCopy(file.name, validation.mimeType);
      return row;
    }
    if (target.isFull) {
      row.status = "quota";
      row.message = target.quotaMessage;
      return row;
    }
    if (taken.has(file.name.toLowerCase())) {
      row.status = "collision";
      row.message = `${file.name} already exists in ${target.folderName}. Choose how to resolve it.`;
      return row;
    }

    taken.add(file.name.toLowerCase());
    starting.push(row.id);
    return row;
  });

  return { rows, starting };
};

/** What an attempt needs from the queue that owns it. */
export type TUploadAttemptIo = {
  target: TUploadTarget;
  /** Settle one row in place. */
  patch: (rowId: string, patch: Partial<TUploadRow>) => void;
  /** Take a settled row, and the file it uploaded, out of the queue. */
  remove: (rowId: string) => void;
  /** The file is stored, so the listing may refetch it. */
  onStored: () => void;
  /** Give an attempt up so its reservation is released (ARCH-001 §2.4). */
  release: (fileId: string | null, versionNo: number | null) => Promise<void>;
  isCancelled: (rowId: string) => boolean;
  setHandle: (rowId: string, handle: TPresignedUploadHandle | null) => void;
};

/**
 * Run one file all the way: reserve and sign, PUT the bytes straight to the store with
 * progress, then have the server verify and store them. Every branch settles the row, so
 * a failure is one row's failure and a retry has a defined state to start from (R-UPL-1,
 * R-UPL-2, R-UPL-3).
 */
export const runUploadAttempt = async (row: TUploadRow, io: TUploadAttemptIo): Promise<void> => {
  const { target } = io;
  const rowId = row.id;

  if (io.isCancelled(rowId)) {
    io.patch(rowId, { status: "cancelled", message: UPLOAD_CANCELLED_COPY });
    return;
  }
  if (target.isFull) {
    io.patch(rowId, { status: "quota", progress: 0, message: target.quotaMessage });
    return;
  }

  io.patch(rowId, { status: "uploading", progress: 0, message: "" });

  let initiation: IProjectFileUploadInitiation;
  try {
    initiation = await fileService.initiateFileUpload(target.workspaceSlug, target.projectId, {
      file_name: row.file.name,
      size_bytes: row.file.size,
      mime_type: row.mimeType,
      folder_id: target.folderId,
      file_id: row.fileId,
    });
  } catch (error) {
    const failure = readUploadFailure(error);
    if (failure.code === "quota_exceeded") {
      io.patch(rowId, { status: "quota", message: target.quotaMessage });
      return;
    }
    io.patch(rowId, { status: "failed", message: uploadFailedCopy(failure) });
    return;
  }

  // The stored name is the server's: a duplicate being kept alongside an existing file
  // comes back suffixed (R-FOLD-5), and the row shows that name from here on.
  io.patch(rowId, {
    name: initiation.file.name_display,
    fileId: initiation.file.id,
    versionNo: initiation.version_no,
  });

  // Cancelled while the presign was in flight: the attempt exists now, so it is given up
  // here rather than left holding a reservation.
  if (io.isCancelled(rowId)) {
    await io.release(initiation.file.id, initiation.version_no);
    io.patch(rowId, { status: "cancelled", message: UPLOAD_CANCELLED_COPY });
    return;
  }

  const handle = putPresignedFile({
    url: initiation.upload.url,
    file: row.file,
    headers: initiation.upload.headers,
    onProgress: (percentage) => io.patch(rowId, { progress: percentage }),
  });
  io.setHandle(rowId, handle);

  try {
    await handle.promise;
  } catch (error) {
    io.setHandle(rowId, null);
    if (io.isCancelled(rowId) || readUploadFailure(error).cancelled) {
      await io.release(initiation.file.id, initiation.version_no);
      io.patch(rowId, { status: "cancelled", message: UPLOAD_CANCELLED_COPY });
      return;
    }
    io.patch(rowId, { status: "failed", message: uploadFailedCopy(readUploadFailure(error)) });
    return;
  }
  io.setHandle(rowId, null);

  if (io.isCancelled(rowId)) {
    await io.release(initiation.file.id, initiation.version_no);
    io.patch(rowId, { status: "cancelled", message: UPLOAD_CANCELLED_COPY });
    return;
  }

  io.patch(rowId, { status: "finalizing", progress: 100 });

  try {
    const completion = await fileService.completeFileUpload(
      target.workspaceSlug,
      target.projectId,
      initiation.file.id,
      {
        version_no: initiation.version_no,
        size_bytes: row.file.size,
      }
    );
    const storedVersion = completion.version.version_no;

    if (io.isCancelled(rowId)) {
      // The bytes reached the store before the cancel did; the version is real, so the row
      // reports it rather than claiming the upload was stopped.
      io.patch(rowId, {
        status: "saved",
        message: `Saved as version ${storedVersion} — the upload completed before the cancel took effect.`,
      });
    } else if (completion.activation_required) {
      // A revision is stored without becoming active: the user confirms that separately,
      // and this surface never implies the switch happened (AD-18, AC-43).
      io.patch(rowId, {
        status: "saved",
        message: `Version ${storedVersion} saved — it is not the active version yet. Open the file to make it active.`,
      });
    } else {
      // Succeeded: the row leaves the queue and the listing shows the real file.
      io.remove(rowId);
    }
    io.onStored();
  } catch (error) {
    const failure = readUploadFailure(error);
    if (failure.code === "quota_exceeded") {
      io.patch(rowId, { status: "quota", message: target.quotaMessage });
      return;
    }
    if (failure.code !== null && UNVERIFIED_CODES[failure.code] === true) {
      io.patch(rowId, { status: "unverified", message: UPLOAD_UNVERIFIED_COPY });
      return;
    }
    io.patch(rowId, { status: "failed", message: uploadFailedCopy(failure) });
  }
};
