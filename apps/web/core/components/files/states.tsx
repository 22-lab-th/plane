/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// plane imports
import { InfoIcon } from "@plane/propel/icons";
import { Button } from "@plane/propel/button";
import { Skeleton } from "@plane/propel/skeleton";
// helpers
import { FILES_FOCUS_RING } from "./helpers";

/** The first paint, while the listing request is still in flight. */
export function FilesLoadingState() {
  return (
    <div data-testid="files-state-loading" className="flex flex-col gap-2 p-4">
      <Skeleton className="flex flex-col gap-2" ariaLabel="Loading files">
        {Array.from({ length: 6 }, (_, index) => (
          <Skeleton.Item key={index} height="20px" width="100%" className="bg-layer-2" />
        ))}
      </Skeleton>
    </div>
  );
}

/** A folder (or a project) that genuinely holds nothing. */
const EMPTY_COPY = {
  folder: { headline: "This folder is empty.", hint: "Files added to this project will appear here." },
  project: {
    headline: "No files yet — drop files here or use Upload.",
    hint: "Files added to this project will appear here.",
  },
} as const;

/** The empty state of a folder, or of a project that has no files at all (DES-001 §7). */
export function FilesEmptyState(props: { variant?: keyof typeof EMPTY_COPY }) {
  const { variant = "folder" } = props;
  return (
    <div
      data-testid="files-state-empty"
      data-variant={variant}
      className="flex h-full min-h-40 flex-col items-center justify-center gap-1 p-8 text-center"
    >
      <p className="text-body-sm-medium text-primary">{EMPTY_COPY[variant].headline}</p>
      <p className="text-caption-md-regular text-tertiary">{EMPTY_COPY[variant].hint}</p>
    </div>
  );
}

type NoMatchProps = {
  onClearFilters: () => void;
};

/** Rows exist somewhere, but not under the filters the caller applied. */
export function FilesNoMatchState(props: NoMatchProps) {
  const { onClearFilters } = props;

  return (
    <div
      data-testid="files-state-no-match"
      className="flex h-full min-h-40 flex-col items-center justify-center gap-2 p-8 text-center"
    >
      <p className="text-body-sm-medium text-primary">No files match your filters.</p>
      <Button
        data-testid="files-clear-filters"
        variant="secondary"
        size="base"
        className={FILES_FOCUS_RING}
        onClick={onClearFilters}
      >
        Clear filters
      </Button>
    </div>
  );
}

type ErrorProps = {
  onRetry: () => void;
};

/** The listing failed and there is nothing to fall back on. */
export function FilesErrorState(props: ErrorProps) {
  const { onRetry } = props;

  return (
    <div
      data-testid="files-state-error"
      className="flex h-full min-h-40 flex-col items-center justify-center gap-2 p-8 text-center"
    >
      <p className="text-body-sm-medium text-primary">We could not load these files.</p>
      <Button data-testid="files-retry" variant="secondary" size="base" className={FILES_FOCUS_RING} onClick={onRetry}>
        Retry
      </Button>
    </div>
  );
}

/**
 * A refresh failed while rows from the previous response are still on screen.
 * The rows stay put; only the notice and the retry sit on top of them.
 */
export function FilesErrorBanner(props: ErrorProps) {
  const { onRetry } = props;

  return (
    <div
      data-testid="files-error-banner"
      role="alert"
      className="flex items-center justify-between gap-3 border-b border-danger-strong bg-danger-subtle px-4 py-2"
    >
      <p className="text-caption-md-regular text-danger-primary">
        We could not refresh this list. Showing the last loaded files.
      </p>
      <Button
        data-testid="files-retry"
        variant="error-outline"
        size="base"
        className={FILES_FOCUS_RING}
        onClick={onRetry}
      >
        Retry
      </Button>
    </div>
  );
}

/** What a GUEST sees instead of any mutation affordance: a plain statement. */
export function FilesReadonlyNotice() {
  return (
    <div
      data-testid="files-readonly-notice"
      role="status"
      className="flex items-center gap-2 border-b border-subtle bg-layer-1 px-4 py-2"
    >
      <InfoIcon className="size-3.5 shrink-0 text-tertiary" aria-hidden="true" />
      <p className="text-caption-md-regular text-tertiary">You have view-only access to this project.</p>
    </div>
  );
}
