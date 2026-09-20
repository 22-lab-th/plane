/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// plane imports
import { observer } from "mobx-react";
import { FavoriteFolderIcon } from "@plane/propel/icons";
import { Breadcrumbs, Header } from "@plane/ui";
// components
import { BreadcrumbLink } from "@/components/common/breadcrumb-link";
// plane web imports
import { CommonProjectBreadcrumbs } from "@/components/breadcrumbs/common";
// hooks
import { useProject } from "@/hooks/store/use-project";

type Props = {
  workspaceSlug: string;
  projectId: string;
};

/** The application header for the Files tab: the project trail plus the current tab. */
export const ProjectFilesHeader = observer(function ProjectFilesHeader(props: Props) {
  const { workspaceSlug, projectId } = props;
  const { loader } = useProject();

  return (
    <Header>
      <Header.LeftItem>
        <div>
          <Breadcrumbs isLoading={loader === "init-loader"}>
            <CommonProjectBreadcrumbs workspaceSlug={workspaceSlug} projectId={projectId} />
            <Breadcrumbs.Item
              component={
                <BreadcrumbLink
                  label="Files"
                  href={`/${workspaceSlug}/projects/${projectId}/files/`}
                  icon={<FavoriteFolderIcon className="size-4 text-tertiary" color="currentColor" />}
                  isLast
                />
              }
              isLast
            />
          </Breadcrumbs>
        </div>
      </Header.LeftItem>
    </Header>
  );
});
