/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { Outlet } from "react-router";
// components
import { AppHeader } from "@/components/core/app-header";
import { ContentWrapper } from "@/components/core/content-wrapper";
import { ProjectFilesHeader } from "@/components/files";
import type { Route } from "./+types/layout";

export default function ProjectFilesLayout({ params }: Route.ComponentProps) {
  const { workspaceSlug, projectId } = params;

  return (
    <>
      <AppHeader header={<ProjectFilesHeader workspaceSlug={workspaceSlug} projectId={projectId} />} />
      <ContentWrapper>
        <Outlet />
      </ContentWrapper>
    </>
  );
}
