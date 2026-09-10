/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { PageHead } from "@/components/core/page-title";
import { WorkspaceBookmarksRoot } from "@/components/workspace/bookmarks/root";

export default function WorkspaceBookmarksPage() {
  return (
    <>
      <PageHead title="Workspace bookmarks" />
      <WorkspaceBookmarksRoot />
    </>
  );
}
