/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { FolderInput, FolderPen, FolderPlus, FolderTree, MoreHorizontal, Trash2 } from "lucide-react";
// plane imports
import { Button } from "@plane/propel/button";
import { CustomMenu, EModalPosition, EModalWidth, Input, ModalCore } from "@plane/ui";
import type { IProjectFolder } from "@/services/project-file.service";
// helpers
import { readUploadFailure } from "./upload-queue";

export type TFolderDialog =
  | { kind: "create"; parentId: string | null }
  | { kind: "rename"; folder: IProjectFolder }
  | { kind: "move"; folder: IProjectFolder }
  | { kind: "delete"; folder: IProjectFolder };

type FolderActionsProps = {
  folder: IProjectFolder;
  onRename: (folder: IProjectFolder) => void;
  onMove: (folder: IProjectFolder) => void;
  onDelete: (folder: IProjectFolder) => void;
};

/** The mutating menu is intentionally available on every folder row, not just the current folder. */
export function FolderActions(props: FolderActionsProps) {
  const { folder, onRename, onMove, onDelete } = props;

  return (
    <div
      role="presentation"
      className="shrink-0"
      onClick={(event) => event.stopPropagation()}
      onKeyDown={(event) => event.stopPropagation()}
    >
      <CustomMenu
        ellipsis
        closeOnSelect
        placement="bottom-end"
        ariaLabel={`Actions for ${folder.name}`}
        customButton={<MoreHorizontal className="size-4 text-tertiary" aria-hidden="true" />}
      >
        <CustomMenu.MenuItem onClick={() => onRename(folder)}>
          <div className="flex items-center gap-2">
            <FolderPen className="size-3.5" aria-hidden="true" />
            <span>Rename folder</span>
          </div>
        </CustomMenu.MenuItem>
        <CustomMenu.MenuItem onClick={() => onMove(folder)}>
          <div className="flex items-center gap-2">
            <FolderInput className="size-3.5" aria-hidden="true" />
            <span>Move folder</span>
          </div>
        </CustomMenu.MenuItem>
        <CustomMenu.MenuItem onClick={() => onDelete(folder)}>
          <div className="flex items-center gap-2 text-danger-primary">
            <Trash2 className="size-3.5" aria-hidden="true" />
            <span>Delete folder</span>
          </div>
        </CustomMenu.MenuItem>
      </CustomMenu>
    </div>
  );
}

type FolderNameDialogProps = {
  isOpen: boolean;
  onClose: () => void;
  title: string;
  initialName?: string;
  submitLabel: string;
  submittingLabel: string;
  onSubmit: (name: string) => Promise<void>;
};

function FolderNameDialog(props: FolderNameDialogProps) {
  const { isOpen, onClose, title, initialName = "", submitLabel, submittingLabel, onSubmit } = props;
  const [name, setName] = useState(initialName);
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!isOpen) return;
    setName(initialName);
    setError(null);
    const timer = setTimeout(() => inputRef.current?.focus(), 100);
    return () => clearTimeout(timer);
  }, [initialName, isOpen]);

  const handleClose = () => {
    if (isSubmitting) return;
    onClose();
  };

  const handleSubmit = async () => {
    const trimmed = name.trim();
    if (!trimmed) {
      setError("Folder name cannot be empty.");
      return;
    }
    if (trimmed === initialName.trim()) {
      onClose();
      return;
    }

    setIsSubmitting(true);
    setError(null);
    try {
      await onSubmit(trimmed);
      onClose();
    } catch (failure) {
      setError(readUploadFailure(failure).message ?? "We could not save this folder.");
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <ModalCore isOpen={isOpen} handleClose={handleClose} position={EModalPosition.CENTER} width={EModalWidth.SM}>
      <div>
        <div className="space-y-4 p-5">
          <h3 className="text-18 font-medium text-secondary">{title}</h3>
          <Input
            ref={inputRef}
            type="text"
            value={name}
            onChange={(event) => setName(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                event.preventDefault();
                void handleSubmit();
              }
            }}
            placeholder="Folder name"
            aria-label="Folder name"
            className="w-full"
            disabled={isSubmitting}
          />
          {error && <p className="text-caption-md-regular text-danger-primary">{error}</p>}
        </div>
        <div className="flex items-center justify-end gap-2 border-t-[0.5px] border-subtle px-5 py-4">
          <Button variant="secondary" size="lg" onClick={handleClose} disabled={isSubmitting}>
            Cancel
          </Button>
          <Button variant="primary" size="lg" loading={isSubmitting} onClick={() => void handleSubmit()}>
            {isSubmitting ? submittingLabel : submitLabel}
          </Button>
        </div>
      </div>
    </ModalCore>
  );
}

type FolderMoveDialogProps = {
  isOpen: boolean;
  folder: IProjectFolder;
  folders: IProjectFolder[];
  isLoading: boolean;
  loadError: string | null;
  onRetry: () => void;
  onClose: () => void;
  onSubmit: (parentId: string | null) => Promise<void>;
};

/** Return valid move destinations, excluding the folder and every descendant below it. */
export function getFolderMoveDestinations(folders: IProjectFolder[], movingFolderId: string): IProjectFolder[] {
  const excluded = new Set([movingFolderId]);
  let added = true;

  while (added) {
    added = false;
    folders.forEach((candidate) => {
      if (candidate.parent_id && excluded.has(candidate.parent_id) && !excluded.has(candidate.id)) {
        excluded.add(candidate.id);
        added = true;
      }
    });
  }

  return folders
    .filter((candidate) => !excluded.has(candidate.id))
    .reduce<IProjectFolder[]>((destinations, candidate) => {
      const insertionIndex = destinations.findIndex(
        (current) =>
          current.depth > candidate.depth ||
          (current.depth === candidate.depth && current.name.localeCompare(candidate.name) > 0)
      );
      destinations.splice(insertionIndex === -1 ? destinations.length : insertionIndex, 0, candidate);
      return destinations;
    }, []);
}

function FolderMoveDialog(props: FolderMoveDialogProps) {
  const { isOpen, folder, folders, isLoading, loadError, onRetry, onClose, onSubmit } = props;
  const [parentId, setParentId] = useState<string | null>(folder.parent_id);
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);

  const destinations = useMemo(() => getFolderMoveDestinations(folders, folder.id), [folder.id, folders]);

  useEffect(() => {
    if (!isOpen) return;
    setParentId(folder.parent_id);
    setError(null);
    setIsSubmitting(false);
  }, [folder.id, folder.parent_id, isOpen]);

  const handleSubmit = async () => {
    if (parentId === folder.parent_id) {
      onClose();
      return;
    }
    setIsSubmitting(true);
    setError(null);
    try {
      await onSubmit(parentId);
      onClose();
    } catch (failure) {
      setError(readUploadFailure(failure).message ?? "We could not move this folder.");
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <ModalCore
      isOpen={isOpen}
      handleClose={isSubmitting ? () => undefined : onClose}
      position={EModalPosition.CENTER}
      width={EModalWidth.SM}
    >
      <div>
        <div className="space-y-4 p-5">
          <div>
            <h3 className="text-18 font-medium text-secondary">Move folder</h3>
            <p className="mt-1 text-caption-md-regular text-tertiary">Choose a destination for {folder.name}.</p>
          </div>
          <div
            className="max-h-72 overflow-y-auto rounded-md border border-subtle p-1"
            role="radiogroup"
            aria-label="Folder destination"
          >
            <button
              type="button"
              role="radio"
              aria-checked={parentId === null}
              className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-body-xs-regular text-primary hover:bg-layer-1"
              onClick={() => setParentId(null)}
              disabled={isSubmitting || folder.parent_id === null}
            >
              <FolderTree className="size-4 shrink-0 text-tertiary" aria-hidden="true" />
              <span>Project root</span>
            </button>
            {isLoading ? (
              <p className="px-2 py-3 text-caption-md-regular text-tertiary">Loading folders…</p>
            ) : loadError ? (
              <div className="space-y-2 px-2 py-3">
                <p className="text-caption-md-regular text-danger-primary">{loadError}</p>
                <Button variant="secondary" size="sm" onClick={onRetry}>
                  Try again
                </Button>
              </div>
            ) : (
              destinations.map((destination) => (
                <button
                  key={destination.id}
                  type="button"
                  role="radio"
                  aria-checked={parentId === destination.id}
                  className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-body-xs-regular text-primary hover:bg-layer-1"
                  style={{ paddingLeft: `${8 + Math.min(destination.depth, 8) * 16}px` }}
                  onClick={() => setParentId(destination.id)}
                  disabled={isSubmitting || folder.parent_id === destination.id}
                >
                  <FolderPlus className="size-4 shrink-0 text-tertiary" aria-hidden="true" />
                  <span className="truncate">{destination.name}</span>
                </button>
              ))
            )}
          </div>
          {error && <p className="text-caption-md-regular text-danger-primary">{error}</p>}
        </div>
        <div className="flex items-center justify-end gap-2 border-t-[0.5px] border-subtle px-5 py-4">
          <Button variant="secondary" size="lg" onClick={onClose} disabled={isSubmitting}>
            Cancel
          </Button>
          <Button
            variant="primary"
            size="lg"
            loading={isSubmitting}
            onClick={() => void handleSubmit()}
            disabled={isLoading || Boolean(loadError)}
          >
            {isSubmitting ? "Moving" : "Move folder"}
          </Button>
        </div>
      </div>
    </ModalCore>
  );
}

type FolderDeleteDialogProps = {
  isOpen: boolean;
  folder: IProjectFolder;
  onClose: () => void;
  onSubmit: () => Promise<void>;
};

function FolderDeleteDialog(props: FolderDeleteDialogProps) {
  const { isOpen, folder, onClose, onSubmit } = props;
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);

  useEffect(() => {
    if (!isOpen) return;
    setError(null);
    setIsSubmitting(false);
  }, [folder.id, isOpen]);

  const handleSubmit = async () => {
    setIsSubmitting(true);
    setError(null);
    try {
      await onSubmit();
      onClose();
    } catch (failure) {
      setError(readUploadFailure(failure).message ?? "We could not delete this folder.");
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <ModalCore
      isOpen={isOpen}
      handleClose={isSubmitting ? () => undefined : onClose}
      position={EModalPosition.CENTER}
      width={EModalWidth.SM}
    >
      <div>
        <div className="space-y-3 p-5">
          <h3 className="text-18 font-medium text-secondary">Delete folder</h3>
          <p className="text-body-xs-regular text-secondary">
            Delete <span className="font-medium text-primary">{folder.name}</span>? Files and subfolders inside it will
            be moved to Trash.
          </p>
          {error && <p className="text-caption-md-regular text-danger-primary">{error}</p>}
        </div>
        <div className="flex items-center justify-end gap-2 border-t-[0.5px] border-subtle px-5 py-4">
          <Button variant="secondary" size="lg" onClick={onClose} disabled={isSubmitting}>
            Cancel
          </Button>
          <Button variant="primary" size="lg" loading={isSubmitting} onClick={() => void handleSubmit()}>
            {isSubmitting ? "Deleting" : "Delete folder"}
          </Button>
        </div>
      </div>
    </ModalCore>
  );
}

type FolderDialogsProps = {
  dialog: TFolderDialog | null;
  folders: IProjectFolder[];
  isFolderTreeLoading: boolean;
  folderTreeError: string | null;
  onRetryFolderTree: () => void;
  onClose: () => void;
  onCreate: (name: string, parentId: string | null) => Promise<void>;
  onRename: (folderId: string, name: string) => Promise<void>;
  onMove: (folderId: string, parentId: string | null) => Promise<void>;
  onDelete: (folderId: string) => Promise<void>;
};

export function FolderDialogs(props: FolderDialogsProps) {
  const {
    dialog,
    folders,
    isFolderTreeLoading,
    folderTreeError,
    onRetryFolderTree,
    onClose,
    onCreate,
    onRename,
    onMove,
    onDelete,
  } = props;

  if (!dialog) return null;

  if (dialog.kind === "create") {
    return (
      <FolderNameDialog
        isOpen
        onClose={onClose}
        title="Create folder"
        submitLabel="Create folder"
        submittingLabel="Creating"
        onSubmit={(name) => onCreate(name, dialog.parentId)}
      />
    );
  }

  if (dialog.kind === "rename") {
    return (
      <FolderNameDialog
        isOpen
        onClose={onClose}
        title="Rename folder"
        initialName={dialog.folder.name}
        submitLabel="Rename folder"
        submittingLabel="Renaming"
        onSubmit={(name) => onRename(dialog.folder.id, name)}
      />
    );
  }

  if (dialog.kind === "move") {
    return (
      <FolderMoveDialog
        isOpen
        folder={dialog.folder}
        folders={folders}
        isLoading={isFolderTreeLoading}
        loadError={folderTreeError}
        onRetry={onRetryFolderTree}
        onClose={onClose}
        onSubmit={(parentId) => onMove(dialog.folder.id, parentId)}
      />
    );
  }

  return (
    <FolderDeleteDialog isOpen folder={dialog.folder} onClose={onClose} onSubmit={() => onDelete(dialog.folder.id)} />
  );
}
