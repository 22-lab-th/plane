/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import React, { useMemo } from "react";
import { observer } from "mobx-react";
import { useTranslation } from "@plane/i18n";
import type { TIssueServiceType } from "@plane/types";
import { EIssueServiceType } from "@plane/types";
import { CollapsibleButton } from "@plane/ui";
// hooks
import { useIssueDetail } from "@/hooks/store/use-issue-detail";
// local imports
import { IssueAttachmentActionButton } from "./quick-action-button";

type Props = {
  isOpen: boolean;
  workspaceSlug: string;
  projectId: string;
  issueId: string;
  disabled: boolean;
  issueServiceType?: TIssueServiceType;
};

export const IssueAttachmentsCollapsibleTitle = observer(function IssueAttachmentsCollapsibleTitle(props: Props) {
  const { isOpen, workspaceSlug, projectId, issueId, disabled, issueServiceType = EIssueServiceType.ISSUES } = props;
  const { t } = useTranslation();
  // store hooks
  const {
    issue: { getIssueById },
    attachment: { getProjectFileAttachmentsByIssueId },
  } = useIssueDetail(issueServiceType);

  // derived values
  const issue = getIssueById(issueId);
  // `attachment_count` is the server's count of the legacy file assets the issue
  // carries; the project files linked to it (AC-16) are counted from the rows the
  // widget itself renders, so the badge and the list cannot disagree.
  const attachmentCount = (issue?.attachment_count ?? 0) + (getProjectFileAttachmentsByIssueId(issueId)?.length ?? 0);

  // indicator element
  const indicatorElement = useMemo(
    () => (
      <span className="flex items-center justify-center">
        <p className="text-14 !leading-3 text-tertiary">{attachmentCount}</p>
      </span>
    ),
    [attachmentCount]
  );

  return (
    <CollapsibleButton
      isOpen={isOpen}
      title={t("common.attachments")}
      indicatorElement={indicatorElement}
      actionItemElement={
        !disabled && (
          <IssueAttachmentActionButton
            workspaceSlug={workspaceSlug}
            projectId={projectId}
            issueId={issueId}
            disabled={disabled}
            issueServiceType={issueServiceType}
          />
        )
      }
    />
  );
});
