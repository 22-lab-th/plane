/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { Fragment } from "react";
// plane imports
import { ChevronRightIcon } from "@plane/propel/icons";
import { cn } from "@plane/utils";
import type { IProjectFileBreadcrumb } from "@/services/project-file.service";
// helpers
import { FILES_FOCUS_RING } from "./helpers";

type Props = {
  breadcrumbs: IProjectFileBreadcrumb[];
  onNavigate: (folderId: string | null) => void;
};

/**
 * The path from the project root down to the browsed folder, exactly as the
 * listing response reports it (an empty trail means the root).
 */
export function FilesBreadcrumbs(props: Props) {
  const { breadcrumbs, onNavigate } = props;

  const crumbClassName = cn(
    "rounded px-1.5 py-0.5 text-body-xs-regular text-secondary hover:bg-layer-2 hover:text-primary",
    FILES_FOCUS_RING
  );

  return (
    <nav
      data-testid="files-breadcrumbs"
      aria-label="Folder path"
      className="flex flex-wrap items-center gap-0.5 border-b border-subtle px-4 py-2"
    >
      <button
        type="button"
        data-testid="files-breadcrumb-root"
        className={crumbClassName}
        onClick={() => onNavigate(null)}
      >
        Files
      </button>
      {breadcrumbs.map((breadcrumb) => (
        <Fragment key={breadcrumb.id}>
          <ChevronRightIcon className="size-3 shrink-0 text-tertiary" aria-hidden="true" />
          <button
            type="button"
            data-testid={`files-breadcrumb-${breadcrumb.id}`}
            aria-current={breadcrumb.id === breadcrumbs[breadcrumbs.length - 1]?.id ? "location" : undefined}
            className={crumbClassName}
            onClick={() => onNavigate(breadcrumb.id)}
          >
            {breadcrumb.name}
          </button>
        </Fragment>
      ))}
    </nav>
  );
}
