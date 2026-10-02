/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { copyUrlToClipboard } from "@plane/utils";

type TFilesDeepLinkTarget = {
  workspaceSlug: string;
  projectId: string;
  folderId?: string | null;
  fileId?: string | null;
};

/**
 * Build an authenticated Files route link. The route rechecks login and project
 * permissions when the link is opened, so this never exposes a storage URL.
 */
export const buildFilesDeepLink = ({ workspaceSlug, projectId, folderId, fileId }: TFilesDeepLinkTarget): string => {
  const params = new URLSearchParams();
  if (folderId) params.set("folder", folderId);
  if (fileId) params.set("file", fileId);

  const query = params.toString();
  return `/${encodeURIComponent(workspaceSlug)}/projects/${encodeURIComponent(projectId)}/files/${query ? `?${query}` : ""}`;
};

/** Copy a canonical Files route link using Plane's existing clipboard fallback. */
export const copyFilesDeepLink = async (target: TFilesDeepLinkTarget): Promise<void> => {
  await copyUrlToClipboard(buildFilesDeepLink(target));
};
