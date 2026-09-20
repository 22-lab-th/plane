/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useCallback, useEffect, useRef, useState } from "react";
// headlessui
import { Dialog } from "@headlessui/react";
// plane imports
import { Button } from "@plane/propel/button";
import { EModalPosition, EModalWidth, ModalCore } from "@plane/ui";
import { cn } from "@plane/utils";
// services
import type { IProjectFileBreadcrumb, IProjectFileDetail, IProjectFolder } from "@/services/project-file.service";
import { ProjectFileService, putPresignedFile } from "@/services/project-file.service";
// helpers
import {
  FILES_FOCUS_RING,
  FILES_UPLOAD_MAX_BYTES,
  purgeConfirmCopy,
  restoredToRootCopy,
  uploadTooLargeCopy,
  uploadTypeNotAllowedCopy,
  validateUploadCandidate,
  versionActivatedCopy,
  versionActivationAskCopy,
  versionDeclinedCopy,
  versionSavedCopy,
} from "./helpers";
import { readUploadFailure, uploadFailedCopy } from "./upload-queue";

const fileService = new ProjectFileService();

/** How many files the move picker asks for: it only wants the child folders of a level. */
const MOVE_PICKER_PAGE_SIZE = 1;

/** The confirmation the drawer is showing; at most one at a time (DESIGN §1). */
export type TDrawerDialog =
  | { kind: "rename" }
  | { kind: "move" }
  | { kind: "purge" }
  | { kind: "activation"; versionNo: number };

/** A line the drawer reports a write with; the panel renders it as a live region. */
export type TDrawerNotice = { tone: "info" | "error"; text: string };

/** The dialogs' shared chrome: a title, the two answers, nothing else. */
const DIALOG_BODY_CLASS_NAME = "flex flex-col gap-3 p-5";
const DIALOG_TITLE_CLASS_NAME = "text-body-md-medium text-primary";
const DIALOG_NOTE_CLASS_NAME = "text-caption-md-regular text-tertiary";

type Props = {
  workspaceSlug: string;
  projectId: string;
  detail: IProjectFileDetail;
  /** The project role the purge door requires on top of write access (AC-27). */
  isProjectAdmin: boolean;
  dialog: TDrawerDialog | null;
  onDialogChange: (dialog: TDrawerDialog | null) => void;
  onNotice: (notice: TDrawerNotice | null) => void;
  /** A write changed the file: refetch the detail, the preview URL and the list behind. */
  onChanged: () => void;
  /** The file no longer exists (purged): there is nothing left to show. */
  onPurged: () => void;
};

/**
 * The drawer's mutating surface: Download, a new version, Move, Rename, and - out of
 * the trash - Restore and Purge, each confirmed by the dialog its action needs.
 *
 * Every affordance is drawn from the payload's own `permissions` block rather than
 * from the caller's role guess, so the drawer cannot offer an action the API will
 * refuse: a GUEST reads the file and mutates nothing (EXP-001 F-01), a trashed file
 * is restored rather than edited, and the purge - which the API keeps for project
 * ADMINs - is not offered to anyone else (AC-27).
 *
 * A new version never ends here: when the server says the stored revision is not
 * active, the activation question opens, and the answer is the only thing that moves
 * the active pointer (AC-43).
 */
export function FileDetailActions(props: Props) {
  const { workspaceSlug, projectId, detail, isProjectAdmin, dialog, onDialogChange, onNotice, onChanged, onPurged } =
    props;

  const file = detail.file;
  const permissions = detail.permissions;
  const isTrashed = file.trashed;

  const versionInputRef = useRef<HTMLInputElement | null>(null);
  const [uploading, setUploading] = useState<{ versionNo: number | null; progress: number } | null>(null);

  /** Sign, PUT and verify one revision; the activation question follows a verified one. */
  const uploadVersion = useCallback(
    async (picked: File) => {
      const validation = validateUploadCandidate({
        name: picked.name,
        sizeBytes: picked.size,
        declaredType: picked.type,
      });
      if (!validation.ok) {
        onNotice({
          tone: "error",
          text:
            validation.reason === "size"
              ? uploadTooLargeCopy(FILES_UPLOAD_MAX_BYTES)
              : uploadTypeNotAllowedCopy(picked.name, validation.mimeType),
        });
        return;
      }

      setUploading({ versionNo: null, progress: 0 });
      try {
        const initiation = await fileService.initiateFileUpload(workspaceSlug, projectId, {
          file_name: picked.name,
          size_bytes: picked.size,
          mime_type: validation.mimeType,
          folder_id: file.folder_id,
          // The file id is what makes this a revision rather than a second file.
          file_id: file.id,
        });
        setUploading({ versionNo: initiation.version_no, progress: 0 });

        const handle = putPresignedFile({
          url: initiation.upload.url,
          file: picked,
          headers: initiation.upload.headers,
          onProgress: (progress) =>
            setUploading((current) =>
              current ? { ...current, progress } : { versionNo: initiation.version_no, progress }
            ),
        });
        await handle.promise;

        const completion = await fileService.completeFileUpload(workspaceSlug, projectId, initiation.file.id, {
          version_no: initiation.version_no,
          size_bytes: picked.size,
        });

        onChanged();
        if (completion.activation_required) {
          // Stored, verified, and deliberately not active: the question is the only
          // thing that may move the pointer (AD-18, AC-43).
          onDialogChange({ kind: "activation", versionNo: completion.version.version_no });
        } else {
          onNotice({ tone: "info", text: versionSavedCopy(completion.version.version_no) });
        }
      } catch (error) {
        onNotice({ tone: "error", text: uploadFailedCopy(readUploadFailure(error)) });
      } finally {
        setUploading(null);
      }
    },
    [file.folder_id, file.id, onChanged, onDialogChange, onNotice, projectId, workspaceSlug]
  );

  const restore = useCallback(async () => {
    try {
      const result = await fileService.restoreProjectFile(workspaceSlug, projectId, file.id);
      onChanged();
      onNotice(
        result.restore.folder_fallback
          ? { tone: "info", text: restoredToRootCopy }
          : { tone: "info", text: `Restored ${result.file.name_display}.` }
      );
    } catch (error) {
      const failure = readUploadFailure(error);
      onNotice({ tone: "error", text: failure.message ?? "We could not restore this file." });
    }
  }, [file.id, onChanged, onNotice, projectId, workspaceSlug]);

  const busy = uploading !== null;

  return (
    <>
      <div
        data-testid="files-drawer-actions"
        className="flex flex-wrap items-center gap-2 border-b border-subtle px-4 py-2"
      >
        {permissions.can_download && (
          <DownloadButton workspaceSlug={workspaceSlug} projectId={projectId} fileId={file.id} versionNo={null} />
        )}
        {permissions.can_edit && (
          <>
            <Button
              data-testid="files-drawer-upload-version"
              variant="secondary"
              size="base"
              className={FILES_FOCUS_RING}
              disabled={busy}
              onClick={() => versionInputRef.current?.click()}
            >
              Upload new version
            </Button>
            <input
              ref={versionInputRef}
              data-testid="files-drawer-version-input"
              type="file"
              className="sr-only"
              aria-label="Upload a new version of this file"
              onChange={(event) => {
                const picked = event.target.files?.[0];
                // The same file picked twice in a row still fires `change`.
                event.target.value = "";
                if (picked) void uploadVersion(picked);
              }}
            />
            <Button
              data-testid="files-drawer-move"
              variant="secondary"
              size="base"
              className={FILES_FOCUS_RING}
              disabled={busy}
              onClick={() => onDialogChange({ kind: "move" })}
            >
              Move…
            </Button>
            <Button
              data-testid="files-drawer-rename"
              variant="secondary"
              size="base"
              className={FILES_FOCUS_RING}
              disabled={busy}
              onClick={() => onDialogChange({ kind: "rename" })}
            >
              Rename
            </Button>
          </>
        )}
        {isTrashed && permissions.can_delete && (
          <Button
            data-testid="files-drawer-restore"
            variant="secondary"
            size="base"
            className={FILES_FOCUS_RING}
            onClick={() => void restore()}
          >
            Restore
          </Button>
        )}
        {isTrashed && isProjectAdmin && (
          <Button
            data-testid="files-drawer-purge"
            variant="error-outline"
            size="base"
            className={FILES_FOCUS_RING}
            onClick={() => onDialogChange({ kind: "purge" })}
          >
            Delete permanently
          </Button>
        )}
        {uploading && (
          <span data-testid="files-drawer-upload-progress" role="status" className={DIALOG_NOTE_CLASS_NAME}>
            {uploading.versionNo === null
              ? "Preparing the new version…"
              : `Uploading v${uploading.versionNo} — ${uploading.progress}%`}
          </span>
        )}
      </div>

      <RenameDialog
        detail={detail}
        isOpen={dialog?.kind === "rename"}
        workspaceSlug={workspaceSlug}
        projectId={projectId}
        onClose={() => onDialogChange(null)}
        onChanged={onChanged}
      />
      <MoveDialog
        detail={detail}
        isOpen={dialog?.kind === "move"}
        workspaceSlug={workspaceSlug}
        projectId={projectId}
        onClose={() => onDialogChange(null)}
        onChanged={onChanged}
        onNotice={onNotice}
      />
      <PurgeDialog
        detail={detail}
        isOpen={dialog?.kind === "purge"}
        workspaceSlug={workspaceSlug}
        projectId={projectId}
        onClose={() => onDialogChange(null)}
        onPurged={onPurged}
      />
      <ActivationDialog
        detail={detail}
        versionNo={dialog?.kind === "activation" ? dialog.versionNo : null}
        workspaceSlug={workspaceSlug}
        projectId={projectId}
        onClose={() => onDialogChange(null)}
        onChanged={onChanged}
        onNotice={onNotice}
      />
    </>
  );
}

/** Sign a URL for one version and hand the file to the browser (AC-07, R-DL-1). */
export function DownloadButton(props: {
  workspaceSlug: string;
  projectId: string;
  fileId: string;
  versionNo: number | null;
}) {
  const { workspaceSlug, projectId, fileId, versionNo } = props;

  const download = async () => {
    const signed = await fileService.getProjectFileDownloadUrl(workspaceSlug, projectId, fileId, { versionNo });
    // The signed response carries `attachment`, so the page is not navigated away from.
    const anchor = document.createElement("a");
    anchor.href = signed.url;
    anchor.rel = "noopener";
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
  };

  return (
    <Button
      data-testid={versionNo === null ? "files-drawer-download" : `files-drawer-version-download-${versionNo}`}
      variant="secondary"
      size="base"
      className={FILES_FOCUS_RING}
      onClick={() => void download()}
    >
      Download
    </Button>
  );
}

/** Rename is `name_display` only: no copy, no delete, no new key (AC-06). */
function RenameDialog(props: {
  workspaceSlug: string;
  projectId: string;
  detail: IProjectFileDetail;
  isOpen: boolean;
  onClose: () => void;
  onChanged: () => void;
}) {
  const { workspaceSlug, projectId, detail, isOpen, onClose, onChanged } = props;
  const [name, setName] = useState(detail.file.name_display);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!isOpen) return;
    setName(detail.file.name_display);
    setError(null);
  }, [detail.file.name_display, isOpen]);

  const submit = async () => {
    const trimmed = name.trim();
    if (!trimmed || trimmed === detail.file.name_display) {
      onClose();
      return;
    }

    setSaving(true);
    setError(null);
    try {
      await fileService.updateProjectFile(workspaceSlug, projectId, detail.file.id, { name_display: trimmed });
      onChanged();
      onClose();
    } catch (failure) {
      setError(readUploadFailure(failure).message ?? "We could not rename this file.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <ModalCore isOpen={isOpen} position={EModalPosition.CENTER} width={EModalWidth.XXL} handleClose={onClose}>
      <div data-testid="files-drawer-rename-modal" className={DIALOG_BODY_CLASS_NAME}>
        <Dialog.Title className={DIALOG_TITLE_CLASS_NAME}>Rename</Dialog.Title>
        <Dialog.Description className={DIALOG_NOTE_CLASS_NAME}>{detail.file.name_display}</Dialog.Description>
        <label className={DIALOG_NOTE_CLASS_NAME} htmlFor="files-drawer-rename-input">
          Display name
        </label>
        <input
          id="files-drawer-rename-input"
          data-testid="files-drawer-rename-input"
          className={cn(
            "rounded-md border border-strong bg-layer-2 px-2 py-1 text-body-xs-regular text-primary",
            FILES_FOCUS_RING
          )}
          value={name}
          onChange={(event) => setName(event.target.value)}
        />
        <p className={DIALOG_NOTE_CLASS_NAME}>
          The stored object keeps its key and its versions; only the displayed name changes.
        </p>
        {error && (
          <p
            data-testid="files-drawer-rename-error"
            role="alert"
            className="text-caption-md-regular text-danger-primary"
          >
            {error}
          </p>
        )}
        <div className="flex items-center justify-end gap-2">
          <Button
            data-testid="files-drawer-rename-cancel"
            variant="secondary"
            size="base"
            className={FILES_FOCUS_RING}
            onClick={onClose}
          >
            Cancel
          </Button>
          <Button
            data-testid="files-drawer-rename-confirm"
            variant="primary"
            size="base"
            className={FILES_FOCUS_RING}
            disabled={saving}
            onClick={() => void submit()}
          >
            Rename
          </Button>
        </div>
      </div>
    </ModalCore>
  );
}

/**
 * The folder picker, walked lazily through the listing endpoint.
 *
 * The API has no route that returns a project's whole folder tree: the listing answers
 * with the child folders of the folder it was asked about, so the picker asks for one
 * level at a time - exactly the requests the tab behind the drawer makes when a user
 * walks the same folders, and never a tree the user did not open. Choosing and opening
 * are two separate controls, because the file's own folder is a legitimate place to
 * *look* in (it holds the subfolders the file could move into) and not a destination.
 *
 * Destination root is the project root, which is where a file goes when its folder no
 * longer matters (R-DEL-2 restores there too). Cross-project move is T-122 and is not
 * offered here: the destination is always a folder of this project.
 */
function MoveDialog(props: {
  workspaceSlug: string;
  projectId: string;
  detail: IProjectFileDetail;
  isOpen: boolean;
  onClose: () => void;
  onChanged: () => void;
  onNotice: (notice: TDrawerNotice | null) => void;
}) {
  const { workspaceSlug, projectId, detail, isOpen, onClose, onChanged, onNotice } = props;
  const file = detail.file;
  const currentFolderId = file.folder_id ?? null;

  const [browsedFolderId, setBrowsedFolderId] = useState<string | null>(null);
  const [folders, setFolders] = useState<IProjectFolder[]>([]);
  const [breadcrumbs, setBreadcrumbs] = useState<IProjectFileBreadcrumb[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [loadFailed, setLoadFailed] = useState(false);
  const [reloadToken, setReloadToken] = useState(0);
  const [target, setTarget] = useState<{ folderId: string | null; name: string } | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!isOpen) return;
    setBrowsedFolderId(null);
    setFolders([]);
    setBreadcrumbs([]);
    setTarget(null);
    setError(null);
  }, [isOpen]);

  useEffect(() => {
    if (!isOpen) return;

    let cancelled = false;
    setIsLoading(true);
    setLoadFailed(false);

    fileService
      .listProjectFiles(workspaceSlug, projectId, {
        folder_id: browsedFolderId ?? undefined,
        page_size: MOVE_PICKER_PAGE_SIZE,
      })
      .then((response) => {
        if (cancelled) return response;
        setFolders(response.folders);
        setBreadcrumbs(response.breadcrumbs);
        return response;
      })
      .catch(() => {
        if (!cancelled) setLoadFailed(true);
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [browsedFolderId, isOpen, projectId, reloadToken, workspaceSlug]);

  const submit = async () => {
    if (!target) return;

    setSaving(true);
    setError(null);
    try {
      await fileService.updateProjectFile(workspaceSlug, projectId, file.id, { folder_id: target.folderId });
      onChanged();
      onNotice({ tone: "info", text: `Moved to ${target.name}.` });
      onClose();
    } catch (failure) {
      setError(readUploadFailure(failure).message ?? "We could not move this file.");
    } finally {
      setSaving(false);
    }
  };

  const choose = (folderId: string | null, name: string) => setTarget({ folderId, name });
  /**
   * Walk to a level, choosing it as the destination too unless the file already lives
   * there: the file's own folder is the one place worth looking inside and never a
   * destination, so the row that selects it is disabled while the control that opens it
   * is not.
   */
  const openLevel = (folderId: string | null, name: string) => {
    setBrowsedFolderId(folderId);
    if (folderId !== currentFolderId) choose(folderId, name);
  };
  const destinationClassName = (folderId: string | null) =>
    cn(
      "flex items-center gap-1 rounded-sm px-2 py-1 text-left text-body-xs-regular",
      FILES_FOCUS_RING,
      target?.folderId === folderId
        ? "bg-accent-primary text-on-color"
        : "bg-layer-1 text-primary hover:bg-layer-1-hover",
      folderId === currentFolderId && folderId !== null ? "opacity-60" : ""
    );

  return (
    <ModalCore isOpen={isOpen} position={EModalPosition.CENTER} width={EModalWidth.XXL} handleClose={onClose}>
      <div data-testid="files-drawer-move-modal" className={DIALOG_BODY_CLASS_NAME}>
        <Dialog.Title className={DIALOG_TITLE_CLASS_NAME}>Move {file.name_display}</Dialog.Title>
        <Dialog.Description className={DIALOG_NOTE_CLASS_NAME}>
          Choose the folder it should live in. The stored object keeps its key.
        </Dialog.Description>

        <nav aria-label="Folder path" className="flex flex-wrap items-center gap-1">
          <button
            type="button"
            data-testid="files-drawer-move-crumb-root"
            className={destinationClassName(null)}
            onClick={() => openLevel(null, "Project root")}
          >
            Project root
          </button>
          {breadcrumbs.map((crumb) => (
            <span key={crumb.id} className="flex items-center gap-1">
              <span aria-hidden="true" className={DIALOG_NOTE_CLASS_NAME}>
                /
              </span>
              <button
                type="button"
                data-testid={`files-drawer-move-crumb-${crumb.id}`}
                className={destinationClassName(crumb.id)}
                onClick={() => openLevel(crumb.id, crumb.name)}
              >
                {crumb.name}
              </button>
            </span>
          ))}
        </nav>

        {isLoading ? (
          <p className={DIALOG_NOTE_CLASS_NAME}>Loading folders…</p>
        ) : loadFailed ? (
          <div className="flex flex-col items-start gap-2" role="alert">
            <p className="text-caption-md-regular text-danger-primary">We could not load the folder tree.</p>
            <Button
              data-testid="files-drawer-move-retry"
              variant="ghost"
              size="base"
              className={FILES_FOCUS_RING}
              onClick={() => setReloadToken((token) => token + 1)}
            >
              Retry
            </Button>
          </div>
        ) : (
          <ul data-testid="files-drawer-move-folders" className="flex flex-col gap-1">
            {folders.length === 0 && <li className={DIALOG_NOTE_CLASS_NAME}>This folder has no subfolders.</li>}
            {folders.map((folder) => (
              <li key={folder.id} className="flex items-center gap-1">
                <button
                  type="button"
                  data-testid={`files-drawer-move-folder-${folder.id}`}
                  className={cn(destinationClassName(folder.id), "flex-1")}
                  aria-disabled={folder.id === currentFolderId}
                  disabled={folder.id === currentFolderId}
                  onClick={() => choose(folder.id, folder.name)}
                >
                  <span className="truncate">{folder.name}</span>
                  {folder.id === currentFolderId && <span className={DIALOG_NOTE_CLASS_NAME}>· current folder</span>}
                </button>
                <Button
                  data-testid={`files-drawer-move-open-${folder.id}`}
                  variant="ghost"
                  size="base"
                  className={FILES_FOCUS_RING}
                  aria-label={`Open ${folder.name}`}
                  onClick={() => openLevel(folder.id, folder.name)}
                >
                  Open
                </Button>
              </li>
            ))}
          </ul>
        )}

        <p data-testid="files-drawer-move-destination" className={DIALOG_NOTE_CLASS_NAME}>
          {target ? `Destination: ${target.name}` : "Destination: none chosen yet"}
        </p>

        {error && (
          <p data-testid="files-drawer-move-error" role="alert" className="text-caption-md-regular text-danger-primary">
            {error}
          </p>
        )}

        <div className="flex items-center justify-end gap-2">
          <Button
            data-testid="files-drawer-move-cancel"
            variant="secondary"
            size="base"
            className={FILES_FOCUS_RING}
            onClick={onClose}
          >
            Cancel
          </Button>
          <Button
            data-testid="files-drawer-move-confirm"
            variant="primary"
            size="base"
            className={FILES_FOCUS_RING}
            disabled={!target || saving}
            onClick={() => void submit()}
          >
            Move here
          </Button>
        </div>
      </div>
    </ModalCore>
  );
}

/** The purge confirmation: irreversible, and only ever offered for a trashed file (AC-12). */
function PurgeDialog(props: {
  workspaceSlug: string;
  projectId: string;
  detail: IProjectFileDetail;
  isOpen: boolean;
  onClose: () => void;
  onPurged: () => void;
}) {
  const { workspaceSlug, projectId, detail, isOpen, onClose, onPurged } = props;
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!isOpen) return;
    setError(null);
  }, [isOpen]);

  const submit = async () => {
    setSaving(true);
    setError(null);
    try {
      await fileService.purgeProjectFile(workspaceSlug, projectId, detail.file.id);
      onPurged();
    } catch (failure) {
      setError(readUploadFailure(failure).message ?? "We could not delete this file.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <ModalCore isOpen={isOpen} position={EModalPosition.CENTER} width={EModalWidth.XXL} handleClose={onClose}>
      <div data-testid="files-drawer-purge-modal" className={DIALOG_BODY_CLASS_NAME}>
        <Dialog.Title className={DIALOG_TITLE_CLASS_NAME}>{purgeConfirmCopy(detail.file.name_display)}</Dialog.Title>
        <Dialog.Description className={DIALOG_NOTE_CLASS_NAME}>
          Every stored version of this file is deleted from the bucket and the file is removed from the project. Audit
          records of what happened to it survive.
        </Dialog.Description>
        {error && (
          <p
            data-testid="files-drawer-purge-error"
            role="alert"
            className="text-caption-md-regular text-danger-primary"
          >
            {error}
          </p>
        )}
        <div className="flex items-center justify-end gap-2">
          <Button
            data-testid="files-drawer-purge-cancel"
            variant="secondary"
            size="base"
            className={FILES_FOCUS_RING}
            onClick={onClose}
          >
            Cancel
          </Button>
          <Button
            data-testid="files-drawer-purge-confirm"
            variant="error-fill"
            size="base"
            className={FILES_FOCUS_RING}
            disabled={saving}
            onClick={() => void submit()}
          >
            Delete permanently
          </Button>
        </div>
      </div>
    </ModalCore>
  );
}

/**
 * The question a verified revision upload ends in (DESIGN §7, AC-43).
 *
 * Neither answer is on a timer and neither is the default: "Make active" calls the
 * activation endpoint, "Keep current" writes nothing at all, because the server already
 * stored the revision superseded. Declining therefore leaves the active pointer exactly
 * where it was and the new revision listed, downloadable and activatable later from the
 * version history.
 */
function ActivationDialog(props: {
  workspaceSlug: string;
  projectId: string;
  detail: IProjectFileDetail;
  versionNo: number | null;
  onClose: () => void;
  onChanged: () => void;
  onNotice: (notice: TDrawerNotice | null) => void;
}) {
  const { workspaceSlug, projectId, detail, versionNo, onClose, onChanged, onNotice } = props;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (versionNo === null) return;
    setError(null);
  }, [versionNo]);

  const activeVersionNo = detail.versions.find((version) => version.is_active)?.version_no ?? null;

  const activate = async () => {
    if (versionNo === null) return;

    setBusy(true);
    setError(null);
    try {
      await fileService.activateProjectFileVersion(workspaceSlug, projectId, detail.file.id, versionNo);
      onChanged();
      onNotice({ tone: "info", text: versionActivatedCopy(versionNo) });
      onClose();
    } catch (failure) {
      setError(readUploadFailure(failure).message ?? "We could not activate that version.");
    } finally {
      setBusy(false);
    }
  };

  const keepCurrent = () => {
    if (versionNo === null) return;

    onChanged();
    onNotice({
      tone: "info",
      text: activeVersionNo === null ? versionSavedCopy(versionNo) : versionDeclinedCopy(versionNo, activeVersionNo),
    });
    onClose();
  };

  return (
    <ModalCore
      isOpen={versionNo !== null}
      position={EModalPosition.CENTER}
      width={EModalWidth.XXL}
      handleClose={keepCurrent}
    >
      <div data-testid="files-drawer-activation-modal" className={DIALOG_BODY_CLASS_NAME}>
        <Dialog.Title className={DIALOG_TITLE_CLASS_NAME}>
          {versionNo === null ? "" : versionActivationAskCopy(versionNo)}
        </Dialog.Title>
        <Dialog.Description className={DIALOG_NOTE_CLASS_NAME}>
          {versionNo === null
            ? ""
            : `v${versionNo} was uploaded and verified under the same file ID and is not active yet — preview and download still follow ${
                activeVersionNo === null ? "the current version" : `v${activeVersionNo}`
              }.`}
        </Dialog.Description>
        <p className={DIALOG_NOTE_CLASS_NAME}>
          <span className="text-primary">Make active</span> moves preview and download to v{versionNo}.{" "}
          <span className="text-primary">Keep current</span> saves v{versionNo} as superseded; it stays downloadable and
          can be activated later from the version history.
        </p>
        {error && (
          <p
            data-testid="files-drawer-activation-error"
            role="alert"
            className="text-caption-md-regular text-danger-primary"
          >
            {error}
          </p>
        )}
        <div className="flex items-center justify-end gap-2">
          <Button
            data-testid="files-drawer-activation-decline"
            variant="secondary"
            size="base"
            className={FILES_FOCUS_RING}
            disabled={busy}
            onClick={keepCurrent}
          >
            Keep current
          </Button>
          <Button
            data-testid="files-drawer-activation-confirm"
            variant="primary"
            size="base"
            className={FILES_FOCUS_RING}
            disabled={busy}
            onClick={() => void activate()}
          >
            Make active
          </Button>
        </div>
      </div>
    </ModalCore>
  );
}
