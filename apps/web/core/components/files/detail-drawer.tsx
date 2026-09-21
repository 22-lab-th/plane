/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
// plane imports
import { Button } from "@plane/propel/button";
import { CloseIcon } from "@plane/propel/icons";
import { IconButton } from "@plane/propel/icon-button";
import { EPillSize, EPillVariant, Pill } from "@plane/propel/pill";
import { cn, calculateTimeAgo, renderFormattedDate, renderFormattedTime } from "@plane/utils";
// services
import type { IProjectFileAccessUrl, IProjectFileDetail, IProjectFileVersion } from "@/services/project-file.service";
import { ProjectFileService } from "@/services/project-file.service";
// components
import type { TDrawerDialog, TDrawerNotice } from "./detail-drawer-actions";
import { DownloadButton, FileDetailActions } from "./detail-drawer-actions";
import { readUploadFailure } from "./upload-queue";
// helpers
import {
  FILES_FOCUS_RING,
  VERSION_STATUS_LABELS,
  fileActivityCopy,
  fileKind,
  formatFileSize,
  isAlwaysDownloadMime,
  isInlineRenderableMime,
  versionActivatedCopy,
} from "./helpers";

const fileService = new ProjectFileService();

type Props = {
  workspaceSlug: string;
  projectId: string;
  fileId: string;
  /** The file is being read out of the trash, so the detail endpoint needs the flag too. */
  trashed: boolean;
  /** Only a project ADMIN may purge; the API refuses the call for anyone else (AC-27). */
  isProjectAdmin: boolean;
  onClose: () => void;
  /** A successful restore leaves Trash and closes this stale drawer. */
  onRestored: () => void;
  /** A write landed: the listing behind the drawer is stale too. */
  onFileMutated: () => void;
};

const META_LABEL_CLASS_NAME = "text-caption-md-regular text-tertiary";
const META_VALUE_CLASS_NAME = "text-body-xs-regular text-primary";
const PANEL_CLASS_NAME = "rounded-md border border-subtle bg-layer-1 px-2 py-1";

/** Used only when the refusal carries no message of its own. */
const FALLBACK_PREVIEW_FAILURE = "We could not prepare a preview for this file.";

/**
 * A version's timestamp: the calendar date the rest of the panel prints, and the
 * version's real local time beside it.
 *
 * The two parts have to come from two helpers. `renderFormattedDate` reads a string's
 * first ten characters - the API's own UTC calendar date, the same day `Created` and
 * `Updated` print - so the date never shifts a day under the reader's timezone, but it
 * cannot carry a clock: passing `HH:mm` in the format still yields midnight, because its
 * `getDate` rebuilds the string as local midnight (DEFECT-004). `renderFormattedTime`
 * formats the instant itself, which is exactly the half that was missing. Composing the
 * pair is what the rest of the app does (`comments/card/display.tsx`).
 */
const formatVersionTimestamp = (iso: string): string => {
  const date = renderFormattedDate(iso) ?? iso;
  const time = renderFormattedTime(iso);
  return time ? `${date}, ${time}` : date;
};

/** The status colour a version's chip carries; text always says which status it is. */
const versionStatusVariant = (version: IProjectFileVersion): EPillVariant => {
  if (version.is_active) return EPillVariant.SUCCESS;
  if (version.status === "failed" || version.status === "purged" || version.status === "purge_failed")
    return EPillVariant.ERROR;
  if (version.status === "uploading") return EPillVariant.WARNING;
  return EPillVariant.DEFAULT;
};

/**
 * The detail drawer. Opening it costs one fresh `GET .../files/<id>/` — the
 * drawer never reuses the list payload, because the list row does not carry the
 * versions, links, permissions or history the detail reports — and one fresh
 * presigned preview URL, because a URL is signed with a TTL and belongs to one
 * rendering (DESIGN §8).
 *
 * What it shows is derived from that payload: the preview's version, the version
 * list with its observed `ETag`s, the links the file is attached to, the file's own
 * audit history, and the actions the payload's `permissions` block allows. Nothing
 * here decides on its own whether a script-capable type may be rendered inline: the
 * server signs SVG and HTML as downloads, and the drawer refuses them a second time.
 */
export function FileDetailDrawer(props: Props) {
  const { workspaceSlug, projectId, fileId, trashed, isProjectAdmin, onClose, onRestored, onFileMutated } = props;

  const panelRef = useRef<HTMLElement | null>(null);
  const [detail, setDetail] = useState<IProjectFileDetail | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [hasError, setHasError] = useState(false);
  const [reloadToken, setReloadToken] = useState(0);

  // The version the preview has been asked for; `null` follows the active version.
  const [previewVersionNo, setPreviewVersionNo] = useState<number | null>(null);
  const [preview, setPreview] = useState<IProjectFileAccessUrl | null>(null);
  const [previewFailure, setPreviewFailure] = useState<string | null>(null);
  const [previewToken, setPreviewToken] = useState(0);

  const [dialog, setDialog] = useState<TDrawerDialog | null>(null);
  const [notice, setNotice] = useState<TDrawerNotice | null>(null);

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

  // Every open signs its own URL, and so does every change of file, of previewed
  // version or of the active version underneath (R-DL-2, DESIGN §8).
  useEffect(() => {
    let cancelled = false;
    setPreview(null);
    setPreviewFailure(null);

    fileService
      .getProjectFilePreviewUrl(workspaceSlug, projectId, fileId, { versionNo: previewVersionNo })
      .then((response) => {
        if (!cancelled) setPreview(response);
        return response;
      })
      .catch((error) => {
        // The refusal is the server's, and it explains itself ("This file is in the
        // trash; restore it before downloading it."), so the reason is shown as it
        // arrived rather than replaced with a generic one.
        if (!cancelled) setPreviewFailure(readUploadFailure(error).message ?? FALLBACK_PREVIEW_FAILURE);
      });

    return () => {
      cancelled = true;
    };
  }, [fileId, previewToken, previewVersionNo, projectId, workspaceSlug]);

  useEffect(() => {
    panelRef.current?.focus();
  }, [fileId]);

  const file = detail?.file;
  const permissions = detail?.permissions;
  const versions = useMemo(() => detail?.versions ?? [], [detail]);
  const activeVersion = useMemo(
    () => versions.find((version) => version.is_active) ?? detail?.version ?? null,
    [detail, versions]
  );
  /** The version the signed URL is for, as the server reported it. */
  const previewVersion = useMemo(
    () => (preview ? (versions.find((version) => version.version_no === preview.version_no) ?? null) : null),
    [preview, versions]
  );
  const metadataVersion = previewVersion ?? activeVersion;

  const handleMutated = useCallback(() => {
    setReloadToken((token) => token + 1);
    // A new active version is a different URL, not the same one reused.
    setPreviewToken((token) => token + 1);
    onFileMutated();
  }, [onFileMutated]);

  /**
   * Make an already stored version active.
   *
   * No question here: the explicit confirmation exists for a *newly uploaded* revision,
   * which the user has not seen or chosen yet (AC-43). Picking a version out of the
   * history is that choice, and it is reversible from the same list, so it moves the
   * pointer and says so (the prototype activates the same way).
   */
  const handleActivated = useCallback(() => {
    // An explicit selection remains pinned until activation really succeeds. Once it
    // has, the drawer follows the new active version and signs that version afresh.
    setPreviewVersionNo(null);
    handleMutated();
  }, [handleMutated]);

  const activateVersion = useCallback(
    async (versionNo: number) => {
      try {
        await fileService.activateProjectFileVersion(workspaceSlug, projectId, fileId, versionNo);
        handleActivated();
        setNotice({ tone: "info", text: versionActivatedCopy(versionNo) });
      } catch (failure) {
        setNotice({
          tone: "error",
          text: readUploadFailure(failure).message ?? "We could not activate that version.",
        });
      }
    },
    [fileId, handleActivated, projectId, workspaceSlug]
  );

  const handlePurged = useCallback(() => {
    onFileMutated();
    onClose();
  }, [onClose, onFileMutated]);

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
            // A confirmation owns Escape while it is open: it closes the question,
            // not the drawer the question was asked from.
            if (dialog) {
              setDialog(null);
              return;
            }
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
                  <Pill variant={EPillVariant.INFO} size={EPillSize.XS}>
                    Pinned
                  </Pill>
                )}
                {file.trashed && (
                  <Pill variant={EPillVariant.WARNING} size={EPillSize.XS}>
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
            data-testid="files-drawer-close"
            className={cn(FILES_FOCUS_RING, "rounded-md")}
            onClick={onClose}
          />
        </header>
        {detail && (
          <FileDetailActions
            workspaceSlug={workspaceSlug}
            projectId={projectId}
            detail={detail}
            isProjectAdmin={isProjectAdmin}
            dialog={dialog}
            onDialogChange={setDialog}
            onNotice={setNotice}
            onRestored={onRestored}
            onChanged={handleMutated}
            onActivated={handleActivated}
            onPurged={handlePurged}
          />
        )}
        <div className="flex flex-col gap-4 px-4 py-3">
          {notice && (
            <p
              data-testid="files-drawer-notice"
              role={notice.tone === "error" ? "alert" : "status"}
              className={cn(
                "rounded-md px-2 py-1 text-caption-md-regular",
                notice.tone === "error" ? "text-danger-primary" : "text-secondary"
              )}
            >
              {notice.text}
            </p>
          )}
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
          {permissions && !permissions.can_download && (
            <p className="text-caption-md-regular text-tertiary">This file is not available for download.</p>
          )}
          {file && permissions && detail && (
            <>
              <section data-testid="files-drawer-preview" className="flex flex-col gap-2">
                <h3 className={META_LABEL_CLASS_NAME}>Preview</h3>
                <PreviewSurface
                  name={file.name_display}
                  extension={file.extension}
                  mimeType={metadataVersion?.mime_type ?? file.mime_type}
                  signed={preview}
                  failure={previewFailure}
                  onRetry={() => setPreviewToken((token) => token + 1)}
                />
                <div className="flex items-end justify-between gap-2">
                  <div className="flex flex-col gap-1">
                    {preview && preview.version_no !== (activeVersion?.version_no ?? null) && (
                      <Pill variant={EPillVariant.WARNING} size={EPillSize.XS} className="w-fit">
                        Previewing v{preview.version_no} (not active)
                      </Pill>
                    )}
                    {preview && (
                      <span className={META_LABEL_CLASS_NAME}>
                        Signed link for v{preview.version_no} · expires {formatVersionTimestamp(preview.expires_at)} ·{" "}
                        {preview.disposition}
                      </span>
                    )}
                  </div>
                  {versions.length > 0 && (
                    <label className="flex items-center gap-2" htmlFor="files-drawer-preview-version-select">
                      <span className={META_LABEL_CLASS_NAME}>Version</span>
                      <select
                        id="files-drawer-preview-version-select"
                        data-testid="files-drawer-preview-version"
                        className={cn(
                          "rounded-md border border-strong bg-layer-2 px-2 py-1 text-caption-md-regular text-primary",
                          FILES_FOCUS_RING
                        )}
                        value={String(previewVersionNo ?? activeVersion?.version_no ?? "")}
                        onChange={(event) =>
                          setPreviewVersionNo(event.target.value ? Number(event.target.value) : null)
                        }
                      >
                        {versions.map((version) => (
                          <option key={version.id} value={String(version.version_no)}>
                            v{version.version_no}
                            {version.is_active ? " (active)" : ""}
                          </option>
                        ))}
                      </select>
                    </label>
                  )}
                </div>
              </section>

              <section className="flex flex-col gap-2">
                <h3 className={META_LABEL_CLASS_NAME}>Details</h3>
                <dl className="grid grid-cols-2 gap-x-4 gap-y-2">
                  <div className="flex flex-col">
                    <dt className={META_LABEL_CLASS_NAME}>Size</dt>
                    <dd className={META_VALUE_CLASS_NAME}>
                      {formatFileSize(metadataVersion?.size_bytes ?? file.size_bytes)}
                      {metadataVersion ? ` (v${metadataVersion.version_no})` : ""}
                    </dd>
                  </div>
                  <div className="flex flex-col">
                    <dt className={META_LABEL_CLASS_NAME}>Type</dt>
                    <dd className={META_VALUE_CLASS_NAME}>{file.mime_type}</dd>
                  </div>
                  <div className="flex flex-col">
                    <dt className={META_LABEL_CLASS_NAME}>Category</dt>
                    <dd className={META_VALUE_CLASS_NAME}>{file.category}</dd>
                  </div>
                  <div className="flex flex-col">
                    <dt className={META_LABEL_CLASS_NAME}>Uploader</dt>
                    <dd className={META_VALUE_CLASS_NAME}>
                      {metadataVersion?.uploaded_by?.display_name ?? file.uploader?.display_name ?? "—"}
                    </dd>
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
                  <div className="col-span-2 flex flex-col">
                    <dt className={META_LABEL_CLASS_NAME}>Checksum (SHA-256)</dt>
                    <dd
                      data-testid="files-drawer-checksum"
                      className={cn(META_VALUE_CLASS_NAME, "font-mono break-all")}
                    >
                      {metadataVersion?.client_checksum_sha256 || file.checksum_sha256 || "—"}
                    </dd>
                  </div>
                  <div className="col-span-2 flex flex-col">
                    <dt className={META_LABEL_CLASS_NAME}>Object key</dt>
                    <dd
                      data-testid="files-drawer-object-key"
                      className={cn(META_VALUE_CLASS_NAME, "font-mono break-all")}
                    >
                      {file.object_key}
                    </dd>
                  </div>
                </dl>
                <p className={META_LABEL_CLASS_NAME}>
                  The object key is write-once: renaming or moving a file changes metadata only, never the key.
                </p>
              </section>

              <section data-testid="files-drawer-links" className="flex flex-col gap-2">
                <h3 className={META_LABEL_CLASS_NAME}>Links ({detail.link_count})</h3>
                {detail.links.length === 0 ? (
                  <p className={META_LABEL_CLASS_NAME}>
                    No live links — this file appears under Orphan until it is attached to an issue or page.
                  </p>
                ) : (
                  <ul className="flex flex-col gap-1">
                    {detail.links.map((link) => (
                      <li
                        key={link.id}
                        data-testid={`files-drawer-link-${link.id}`}
                        className={cn("flex items-center gap-2", PANEL_CLASS_NAME)}
                      >
                        <Pill variant={EPillVariant.DEFAULT} size={EPillSize.XS}>
                          {link.entity_type}
                        </Pill>
                        <span className={cn(META_VALUE_CLASS_NAME, "truncate")}>
                          {link.entity_identifier || link.entity_id}
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
              </section>

              <section data-testid="files-drawer-versions" className="flex flex-col gap-2">
                <h3 className={META_LABEL_CLASS_NAME}>Version history ({versions.length})</h3>
                <ul className="flex flex-col gap-2">
                  {versions.map((version) => (
                    <li
                      key={version.id}
                      data-testid={`files-drawer-version-${version.version_no}`}
                      data-version={version.version_no}
                      data-status={version.status}
                      className={cn("flex flex-col gap-1", PANEL_CLASS_NAME)}
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span className={META_VALUE_CLASS_NAME}>v{version.version_no}</span>
                        <Pill variant={versionStatusVariant(version)} size={EPillSize.XS}>
                          {VERSION_STATUS_LABELS[version.status] ?? version.status}
                        </Pill>
                      </div>
                      <div className={META_LABEL_CLASS_NAME}>
                        {version.uploaded_by?.display_name ?? "Unknown uploader"} · {formatFileSize(version.size_bytes)}{" "}
                        · {formatVersionTimestamp(version.created_at)}
                      </div>
                      <div className="flex items-center justify-between gap-2">
                        <span
                          data-testid={`files-drawer-version-etag-${version.version_no}`}
                          className={cn("font-mono truncate", META_LABEL_CLASS_NAME)}
                        >
                          ETag {version.etag || "—"}
                        </span>
                        <span className="flex items-center gap-2">
                          {permissions.can_download && (
                            <DownloadButton
                              workspaceSlug={workspaceSlug}
                              projectId={projectId}
                              fileId={file.id}
                              versionNo={version.version_no}
                            />
                          )}
                          {/* `can_activate` reports what the *rows* allow, not what the caller
                              may do: the permissions block is what keeps a guest from being
                              offered a control the endpoint would refuse. */}
                          {permissions.can_edit && version.can_activate && !version.is_active && (
                            <Button
                              data-testid={`files-drawer-version-activate-${version.version_no}`}
                              variant="primary"
                              size="base"
                              className={FILES_FOCUS_RING}
                              onClick={() => void activateVersion(version.version_no)}
                            >
                              Make active
                            </Button>
                          )}
                        </span>
                      </div>
                    </li>
                  ))}
                </ul>
                <p className={META_LABEL_CLASS_NAME}>
                  One version is active, and preview and download follow it. A newly uploaded revision stays inactive
                  until the question is answered, and activating writes an audit event without copying objects.
                </p>
              </section>

              <section data-testid="files-drawer-activity" className="flex flex-col gap-2">
                <h3 className={META_LABEL_CLASS_NAME}>Activity</h3>
                {detail.activity.length === 0 ? (
                  <p className="text-caption-md-regular text-tertiary">No recorded activity.</p>
                ) : (
                  <ul className="flex flex-col gap-1">
                    {detail.activity.map((entry) => {
                      const { actor, action } = fileActivityCopy(entry);
                      return (
                        <li
                          key={entry.id}
                          data-testid={`files-drawer-activity-${entry.id}`}
                          className="flex items-center justify-between gap-2"
                        >
                          <span className={META_VALUE_CLASS_NAME}>
                            {actor} {action}
                          </span>
                          <span className={META_LABEL_CLASS_NAME}>{calculateTimeAgo(entry.created_at)}</span>
                        </li>
                      );
                    })}
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

/**
 * The preview area.
 *
 * Two independent decisions gate the render, and both have to agree: the signed payload
 * has to say `inline` (the server signs SVG, HTML and everything script-capable as
 * `attachment`), and the type has to be a raster image, the only kind an element of this
 * page may be handed. Anything else - SVG, HTML, a PDF, an archive - shows a type tile
 * and a Download action instead (DESIGN §8, AC-08).
 */
function PreviewSurface(props: {
  name: string;
  extension: string;
  mimeType: string;
  signed: IProjectFileAccessUrl | null;
  failure: string | null;
  onRetry: () => void;
}) {
  const { name, extension, mimeType, signed, failure, onRetry } = props;

  if (failure) {
    return (
      <div data-testid="files-drawer-preview-failed" className="flex flex-col items-center gap-2" role="alert">
        <p className="text-caption-md-regular text-danger-primary">{failure}</p>
        <button
          type="button"
          className={cn("rounded text-caption-md-medium text-link-primary", FILES_FOCUS_RING)}
          onClick={onRetry}
        >
          Retry
        </button>
      </div>
    );
  }

  if (!signed) {
    return <p className="text-caption-md-regular text-tertiary">Preparing the preview…</p>;
  }

  // The server signs `inline` only for types it verified as inert, and the drawer renders
  // only raster images; both have to agree before a URL reaches an element.
  const renderInline = signed.disposition === "inline" && isInlineRenderableMime(mimeType);

  if (renderInline) {
    return (
      // The URL is signed for this rendering and is never stored (DESIGN §8).
      <img
        data-testid="files-drawer-preview-image"
        src={signed.url}
        alt={`Preview of ${name} (v${signed.version_no})`}
        className="max-h-64 w-full object-contain"
      />
    );
  }

  return (
    <div data-testid="files-drawer-preview-tile" className="flex flex-col items-center gap-2 px-4 py-6 text-center">
      <span
        aria-hidden="true"
        className="flex h-16 w-14 items-center justify-center rounded-md border border-strong bg-layer-3 text-body-sm-medium tracking-wide text-tertiary uppercase"
      >
        {extension}
      </span>
      <p className="max-w-64 text-caption-md-regular text-tertiary">
        {isAlwaysDownloadMime(mimeType)
          ? "SVG and HTML are always downloads — this type is never rendered inline."
          : "Preview is not available for this type. Download to open it."}
      </p>
    </div>
  );
}
