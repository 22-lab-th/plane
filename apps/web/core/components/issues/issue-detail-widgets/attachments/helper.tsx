/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useMemo } from "react";
import { setPromiseToast, TOAST_TYPE, setToast } from "@plane/propel/toast";
import type { TIssueServiceType } from "@plane/types";
import { EIssueServiceType } from "@plane/types";
// hooks
import { useIssueDetail } from "@/hooks/store/use-issue-detail";
// types
import type {
  TAttachmentUploadStatus,
  TIssueProjectFileAttachment,
} from "@/store/issue/issue-details/attachment.store";

export type TAttachmentOperations = {
  create: (file: File) => Promise<void>;
  remove: (attachmentId: string) => Promise<void>;
};

export type TAttachmentSnapshot = {
  uploadStatus: TAttachmentUploadStatus[] | undefined;
  /**
   * The work item's project files (AC-16). Empty for an epic, whose attachments
   * have no entity type in the file-link vocabulary and stay on the legacy path.
   */
  projectFileAttachments: TIssueProjectFileAttachment[] | undefined;
  /** The scope a project file's download or preview URL is signed for. */
  workspaceSlug: string;
  projectId: string;
};

export type TAttachmentHelpers = {
  operations: TAttachmentOperations;
  snapshot: TAttachmentSnapshot;
};

export const useAttachmentOperations = (
  workspaceSlug: string,
  projectId: string,
  issueId: string,
  issueServiceType: TIssueServiceType = EIssueServiceType.ISSUES
): TAttachmentHelpers => {
  const {
    attachment: {
      createAttachment,
      removeAttachment,
      getAttachmentsUploadStatusByIssueId,
      createProjectFileAttachment,
      removeProjectFileAttachment,
      getProjectFileAttachmentByLinkId,
      getProjectFileAttachmentsByIssueId,
    },
  } = useIssueDetail(issueServiceType);

  const supportsProjectFiles = issueServiceType === EIssueServiceType.ISSUES;

  const attachmentOperations: TAttachmentOperations = useMemo(
    () => ({
      create: async (file) => {
        if (!workspaceSlug || !projectId || !issueId) throw new Error("Missing required fields");
        const attachmentUploadPromise: Promise<void> = supportsProjectFiles
          ? createProjectFileAttachment(workspaceSlug, projectId, issueId, file)
          : createAttachment(workspaceSlug, projectId, issueId, file).then(() => undefined);
        setPromiseToast(attachmentUploadPromise, {
          loading: "Uploading attachment...",
          success: {
            title: "Attachment uploaded",
            message: () => "The attachment has been successfully uploaded",
          },
          error: {
            title: "Attachment not uploaded",
            message: () => "The attachment could not be uploaded",
          },
        });

        await attachmentUploadPromise;
      },
      remove: async (attachmentId) => {
        try {
          if (!workspaceSlug || !projectId || !issueId) throw new Error("Missing required fields");
          // One entry point for both kinds, decided by which store actually holds the
          // id: a project file is *unlinked* so the file survives (AC-21), while a
          // legacy file asset keeps the delete it has always had.
          if (getProjectFileAttachmentByLinkId(attachmentId)) {
            await removeProjectFileAttachment(workspaceSlug, projectId, issueId, attachmentId);
            setToast({
              message: "The file stays in the project's Files view, linked to nothing else.",
              type: TOAST_TYPE.SUCCESS,
              title: "Attachment removed",
            });
            return;
          }
          await removeAttachment(workspaceSlug, projectId, issueId, attachmentId);
          setToast({
            message: "The attachment has been successfully removed",
            type: TOAST_TYPE.SUCCESS,
            title: "Attachment removed",
          });
        } catch {
          setToast({
            message: "The Attachment could not be removed",
            type: TOAST_TYPE.ERROR,
            title: "Attachment not removed",
          });
        }
      },
    }),
    [
      workspaceSlug,
      projectId,
      issueId,
      supportsProjectFiles,
      createAttachment,
      createProjectFileAttachment,
      removeAttachment,
      removeProjectFileAttachment,
      getProjectFileAttachmentByLinkId,
    ]
  );
  const attachmentsUploadStatus = getAttachmentsUploadStatusByIssueId(issueId);

  return {
    operations: attachmentOperations,
    snapshot: {
      uploadStatus: attachmentsUploadStatus,
      projectFileAttachments: supportsProjectFiles ? getProjectFileAttachmentsByIssueId(issueId) : undefined,
      workspaceSlug,
      projectId,
    },
  };
};
