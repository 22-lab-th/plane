/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useState } from "react";
import { observer } from "mobx-react";
// plane imports
import { useTranslation } from "@plane/i18n";
import { TrashIcon } from "@plane/propel/icons";
import { setToast, TOAST_TYPE } from "@plane/propel/toast";
import { Tooltip } from "@plane/propel/tooltip";
import { CustomMenu } from "@plane/ui";
import { convertBytesToSize, getFileName, renderFormattedDate } from "@plane/utils";
// components
import { ButtonAvatars } from "@/components/dropdowns/member/avatar";
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
 * One project-file attachment on a work item (AC-16).
 *
 * The values are the file row's own - the same id, name, size and uploader the
 * project's Files view shows - because the row is that file, not a copy of it. The
 * download is signed on click rather than carried in the markup: the URL expires,
 * and the file may be opened long after the row was rendered (DESIGN §8).
 */
export const IssueProjectFileAttachmentRow = observer(function IssueProjectFileAttachmentRow(props: Props) {
  const { workspaceSlug, projectId, attachment, attachmentHelpers, disabled = false } = props;
  const { t } = useTranslation();
  // store hooks
  const { getUserDetails } = useMember();
  // state
  const [isUnlinkModalOpen, setIsUnlinkModalOpen] = useState(false);
  // derived values
  const file = attachment.file;
  const fileExtension = file.extension;
  const fileIcon = getFileIcon(fileExtension, 18);
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
        className="group flex h-11 items-center justify-between gap-3 pr-2 pl-9 hover:bg-surface-2"
        data-testid={`issue-attachment-${file.id}`}
      >
        <button type="button" className="flex min-w-0 flex-1" onClick={() => void handleDownload()}>
          <div className="flex min-w-0 items-center gap-3 truncate text-13">
            <div className="flex items-center gap-3">{fileIcon}</div>
            <Tooltip tooltipContent={`${getFileName(file.name_display)}.${fileExtension}`} isMobile={isMobile}>
              <p className="truncate font-medium text-secondary">{`${getFileName(file.name_display)}.${fileExtension}`}</p>
            </Tooltip>
            <span className="flex size-1.5 rounded-full bg-layer-1" />
            <span className="flex-shrink-0 text-placeholder">{convertBytesToSize(file.size_bytes)}</span>
          </div>
        </button>

        <div className="flex items-center gap-3">
          {uploader && (
            <Tooltip
              isMobile={isMobile}
              tooltipContent={`${getUserDetails(uploader)?.display_name ?? ""} uploaded on ${renderFormattedDate(
                file.created_at
              )}`}
            >
              <div className="flex items-center justify-center">
                <ButtonAvatars showTooltip userIds={uploader} />
              </div>
            </Tooltip>
          )}

          <CustomMenu
            ellipsis
            closeOnSelect
            placement="bottom-end"
            disabled={disabled}
            ariaLabel={`Actions for ${file.name_display}`}
          >
            <CustomMenu.MenuItem onClick={() => setIsUnlinkModalOpen(true)}>
              <div className="flex items-center gap-2">
                <TrashIcon className="h-3.5 w-3.5" strokeWidth={2} />
                <span>{t("common.actions.delete")}</span>
              </div>
            </CustomMenu.MenuItem>
          </CustomMenu>
        </div>
      </div>
    </>
  );
});
