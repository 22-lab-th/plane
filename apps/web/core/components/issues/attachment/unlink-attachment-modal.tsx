/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useState } from "react";
import { observer } from "mobx-react";
// ui
import { AlertModalCore } from "@plane/ui";
// types
import type { TIssueProjectFileAttachment } from "@/store/issue/issue-details/attachment.store";
// types
import type { TAttachmentOperations } from "../issue-detail-widgets/attachments/helper";

export type TAttachmentOperationsUnlinkModal = Pick<TAttachmentOperations, "remove">;

type Props = {
  isOpen: boolean;
  onClose: () => void;
  attachment: TIssueProjectFileAttachment;
  attachmentOperations: TAttachmentOperationsUnlinkModal;
};

/**
 * Detach a project file from a work item (AC-21).
 *
 * Deliberately not the legacy delete modal's copy: an attachment is now a project
 * file linked to this work item, so removing it makes the link inactive and leaves
 * the file, its versions and its bytes in the project's Files view. The wording
 * says that rather than "permanently removed", which is what the legacy file-asset
 * path does and what this path must not do.
 */
export const IssueProjectFileUnlinkModal = observer(function IssueProjectFileUnlinkModal(props: Props) {
  const { isOpen, onClose, attachment, attachmentOperations } = props;
  // states
  const [loader, setLoader] = useState(false);

  // handlers
  const handleClose = () => {
    onClose();
    setLoader(false);
  };

  const handleUnlink = async (linkId: string) => {
    setLoader(true);
    attachmentOperations.remove(linkId).finally(() => handleClose());
  };

  return (
    <AlertModalCore
      handleClose={handleClose}
      handleSubmit={() => handleUnlink(attachment.link.id)}
      isSubmitting={loader}
      isOpen={isOpen}
      variant="primary"
      title="Remove attachment"
      content={
        <>
          Remove <span className="font-bold">{attachment.file.name_display}</span> from this work item? The file stays
          in the project&apos;s Files view, and any other link to it is unaffected.
        </>
      }
    />
  );
});
