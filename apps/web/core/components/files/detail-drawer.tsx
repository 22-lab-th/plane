/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useCallback, useEffect, useRef, useState } from "react";
// plane imports
import { CloseIcon } from "@plane/propel/icons";
import { IconButton } from "@plane/propel/icon-button";
import { Pill } from "@plane/propel/pill";
import { cn, calculateTimeAgo, renderFormattedDate } from "@plane/utils";
import type { IProjectFileDetail } from "@/services/project-file.service";
import { ProjectFileService } from "@/services/project-file.service";
// helpers
import { FILES_FOCUS_RING, fileKind, formatFileSize } from "./helpers";

const fileService = new ProjectFileService();

type Props = {
  workspaceSlug: string;
  projectId: string;
  fileId: string;
  /** The file is being read out of the trash, so the detail endpoint needs the flag too. */
  trashed: boolean;
  onClose: () => void;
};

/** How much of a file's own history the drawer summarises. */
const ACTIVITY_PREVIEW_LIMIT = 5;

const META_LABEL_CLASS_NAME = "text-caption-md-regular text-tertiary";
const META_VALUE_CLASS_NAME = "text-body-xs-regular text-primary";

/**
 * The detail drawer. Opening it costs one fresh `GET .../files/<id>/` — the
 * drawer never reuses the list payload, because the list row does not carry the
 * versions, links, permissions or history the detail reports.
 */
export function FileDetailDrawer(props: Props) {
  const { workspaceSlug, projectId, fileId, trashed, onClose } = props;

  const panelRef = useRef<HTMLElement | null>(null);
  const [detail, setDetail] = useState<IProjectFileDetail | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [hasError, setHasError] = useState(false);
  const [reloadToken, setReloadToken] = useState(0);

  const fetchDetail = useCallback(() => {
    let cancelled = false;
    setIsLoading(true);
    setHasError(false);

    fileService
      .getProjectFile(workspaceSlug, projectId, fileId, { trashed })
      .then((response) => {
        if (!cancelled) setDetail(response);
        return response;
      })
      .catch(() => {
        if (!cancelled) setHasError(true);
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [fileId, projectId, trashed, workspaceSlug]);

  useEffect(() => fetchDetail(), [fetchDetail, reloadToken]);

  useEffect(() => {
    panelRef.current?.focus();
  }, [fileId]);

  const file = detail?.file;
  const permissions = detail?.permissions;

  return (
    <div className="fixed inset-0 z-30 flex justify-end">
      {/* the overlay dismisses on click; the labelled close button is the keyboard control */}
      <div aria-hidden="true" className="absolute inset-0 bg-black/20" onClick={onClose} />
      <section
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label="File details"
        data-testid="files-drawer"
        data-file-id={fileId}
        tabIndex={-1}
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            event.stopPropagation();
            onClose();
            return;
          }
          if (event.key !== "Tab") return;

          // keep focus inside the drawer (DESIGN §6)
          const focusable = panelRef.current?.querySelectorAll<HTMLElement>(
            'button, [href], input, [tabindex]:not([tabindex="-1"])'
          );
          if (!focusable || focusable.length === 0) {
            event.preventDefault();
            return;
          }
          const first = focusable[0];
          const last = focusable[focusable.length - 1];
          if (event.shiftKey && (document.activeElement === first || document.activeElement === panelRef.current)) {
            event.preventDefault();
            last.focus();
          } else if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault();
            first.focus();
          }
        }}
        className="relative flex h-full w-full flex-col overflow-y-auto border-l border-subtle bg-surface-1 outline-none md:w-[420px] xl:w-[480px]"
      >
        <header className="flex items-start justify-between gap-2 border-b border-subtle px-4 py-3">
          <div className="flex min-w-0 flex-col gap-1">
            <h2 className="truncate text-body-sm-medium text-primary">{file?.name_display ?? "Loading file"}</h2>
            {file && (
              <div className="flex items-center gap-2">
                <span className="rounded-sm bg-layer-3 px-1.5 py-0.5 text-caption-md-medium text-secondary uppercase">
                  {file.extension}
                </span>
                <span className={META_LABEL_CLASS_NAME}>{fileKind(file)}</span>
                {file.is_pinned && (
                  <Pill variant="info" size="xs">
                    Pinned
                  </Pill>
                )}
                {file.trashed && (
                  <Pill variant="warning" size="xs">
                    In trash
                  </Pill>
                )}
              </div>
            )}
          </div>
          <IconButton
            variant="ghost"
            size="base"
            icon={CloseIcon}
            aria-label="Close file details"
            className={cn(FILES_FOCUS_RING, "rounded-md")}
            onClick={onClose}
          />
        </header>
        <div className="flex flex-col gap-4 px-4 py-3">
          {isLoading && !detail && <p className="text-caption-md-regular text-tertiary">Loading file details…</p>}
          {hasError && !detail && (
            <div className="flex flex-col items-start gap-2" role="alert">
              <p className="text-caption-md-regular text-danger-primary">We could not load this file.</p>
              <button
                type="button"
                className={cn("rounded text-caption-md-medium text-link-primary", FILES_FOCUS_RING)}
                onClick={() => setReloadToken((token) => token + 1)}
              >
                Retry
              </button>
            </div>
          )}
          {file && permissions && detail && (
            <>
              <dl className="grid grid-cols-2 gap-x-4 gap-y-2">
                <div className="flex flex-col">
                  <dt className={META_LABEL_CLASS_NAME}>Size</dt>
                  <dd className={META_VALUE_CLASS_NAME}>{formatFileSize(file.size_bytes)}</dd>
                </div>
                <div className="flex flex-col">
                  <dt className={META_LABEL_CLASS_NAME}>Type</dt>
                  <dd className={META_VALUE_CLASS_NAME}>{file.mime_type}</dd>
                </div>
                <div className="flex flex-col">
                  <dt className={META_LABEL_CLASS_NAME}>Owner</dt>
                  <dd className={META_VALUE_CLASS_NAME}>{file.uploader?.display_name ?? "—"}</dd>
                </div>
                <div className="flex flex-col">
                  <dt className={META_LABEL_CLASS_NAME}>Links</dt>
                  <dd className={META_VALUE_CLASS_NAME}>{String(file.link_count)}</dd>
                </div>
                <div className="flex flex-col">
                  <dt className={META_LABEL_CLASS_NAME}>Created</dt>
                  <dd className={META_VALUE_CLASS_NAME}>{renderFormattedDate(file.created_at)}</dd>
                </div>
                <div className="flex flex-col">
                  <dt className={META_LABEL_CLASS_NAME}>Updated</dt>
                  <dd className={META_VALUE_CLASS_NAME}>{renderFormattedDate(file.updated_at)}</dd>
                </div>
              </dl>
              {!permissions.can_download && (
                <p className="text-caption-md-regular text-tertiary">This file is not available for download.</p>
              )}
              <section className="flex flex-col gap-2">
                <h3 className={META_LABEL_CLASS_NAME}>Versions ({detail.versions.length})</h3>
                <ul className="flex flex-col gap-1">
                  {detail.versions.map((version) => (
                    <li
                      key={version.id}
                      className="flex items-center justify-between gap-2 rounded-sm bg-layer-1 px-2 py-1"
                    >
                      <span className={META_VALUE_CLASS_NAME}>
                        v{version.version_no} · {formatFileSize(version.size_bytes)}
                      </span>
                      <span className="flex items-center gap-2">
                        <span className={META_LABEL_CLASS_NAME}>{version.status}</span>
                        {version.is_active && (
                          <Pill variant="success" size="xs">
                            Active
                          </Pill>
                        )}
                      </span>
                    </li>
                  ))}
                </ul>
              </section>
              <section className="flex flex-col gap-2">
                <h3 className={META_LABEL_CLASS_NAME}>Activity</h3>
                {detail.activity.length === 0 ? (
                  <p className="text-caption-md-regular text-tertiary">No recorded activity.</p>
                ) : (
                  <ul className="flex flex-col gap-1">
                    {detail.activity.slice(0, ACTIVITY_PREVIEW_LIMIT).map((entry) => (
                      <li key={entry.id} className="flex items-center justify-between gap-2">
                        <span className={META_VALUE_CLASS_NAME}>
                          {entry.action} · {entry.actor?.display_name ?? entry.actor_display ?? "Unknown"}
                        </span>
                        <span className={META_LABEL_CLASS_NAME}>{calculateTimeAgo(entry.created_at)}</span>
                      </li>
                    ))}
                  </ul>
                )}
              </section>
            </>
          )}
        </div>
      </section>
    </div>
  );
}
