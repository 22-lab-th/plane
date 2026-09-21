/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useState } from "react";
import { observer } from "mobx-react";
import { AlertCircle } from "lucide-react";
// plane imports
import { CloseIcon } from "@plane/propel/icons";
import { setToast, TOAST_TYPE } from "@plane/propel/toast";
import { Tooltip } from "@plane/propel/tooltip";
import { convertBytesToSize, getFileName, renderFormattedDate, truncateText } from "@plane/utils";
// components
import { getFileIcon } from "@/components/icons";
// hooks
import { useMember } from "@/hooks/store/use-member";
import { usePlatformOS } from "@/hooks/use-platform-os";
// services
import { ProjectFileService } from "@/services/project-file.service";
// types
import type { TIssueProjectFileAttachment } from "@/store/issue/issue-details/attachment.store";
// local imports
import { IssueProjectFileUnlinkModal } from "./unlink-attachment-modal";
import type { TAttachmentHelpers } from "../issue-detail-widgets/attachments/helper";

const projectFileService = new ProjectFileService();

type Props = {
  workspaceSlug: string;
  projectId: string;
  attachment: TIssueProjectFileAttachment;
  attachmentHelpers: TAttachmentHelpers;
  disabled?: boolean;
};

/**
 * The inbox surface's card for one project-file attachment (AC-16).
 *
 * Same file, same values and the same unlink semantics as the work-item widget's
 * row; the two surfaces differ only in how they lay a file out.
 */
export const IssueProjectFileAttachmentCard = observer(function IssueProjectFileAttachmentCard(props: Props) {
  const { workspaceSlug, projectId, attachment, attachmentHelpers, disabled = false } = props;
  // store hooks
  const { getUserDetails } = useMember();
  // state
  const [isUnlinkModalOpen, setIsUnlinkModalOpen] = useState(false);
  // derived values
  const file = attachment.file;
  const fileName = getFileName(file.name_display);
  const fileIcon = getFileIcon(file.extension, 28);
  const uploader = file.uploader?.id;
  // hooks
  const { isMobile } = usePlatformOS();

  const handleDownload = async () => {
    try {
      const { url } = await projectFileService.getProjectFileDownloadUrl(workspaceSlug, projectId, file.id);
      window.open(url, "_blank");
    } catch {
      setToast({
        type: TOAST_TYPE.ERROR,
        title: "Attachment not downloaded",
        message: "The file could not be signed for download. Try again.",
      });
    }
  };

  return (
    <>
      {isUnlinkModalOpen && (
        <IssueProjectFileUnlinkModal
          isOpen={isUnlinkModalOpen}
          onClose={() => setIsUnlinkModalOpen(false)}
          attachment={attachment}
          attachmentOperations={attachmentHelpers.operations}
        />
      )}
      <div
        className="flex h-[60px] items-center justify-between gap-1 rounded-md border-[2px] border-subtle bg-surface-1 px-4 py-2 text-13"
        data-testid={`issue-attachment-${file.id}`}
      >
        <button type="button" className="min-w-0" onClick={() => void handleDownload()}>
          <div className="flex items-center gap-3">
            <div className="h-7 w-7">{fileIcon}</div>
            <div className="flex flex-col gap-1">
              <div className="flex items-center gap-2">
                <Tooltip tooltipContent={fileName} isMobile={isMobile}>
                  <span className="text-13">{truncateText(`${fileName}`, 10)}</span>
                </Tooltip>
                {uploader && (
                  <Tooltip
                    isMobile={isMobile}
                    tooltipContent={`${getUserDetails(uploader)?.display_name ?? ""} uploaded on ${renderFormattedDate(
                      file.created_at
                    )}`}
                  >
                    <span>
                      <AlertCircle className="h-3 w-3" />
                    </span>
                  </Tooltip>
                )}
              </div>

              <div className="flex items-center gap-3 text-11 text-secondary">
                <span>{file.extension.toUpperCase()}</span>
                <span>{convertBytesToSize(file.size_bytes)}</span>
              </div>
            </div>
          </div>
        </button>

        {!disabled && (
          <button
            type="button"
            aria-label={`Remove attachment ${file.name_display}`}
            onClick={() => setIsUnlinkModalOpen(true)}
          >
            <CloseIcon className="h-4 w-4 text-secondary hover:text-primary" />
          </button>
        )}
      </div>
    </>
  );
});
