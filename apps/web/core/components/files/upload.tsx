/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import type { DragEvent as ReactDragEvent, RefObject } from "react";
import { useCallback, useMemo, useRef, useState } from "react";
// headlessui
import { Dialog } from "@headlessui/react";
// plane imports
import { Button } from "@plane/propel/button";
import { CloseCircleFilledIcon, InfoIcon } from "@plane/propel/icons";
import { EModalPosition, EModalWidth, ModalCore } from "@plane/ui";
import { cn } from "@plane/utils";
// services
import type { IProjectFile, IProjectFileStorage, TPresignedUploadHandle } from "@/services/project-file.service";
import { ProjectFileService } from "@/services/project-file.service";
// helpers
import {
  FILES_FOCUS_RING,
  FILES_UPLOAD_MAX_BYTES,
  formatFileSize,
  quotaExceededCopy,
  quotaWarningCopy,
  storageUsedPct,
} from "./helpers";
// the queue's own decisions and attempt runner, kept free of React so each is a plain call
import type { TUploadRow, TUploadTarget } from "./upload-queue";
import {
  UPLOAD_CANCELLED_COPY,
  UPLOAD_QUEUED_COPY,
  UPLOAD_VERIFYING_COPY,
  nextAvailableName,
  planUploadRows,
  runUploadAttempt,
} from "./upload-queue";

const fileService = new ProjectFileService();

type TFilesUploadOptions = {
  workspaceSlug: string;
  projectId: string;
  /** The folder uploads land in; `null` is the project root. */
  folderId: string | null;
  /** What that folder is called in the drop overlay and in the row messages. */
  folderName: string;
  /** The files the view currently lists, for the client-side collision check. */
  listedFiles: IProjectFile[];
  /** The listing's storage block; the quota pre-check and the banners read it. */
  storage: IProjectFileStorage | undefined;
  /** False for a GUEST: the tab then offers no upload affordance at all. */
  canUpload: boolean;
  /** Called once a file is stored so the listing can refetch it. */
  onStored: () => void;
};

/** Everything the tab's upload surface reads and calls. */
export type TFilesUpload = {
  rows: TUploadRow[];
  /** The row waiting for a collision decision; only one modal is open at a time. */
  collision: { row: TUploadRow; keepBothName: string } | null;
  /** The destination, as the drop overlay names it. */
  folderName: string;
  isDraggingOver: boolean;
  /** False for a GUEST: nothing of this surface renders. */
  canUpload: boolean;
  isFull: boolean;
  usedPct: number;
  /** The listing's warning threshold; 0 means none is configured. */
  warningPct: number;
  /** The ceiling the listing reported, or null until it answers. */
  quota: { exceeded: string; warning: string } | null;
  inputRef: RefObject<HTMLInputElement | null>;
  openPicker: () => void;
  addFiles: (files: ArrayLike<File> | null | undefined) => void;
  cancel: (rowId: string) => void;
  retry: (rowId: string) => void;
  dismiss: (rowId: string) => void;
  chooseCollision: (choice: "keep" | "replace" | "cancel") => void;
  dropHandlers: {
    onDragEnter: (event: ReactDragEvent<HTMLElement>) => void;
    onDragOver: (event: ReactDragEvent<HTMLElement>) => void;
    onDragLeave: (event: ReactDragEvent<HTMLElement>) => void;
    onDrop: (event: ReactDragEvent<HTMLElement>) => void;
  };
};

/**
 * The upload queue behind the tab's drop target and picker.
 *
 * One file is one row and one initiate → PUT → complete cycle, so a failure leaves the
 * other files alone (R-UPL-2). The client pre-checks size and type against the same rules
 * the server enforces (R-UPL-7) and refuses a batch the quota has no room for before
 * anything is signed; the server stays authoritative for both.
 */
export function useFilesUpload(options: TFilesUploadOptions): TFilesUpload {
  const { workspaceSlug, projectId, folderId, folderName, listedFiles, storage, canUpload, onStored } = options;

  const [rows, setRows] = useState<TUploadRow[]>([]);
  const [isDraggingOver, setIsDraggingOver] = useState(false);
  const inputRef = useRef<HTMLInputElement | null>(null);
  // The queue is advanced from async continuations (progress, verification, cancellation),
  // so the rows are mirrored here: a continuation then decides from the queue as it is now
  // rather than from the array its closure was built with.
  const rowsRef = useRef<TUploadRow[]>([]);
  const handlesRef = useRef(new Map<string, TPresignedUploadHandle>());
  const cancelledRef = useRef(new Set<string>());
  const dragDepthRef = useRef(0);
  const rowSeqRef = useRef(0);

  const applyRows = useCallback((update: (current: TUploadRow[]) => TUploadRow[]) => {
    rowsRef.current = update(rowsRef.current);
    setRows(rowsRef.current);
  }, []);

  /**
   * The row is settled in place and the queue is republished as a new array. Rows are only
   * ever written through this function, and the list re-renders on the array's identity, so
   * an in-place patch costs one array copy instead of one row copy per update.
   */
  const patchRow = useCallback((rowId: string, patch: Partial<TUploadRow>) => {
    const row = rowsRef.current.find((candidate) => candidate.id === rowId);
    if (!row) return;
    Object.assign(row, patch);
    setRows([...rowsRef.current]);
  }, []);

  // The ceiling the listing reported. With no ceiling — or no listing answer yet — nothing
  // is refused here; the server refuses what it cannot fit, with the same message.
  const isFull = Boolean(storage && storage.limit_bytes > 0 && storage.project_used_bytes >= storage.limit_bytes);
  const usedPct = storage ? storageUsedPct(storage) : 0;
  const quota = useMemo(
    () =>
      storage
        ? {
            exceeded: quotaExceededCopy(storage.project_used_bytes, storage.limit_bytes),
            warning: quotaWarningCopy(usedPct),
          }
        : null,
    [storage, usedPct]
  );

  const target: TUploadTarget = useMemo(
    () => ({
      workspaceSlug,
      projectId,
      folderId,
      folderName,
      listedFiles,
      isFull,
      quotaMessage: quota?.exceeded ?? quotaExceededCopy(0, 0),
    }),
    [folderId, folderName, isFull, listedFiles, projectId, quota, workspaceSlug]
  );

  /**
   * Give an attempt up so the next presign for the file is not refused as
   * `upload_in_progress` (ARCH-001 §2.4). Best effort: a version the server already settled
   * refuses the abort, and the server's single-fire release guard makes a repeated abort
   * harmless — either way the attempt is over and nothing is left behind.
   */
  const releaseAttempt = useCallback(
    async (fileId: string | null, versionNo: number | null) => {
      if (!fileId || versionNo === null) return;
      await fileService.abortFileUpload(workspaceSlug, projectId, fileId, versionNo).catch(() => undefined);
    },
    [projectId, workspaceSlug]
  );

  const startRow = useCallback(
    (rowId: string) => {
      const row = rowsRef.current.find((candidate) => candidate.id === rowId);
      if (!row || !canUpload) return;

      // The attempt reads the row's file, type and replace target, none of which it patches.
      void runUploadAttempt(row, {
        target,
        patch: patchRow,
        remove: (id) => {
          cancelledRef.current.delete(id);
          applyRows((current) => current.filter((candidate) => candidate.id !== id));
        },
        onStored,
        release: releaseAttempt,
        isCancelled: (id) => cancelledRef.current.has(id),
        setHandle: (id, handle) => {
          if (handle) handlesRef.current.set(id, handle);
          else handlesRef.current.delete(id);
        },
      });
    },
    [applyRows, canUpload, onStored, patchRow, releaseAttempt, target]
  );

  const addFiles = useCallback(
    (incoming: ArrayLike<File> | null | undefined) => {
      if (!canUpload || !incoming || incoming.length === 0) return;

      const { rows: planned, starting } = planUploadRows(Array.from(incoming), target, rowsRef.current, () => {
        rowSeqRef.current += 1;
        return `u${rowSeqRef.current}`;
      });

      applyRows((current) => [...current, ...planned]);
      starting.forEach((rowId) => startRow(rowId));
    },
    [applyRows, canUpload, startRow, target]
  );

  const openPicker = useCallback(() => {
    if (!canUpload || isFull) return;
    inputRef.current?.click();
  }, [canUpload, isFull]);

  const cancel = useCallback(
    (rowId: string) => {
      const row = rowsRef.current.find((candidate) => candidate.id === rowId);
      if (!row) return;

      cancelledRef.current.add(rowId);
      patchRow(rowId, { status: "cancelled", message: UPLOAD_CANCELLED_COPY });

      const handle = handlesRef.current.get(rowId);
      if (handle) {
        // The PUT's own rejection path settles the row and gives the attempt up, so the
        // abort and the row's state cannot disagree about which happened first.
        handlesRef.current.delete(rowId);
        handle.abort();
        return;
      }

      void releaseAttempt(row.fileId, row.versionNo);
    },
    [patchRow, releaseAttempt]
  );

  const retry = useCallback(
    (rowId: string) => {
      const row = rowsRef.current.find((candidate) => candidate.id === rowId);
      if (!row || !canUpload) return;

      const previous = { fileId: row.fileId, versionNo: row.versionNo };
      cancelledRef.current.delete(rowId);
      handlesRef.current.delete(rowId);
      // The file the attempt belongs to is kept: a retry has to end as the same file the
      // failed attempt created. Asking for a new one instead would leave the first file
      // behind as an unverified row and store the retry under a suffixed name — the retry
      // is documented as resuming the interrupted upload, "no duplicate file row"
      // (EXPERIENCE, upload interrupted). Only the version is dropped: the fresh presign
      // allocates the attempt's own version, and `runUploadAttempt` fills it back in from
      // the response.
      patchRow(rowId, { status: "queued", progress: 0, message: UPLOAD_QUEUED_COPY, versionNo: null });

      void (async () => {
        // The server refuses a presign for this file while the abandoned attempt still
        // holds its reservation, so that attempt is given up first (ARCH-001 §2.4).
        await releaseAttempt(previous.fileId, previous.versionNo);
        startRow(rowId);
      })();
    },
    [canUpload, patchRow, releaseAttempt, startRow]
  );

  const dismiss = useCallback(
    (rowId: string) => {
      handlesRef.current.delete(rowId);
      cancelledRef.current.delete(rowId);
      applyRows((current) => current.filter((row) => row.id !== rowId));
    },
    [applyRows]
  );

  const collisionRow = rows.find((row) => row.status === "collision") ?? null;
  const collision = useMemo(() => {
    if (!collisionRow) return null;

    const taken = new Set(listedFiles.map((file) => file.name_display.toLowerCase()));
    rows.forEach((row) => {
      if (row.id !== collisionRow.id) taken.add(row.name.toLowerCase());
    });

    return { row: collisionRow, keepBothName: nextAvailableName(collisionRow.name, taken) };
  }, [collisionRow, listedFiles, rows]);

  const chooseCollision = useCallback(
    (choice: "keep" | "replace" | "cancel") => {
      const row = rowsRef.current.find((candidate) => candidate.status === "collision");
      if (!row) return;

      if (choice === "cancel") {
        applyRows((current) => current.filter((candidate) => candidate.id !== row.id));
        return;
      }

      if (choice === "replace") {
        const replaced = listedFiles.find(
          (file) =>
            (file.folder_id ?? null) === folderId &&
            !file.trashed &&
            file.name_display.toLowerCase() === row.name.toLowerCase()
        );
        // The file the collision was judged against can be gone (another member removed it,
        // or the view refetched): the upload then continues as a new file instead of
        // revising a row that no longer exists.
        patchRow(row.id, {
          status: "queued",
          message: UPLOAD_QUEUED_COPY,
          fileId: replaced ? replaced.id : null,
        });
        startRow(row.id);
        return;
      }

      patchRow(row.id, { status: "queued", message: UPLOAD_QUEUED_COPY });
      startRow(row.id);
    },
    [applyRows, folderId, listedFiles, patchRow, startRow]
  );

  /**
   * The drop target. Every one of these is a progressive enhancement over the picker: a
   * drag adds nothing a keyboard cannot reach through Upload.
   */
  const dropHandlers = useMemo(
    () => ({
      onDragEnter: (event: ReactDragEvent<HTMLElement>) => {
        if (!canUpload) return;
        event.preventDefault();
        dragDepthRef.current += 1;
        setIsDraggingOver(true);
      },
      onDragOver: (event: ReactDragEvent<HTMLElement>) => {
        // Required, or the browser refuses the drop and shows a "no drop" cursor.
        if (!canUpload) return;
        event.preventDefault();
        if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
      },
      onDragLeave: () => {
        if (!canUpload) return;
        dragDepthRef.current = Math.max(0, dragDepthRef.current - 1);
        if (dragDepthRef.current === 0) setIsDraggingOver(false);
      },
      onDrop: (event: ReactDragEvent<HTMLElement>) => {
        if (!canUpload) return;
        event.preventDefault();
        dragDepthRef.current = 0;
        setIsDraggingOver(false);
        const dropped = event.dataTransfer?.files;
        if (dropped && dropped.length > 0) addFiles(dropped);
      },
    }),
    [addFiles, canUpload]
  );

  return {
    rows,
    collision,
    folderName,
    isDraggingOver: isDraggingOver && canUpload,
    canUpload,
    isFull,
    usedPct,
    warningPct: storage ? storage.warn_threshold_pct : 0,
    quota,
    inputRef,
    openPicker,
    addFiles,
    cancel,
    retry,
    dismiss,
    chooseCollision,
    dropHandlers,
  };
}

/** The quota notices, driven by the same storage block the header chip shows. */
export function FilesUploadQuotaBanners(props: { upload: TFilesUpload }) {
  const { upload } = props;
  const { quota, isFull, usedPct, warningPct } = upload;

  if (!quota) return null;

  if (isFull) {
    return (
      <div
        data-testid="files-upload-quota-exceeded"
        role="alert"
        className="flex items-center gap-2 border-b border-danger-strong bg-danger-subtle px-4 py-2"
      >
        <CloseCircleFilledIcon className="size-3.5 shrink-0 text-danger-primary" aria-hidden="true" />
        <p className="text-caption-md-regular text-danger-primary">{quota.exceeded}</p>
      </div>
    );
  }

  // `warn_threshold_pct` of 0 means no threshold is configured, not "warn always".
  if (warningPct <= 0 || usedPct < warningPct) return null;

  return (
    <div
      data-testid="files-upload-quota-warning"
      role="status"
      className="flex items-center gap-2 border-b border-warning-strong bg-warning-subtle px-4 py-2"
    >
      <InfoIcon className="size-3.5 shrink-0 text-warning-primary" aria-hidden="true" />
      <p className="text-caption-md-regular text-warning-primary">{quota.warning}</p>
    </div>
  );
}

/**
 * What the destination folder is called: the breadcrumb the listing returns ends at the
 * browsed folder. A folder whose name the response cannot answer for — a listing that
 * failed for the filter on screen — is named as such rather than being called the root.
 */
export const filesUploadTargetName = (breadcrumbs: { name: string }[], folderId: string | null): string =>
  breadcrumbs.at(-1)?.name ?? (folderId ? "the current folder" : "Project root");

/** The full-tab overlay a drag shows, naming where the files would land (DESIGN §3). */
export function FilesDropOverlay(props: { folderName: string }) {
  const { folderName } = props;

  return (
    <div
      data-testid="files-upload-drop-overlay"
      aria-hidden="true"
      className="pointer-events-none fixed inset-0 z-20 flex items-center justify-center bg-accent-subtle/40 p-6"
    >
      <div className="rounded-lg border-2 border-dashed border-accent-strong bg-surface-1 px-6 py-5 text-center shadow-raised-200">
        <p className="text-body-md-medium text-primary">Drop to upload to {folderName}</p>
        <p className="mt-1 text-caption-md-regular text-tertiary">
          Checked here first for the {formatFileSize(FILES_UPLOAD_MAX_BYTES)} per-file limit and the allowed types.
        </p>
      </div>
    </div>
  );
}

/** One queue row: the name, the progress, the message and the action it offers. */
function FilesUploadRow(props: { upload: TFilesUpload; row: TUploadRow }) {
  const { upload, row } = props;
  const isUploading = row.status === "uploading";
  const isRunning = isUploading || row.status === "finalizing" || row.status === "queued";
  const isFailure = row.status === "failed" || row.status === "rejected" || row.status === "quota";
  const canRetry = row.status === "failed" || row.status === "quota" || row.status === "cancelled";
  const canDismiss = canRetry || (!isRunning && row.status !== "collision");

  const message = isUploading
    ? `Uploading to ${upload.folderName}…`
    : row.status === "finalizing"
      ? UPLOAD_VERIFYING_COPY
      : row.message;

  return (
    <li
      data-testid={`files-upload-row-${row.id}`}
      data-upload-name={row.name}
      data-upload-status={row.status}
      data-upload-progress={String(row.progress)}
      role={isFailure ? "alert" : "status"}
      aria-live={isFailure ? undefined : "polite"}
      className={cn(
        "flex items-center gap-3 border-b border-subtle px-4 py-2",
        isFailure ? "bg-danger-subtle/40" : "bg-surface-1"
      )}
    >
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline gap-2">
          <span className="truncate text-body-xs-regular text-primary">{row.name}</span>
          <span className="shrink-0 text-caption-md-regular text-tertiary">{formatFileSize(row.sizeBytes)}</span>
        </div>

        {isRunning && row.status !== "queued" && (
          <div
            data-testid={`files-upload-progress-${row.id}`}
            role="progressbar"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={row.progress}
            aria-label={`Upload progress for ${row.name}`}
            className="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-layer-2"
          >
            <div className="h-full rounded-full bg-accent-primary" style={{ width: `${row.progress}%` }} />
          </div>
        )}

        <p
          data-testid={`files-upload-message-${row.id}`}
          className={cn("mt-0.5 text-caption-md-regular", isFailure ? "text-danger-primary" : "text-tertiary")}
        >
          {message}
          {/* The percentage is decoration: the live region announces the status, not every
              step of the bar. */}
          {isUploading && <span aria-hidden="true">{` ${row.progress}%`}</span>}
        </p>
      </div>

      <div className="flex shrink-0 items-center gap-2">
        {isRunning && (
          <Button
            data-testid={`files-upload-cancel-${row.id}`}
            variant="secondary"
            size="base"
            className={FILES_FOCUS_RING}
            onClick={() => upload.cancel(row.id)}
          >
            Cancel
          </Button>
        )}
        {canRetry && (
          <Button
            data-testid={`files-upload-retry-${row.id}`}
            variant="secondary"
            size="base"
            className={FILES_FOCUS_RING}
            onClick={() => upload.retry(row.id)}
          >
            Retry
          </Button>
        )}
        {canDismiss && (
          <Button
            data-testid={`files-upload-dismiss-${row.id}`}
            variant="ghost"
            size="base"
            className={FILES_FOCUS_RING}
            aria-label={`Dismiss ${row.name}`}
            onClick={() => upload.dismiss(row.id)}
          >
            Dismiss
          </Button>
        )}
      </div>
    </li>
  );
}

/** The three ways out of a collision, as the modal offers them (DESIGN §8, R-FOLD-5). */
export function FilesCollisionChoices(props: { upload: TFilesUpload; existingName: string; keepBothName: string }) {
  const { upload, existingName, keepBothName } = props;

  const choiceClassName = cn(
    "flex flex-col items-start gap-1 rounded-md border border-strong bg-layer-2 px-3 py-2 text-left hover:bg-layer-2-hover",
    FILES_FOCUS_RING
  );

  return (
    <div className="flex flex-col gap-2">
      <button
        type="button"
        data-testid="files-upload-collision-keep-both"
        className={choiceClassName}
        onClick={() => upload.chooseCollision("keep")}
      >
        <span className="text-body-xs-regular text-primary">Keep both ({keepBothName})</span>
        <span className="text-caption-md-regular text-tertiary">
          Adds a new file next to the existing one. The existing file is untouched.
        </span>
      </button>
      <button
        type="button"
        data-testid="files-upload-collision-replace"
        className={choiceClassName}
        onClick={() => upload.chooseCollision("replace")}
      >
        <span className="text-body-xs-regular text-primary">Replace as new version</span>
        <span className="text-caption-md-regular text-tertiary">
          Adds a new version to {existingName}, which this upload does not make active. The old version stays
          downloadable.
        </span>
      </button>
      <button
        type="button"
        data-testid="files-upload-collision-cancel"
        className={choiceClassName}
        onClick={() => upload.chooseCollision("cancel")}
      >
        <span className="text-body-xs-regular text-primary">Cancel</span>
        <span className="text-caption-md-regular text-tertiary">Stops this upload. No file is created.</span>
      </button>
    </div>
  );
}

/**
 * The name collision, resolved before any file row exists (R-FOLD-5): keep both (the server
 * suffixes the name), replace as a new version of the existing file, or stop.
 */
function FilesCollisionModal(props: { upload: TFilesUpload }) {
  const { upload } = props;
  const collision = upload.collision;

  return (
    <ModalCore
      isOpen={Boolean(collision)}
      position={EModalPosition.CENTER}
      width={EModalWidth.XXL}
      handleClose={() => upload.chooseCollision("cancel")}
    >
      {collision && (
        <div data-testid="files-upload-collision" className="flex flex-col gap-4 p-5">
          <div className="flex flex-col gap-1">
            <Dialog.Title className="text-body-md-medium text-primary">
              &ldquo;{collision.row.name}&rdquo; already exists in {upload.folderName}
            </Dialog.Title>
            <Dialog.Description className="text-caption-md-regular text-tertiary">
              Choose how to resolve the name collision. Nothing is overwritten.
            </Dialog.Description>
          </div>
          <FilesCollisionChoices
            upload={upload}
            existingName={collision.row.name}
            keepBothName={collision.keepBothName}
          />
        </div>
      )}
    </ModalCore>
  );
}

/**
 * The queue in the tab: the hidden picker, the quota notices, the per-file rows, the
 * collision modal and the drag overlay. It renders nothing at all for a GUEST — the
 * read-only view offers no upload affordance, not a disabled one (EXP-001 F-01).
 */
export function FilesUploadSurface(props: { upload: TFilesUpload }) {
  const { upload } = props;

  if (!upload.canUpload) return null;

  return (
    <>
      <FilesUploadQuotaBanners upload={upload} />
      <input
        ref={upload.inputRef}
        data-testid="files-upload-input"
        type="file"
        multiple
        className="sr-only"
        aria-label="Upload files"
        onChange={(event) => {
          upload.addFiles(event.target.files);
          // The same file picked twice in a row still fires `change`.
          event.target.value = "";
        }}
      />
      {upload.rows.length > 0 && (
        <ul data-testid="files-upload-rows" aria-label="Uploads" className="flex flex-col border-y border-subtle">
          {upload.rows.map((row) => (
            <FilesUploadRow key={row.id} upload={upload} row={row} />
          ))}
        </ul>
      )}
      {upload.isDraggingOver && <FilesDropOverlay folderName={upload.folderName} />}
      <FilesCollisionModal upload={upload} />
    </>
  );
}
