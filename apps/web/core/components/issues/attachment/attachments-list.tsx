/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { observer } from "mobx-react";
// hooks
import { useIssueDetail } from "@/hooks/store/use-issue-detail";
// types
import type { TAttachmentHelpers } from "../issue-detail-widgets/attachments/helper";
// components
import { IssueAttachmentsDetail } from "./attachment-detail";
import { IssueAttachmentsUploadDetails } from "./attachment-upload-details";
import { IssueProjectFileAttachmentCard } from "./project-file-attachment-card";

type TIssueAttachmentsList = {
  issueId: string;
  attachmentHelpers: TAttachmentHelpers;
  disabled?: boolean;
};

export const IssueAttachmentsList = observer(function IssueAttachmentsList(props: TIssueAttachmentsList) {
  const { issueId, attachmentHelpers, disabled } = props;
  // store hooks
  const {
    attachment: { getAttachmentsByIssueId },
  } = useIssueDetail();
  // derived values
  const { snapshot: attachmentSnapshot } = attachmentHelpers;
  const { uploadStatus, projectFileAttachments, workspaceSlug, projectId } = attachmentSnapshot;
  const issueAttachments = getAttachmentsByIssueId(issueId);

  return (
    <>
      {uploadStatus?.map((status) => (
        <IssueAttachmentsUploadDetails key={status.id} uploadStatus={status} />
      ))}
      {issueAttachments?.map((attachmentId) => (
        <IssueAttachmentsDetail
          key={attachmentId}
          attachmentId={attachmentId}
          disabled={disabled}
          attachmentHelpers={attachmentHelpers}
        />
      ))}
      {projectFileAttachments?.map((attachment) => (
        <IssueProjectFileAttachmentCard
          key={attachment.link.id}
          workspaceSlug={workspaceSlug}
          projectId={projectId}
          attachment={attachment}
          attachmentHelpers={attachmentHelpers}
          disabled={disabled}
        />
      ))}
    </>
  );
});
