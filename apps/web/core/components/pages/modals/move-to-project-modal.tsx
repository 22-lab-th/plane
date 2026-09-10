/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useState } from "react";
import { observer } from "mobx-react";
import { useParams } from "next/navigation";
// plane ui
import { Button } from "@plane/propel/button";
import { TOAST_TYPE, setToast } from "@plane/propel/toast";
import { EModalPosition, EModalWidth, ModalCore } from "@plane/ui";
import { getPageName } from "@plane/utils";
// components
import { ProjectDropdown } from "@/components/dropdowns/project/dropdown";
// hooks
import { useAppRouter } from "@/hooks/use-app-router";
import { EPageStoreType, usePageStore } from "@/hooks/store";
import { useProject } from "@/hooks/store/use-project";
import { useUser } from "@/hooks/store/user";
// store types
import type { TPageInstance } from "@/store/pages/base-page";

type Props = {
  isOpen: boolean;
  onClose: () => void;
  page: TPageInstance;
};

export const MoveToProjectModal = observer(function MoveToProjectModal(props: Props) {
  const { isOpen, onClose, page } = props;
  const [targetProjectId, setTargetProjectId] = useState<string | null>(null);
  const [isMoving, setIsMoving] = useState(false);
  const router = useAppRouter();
  const { workspaceSlug, projectId, pageId: routePageId } = useParams();
  const { movePage } = usePageStore(EPageStoreType.PROJECT);
  const { getProjectById } = useProject();
  const { projectsWithCreatePermissions } = useUser();

  const handleClose = () => {
    if (isMoving) return;
    setTargetProjectId(null);
    onClose();
  };

  const handleMove = async () => {
    const sourceProjectId = projectId?.toString();
    const slug = workspaceSlug?.toString();
    if (!page.id || !sourceProjectId || !slug || !targetProjectId || isMoving) return;

    setIsMoving(true);
    try {
      await movePage(slug, sourceProjectId, page.id, targetProjectId);
      const targetProjectName = getProjectById(targetProjectId)?.name ?? "the selected project";
      setToast({
        type: TOAST_TYPE.SUCCESS,
        title: "Page moved",
        message: `${getPageName(page.name)} was moved to ${targetProjectName}.`,
      });
      setTargetProjectId(null);
      onClose();

      if (routePageId?.toString() === page.id) {
        router.push(`/${slug}/projects/${targetProjectId}/pages/${page.id}`);
      }
    } catch (_error) {
      setToast({
        type: TOAST_TYPE.ERROR,
        title: "Could not move page",
        message: "Check your access to the target project and try again.",
      });
    } finally {
      setIsMoving(false);
    }
  };

  return (
    <ModalCore isOpen={isOpen} handleClose={handleClose} position={EModalPosition.CENTER} width={EModalWidth.SM}>
      <div className="space-y-5 p-5">
        <div>
          <h3 className="text-18 font-medium text-secondary">Move page to another project</h3>
          <p className="mt-1 text-13 text-tertiary">
            The document, images, and version history will move with it. The page will be placed at the root of the
            target project.
          </p>
        </div>

        <div className="space-y-2">
          <div className="text-13 font-medium text-secondary">Target project</div>
          <div className="h-9">
            <ProjectDropdown
              value={targetProjectId}
              onChange={setTargetProjectId}
              multiple={false}
              currentProjectId={projectId?.toString()}
              renderCondition={(candidateProjectId) => !!projectsWithCreatePermissions?.[candidateProjectId]}
              buttonVariant="border-with-text"
              buttonContainerClassName="w-full"
              buttonClassName="w-full justify-between"
              dropdownArrow
              placeholder="Select a project"
              disabled={isMoving}
            />
          </div>
        </div>

        <div className="flex justify-end gap-2">
          <Button variant="secondary" size="lg" onClick={handleClose} disabled={isMoving}>
            Cancel
          </Button>
          <Button
            variant="primary"
            size="lg"
            onClick={() => void handleMove()}
            loading={isMoving}
            disabled={!targetProjectId}
          >
            Move page
          </Button>
        </div>
      </div>
    </ModalCore>
  );
});
