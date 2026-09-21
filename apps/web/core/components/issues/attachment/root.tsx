/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useEffect } from "react";
import { observer } from "mobx-react";
// hooks
import { useIssueDetail } from "@/hooks/store/use-issue-detail";
import { useAttachmentOperations } from "../issue-detail-widgets/attachments/helper";
// components
import { IssueAttachmentUpload } from "./attachment-upload";
import { IssueAttachmentsList } from "./attachments-list";

export type TIssueAttachmentRoot = {
  workspaceSlug: string;
  projectId: string;
  issueId: string;
  disabled?: boolean;
};

export const IssueAttachmentRoot = observer(function IssueAttachmentRoot(props: TIssueAttachmentRoot) {
  // props
  const { workspaceSlug, projectId, issueId, disabled = false } = props;
  // store hooks
  const {
    attachment: { fetchProjectFileAttachments },
  } = useIssueDetail();
  // hooks
  const attachmentHelpers = useAttachmentOperations(workspaceSlug, projectId, issueId);

  // This surface always renders its attachments, and the work item's project files are
  // not in the issue payload (only the legacy assets are), so they are read here:
  // otherwise the cards would render nothing while the file is really attached
  // (AC-16).
  useEffect(() => {
    if (!workspaceSlug || !projectId || !issueId) return;
    fetchProjectFileAttachments(workspaceSlug, projectId, issueId).catch(() => undefined);
  }, [workspaceSlug, projectId, issueId, fetchProjectFileAttachments]);

  return (
    <div className="relative space-y-3">
      <h3 className="text-body-sm-medium">Attachments</h3>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4">
        <IssueAttachmentUpload
          workspaceSlug={workspaceSlug}
          disabled={disabled}
          attachmentOperations={attachmentHelpers.operations}
        />
        <IssueAttachmentsList issueId={issueId} disabled={disabled} attachmentHelpers={attachmentHelpers} />
      </div>
    </div>
  );
});
