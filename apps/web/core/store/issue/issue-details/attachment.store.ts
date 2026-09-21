/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { uniq, pull, set, debounce, update, concat } from "lodash-es";
import { action, computed, makeObservable, observable, runInAction } from "mobx";
import { computedFn } from "mobx-utils";
import { v4 as uuidv4 } from "uuid";
// types
import type { TIssueAttachment, TIssueAttachmentMap, TIssueAttachmentIdMap, TIssueServiceType } from "@plane/types";
// services
import { IssueAttachmentService } from "@/services/issue";
import type { IProjectFileEntityLink } from "@/services/project-file.service";
import { ProjectFileService, uploadProjectFile } from "@/services/project-file.service";
import type { IIssueRootStore } from "../root.store";
import type { IIssueDetail } from "./root.store";

export type TAttachmentUploadStatus = {
  id: string;
  name: string;
  progress: number;
  size: number;
  type: string;
};

/**
 * How the issue's attachment surface reads a project file.
 *
 * The API answers with the link and the file together, and both are kept as they
 * arrived so the row prints the file's own values rather than a projection of them
 * (the same discipline the Files view follows).
 */
export type TIssueProjectFileAttachment = IProjectFileEntityLink;

export interface IIssueAttachmentStoreActions {
  // actions
  addAttachments: (issueId: string, attachments: TIssueAttachment[]) => void;
  fetchAttachments: (workspaceSlug: string, projectId: string, issueId: string) => Promise<TIssueAttachment[]>;
  createAttachment: (
    workspaceSlug: string,
    projectId: string,
    issueId: string,
    file: File
  ) => Promise<TIssueAttachment>;
  removeAttachment: (
    workspaceSlug: string,
    projectId: string,
    issueId: string,
    attachmentId: string
  ) => Promise<TIssueAttachment>;
  /**
   * T-115: the project files this issue surfaces, and the two operations that change
   * them. An issue attachment is a project file linked to the issue, so it appears
   * once in the project's Files view with the id the issue refers to (AC-16), and
   * removing the attachment unlinks rather than deletes (AC-21).
   */
  fetchProjectFileAttachments: (workspaceSlug: string, projectId: string, issueId: string) => Promise<void>;
  createProjectFileAttachment: (workspaceSlug: string, projectId: string, issueId: string, file: File) => Promise<void>;
  removeProjectFileAttachment: (
    workspaceSlug: string,
    projectId: string,
    issueId: string,
    linkId: string
  ) => Promise<void>;
}

export interface IIssueAttachmentStore extends IIssueAttachmentStoreActions {
  // observables
  attachments: TIssueAttachmentIdMap;
  attachmentMap: TIssueAttachmentMap;
  attachmentsUploadStatusMap: Record<string, Record<string, TAttachmentUploadStatus>>;
  projectFileAttachments: Record<string, string[]>;
  projectFileAttachmentMap: Record<string, TIssueProjectFileAttachment>;
  // computed
  issueAttachments: string[] | undefined;
  // helper methods
  getAttachmentsUploadStatusByIssueId: (issueId: string) => TAttachmentUploadStatus[] | undefined;
  getAttachmentsByIssueId: (issueId: string) => string[] | undefined;
  getAttachmentById: (attachmentId: string) => TIssueAttachment | undefined;
  getAttachmentsCountByIssueId: (issueId: string) => number;
  getProjectFileAttachmentsByIssueId: (issueId: string) => TIssueProjectFileAttachment[] | undefined;
  getProjectFileAttachmentByLinkId: (linkId: string) => TIssueProjectFileAttachment | undefined;
}

export class IssueAttachmentStore implements IIssueAttachmentStore {
  // observables
  attachments: TIssueAttachmentIdMap = {};
  attachmentMap: TIssueAttachmentMap = {};
  attachmentsUploadStatusMap: Record<string, Record<string, TAttachmentUploadStatus>> = {};
  /** Link ids of the project files this issue surfaces, in the API's order. */
  projectFileAttachments: Record<string, string[]> = {};
  projectFileAttachmentMap: Record<string, TIssueProjectFileAttachment> = {};
  // root store
  rootIssueStore: IIssueRootStore;
  rootIssueDetailStore: IIssueDetail;
  // services
  issueAttachmentService;
  projectFileService: ProjectFileService;

  constructor(rootStore: IIssueRootStore, serviceType: TIssueServiceType) {
    makeObservable(this, {
      // observables
      attachments: observable,
      attachmentMap: observable,
      attachmentsUploadStatusMap: observable,
      projectFileAttachments: observable,
      projectFileAttachmentMap: observable,
      // computed
      issueAttachments: computed,
      // actions
      addAttachments: action.bound,
      fetchAttachments: action,
      createAttachment: action,
      removeAttachment: action,
      fetchProjectFileAttachments: action,
      createProjectFileAttachment: action,
      removeProjectFileAttachment: action,
    });
    // root store
    this.rootIssueStore = rootStore;
    this.rootIssueDetailStore = rootStore.issueDetail;
    // services
    this.issueAttachmentService = new IssueAttachmentService(serviceType);
    this.projectFileService = new ProjectFileService();
  }

  // computed
  get issueAttachments() {
    const issueId = this.rootIssueDetailStore.peekIssue?.issueId;
    if (!issueId) return undefined;
    return this.attachments[issueId] ?? undefined;
  }

  // helper methods
  getAttachmentsUploadStatusByIssueId = computedFn((issueId: string) => {
    if (!issueId) return undefined;
    const attachmentsUploadStatus = Object.values(this.attachmentsUploadStatusMap[issueId] ?? {});
    return attachmentsUploadStatus ?? undefined;
  });

  getAttachmentsByIssueId = (issueId: string) => {
    if (!issueId) return undefined;
    return this.attachments[issueId] ?? undefined;
  };

  getAttachmentById = (attachmentId: string) => {
    if (!attachmentId) return undefined;
    return this.attachmentMap[attachmentId] ?? undefined;
  };

  /**
   * How many attachments this work item has, as its own surface renders them.
   *
   * Both kinds count: the legacy file assets the issue payload carries **and** the
   * project files linked to it (AC-16). The section's visibility, its header badge
   * and its row list all read this, so a work item whose only attachment is a
   * project file still shows the section - and with it the unlink action.
   *
   * The number the *server* owns (`issue.attachment_count`) is narrower: it counts
   * the legacy file assets only, so the two writers below recompute that field from
   * the legacy map rather than from this total.
   */
  getAttachmentsCountByIssueId = (issueId: string) => {
    const attachments = this.getAttachmentsByIssueId(issueId);
    return (attachments?.length ?? 0) + (this.getProjectFileAttachmentsByIssueId(issueId)?.length ?? 0);
  };

  getProjectFileAttachmentsByIssueId = (issueId: string) => {
    if (!issueId) return undefined;
    const linkIds = this.projectFileAttachments[issueId];
    if (!linkIds) return undefined;
    return linkIds.map((linkId) => this.projectFileAttachmentMap[linkId]).filter(Boolean);
  };

  getProjectFileAttachmentByLinkId = (linkId: string) => {
    if (!linkId) return undefined;
    return this.projectFileAttachmentMap[linkId] ?? undefined;
  };

  // T-115: the issue's project-file attachments. An attachment is a project file
  // linked to the issue, so it is one row in the project's Files view with the same
  // id, and removing the attachment unlinks rather than deletes (AC-16, AC-21).

  private replaceProjectFileAttachments = (issueId: string, rows: TIssueProjectFileAttachment[]) => {
    runInAction(() => {
      const nextLinkIds = rows.map((row) => row.link.id);
      (this.projectFileAttachments[issueId] ?? []).forEach((linkId) => {
        if (!nextLinkIds.includes(linkId)) delete this.projectFileAttachmentMap[linkId];
      });
      rows.forEach((row) => set(this.projectFileAttachmentMap, row.link.id, row));
      update(this.projectFileAttachments, [issueId], () => nextLinkIds);
    });
  };

  /**
   * The project files this issue surfaces, straight from the API (AC-16).
   *
   * Re-read rather than patched in place: the entity listing is where the link id,
   * the file's own name/size/uploader and the order come from, so the rows on screen
   * are the server's answer rather than this client's reconstruction of it.
   */
  fetchProjectFileAttachments = async (workspaceSlug: string, projectId: string, issueId: string) => {
    const rows = await this.projectFileService.listEntityFileLinks(workspaceSlug, projectId, {
      entity_type: "issue",
      entity_id: issueId,
    });
    this.replaceProjectFileAttachments(issueId, rows);
  };

  /**
   * Upload one file as this issue's attachment (AC-16).
   *
   * The link travels with the initiation, so the file and its issue binding commit
   * together and the file cannot end up attached to nothing. The progress row is the
   * same one the legacy path shows, because the widget renders one upload list.
   */
  createProjectFileAttachment = async (workspaceSlug: string, projectId: string, issueId: string, file: File) => {
    const tempId = uuidv4();
    try {
      runInAction(() => {
        set(this.attachmentsUploadStatusMap, [issueId, tempId], {
          id: tempId,
          name: file.name,
          progress: 0,
          size: file.size,
          type: file.type,
        });
      });
      await uploadProjectFile({
        workspaceSlug,
        projectId,
        file,
        link: { entity_type: "issue", entity_id: issueId },
        onProgress: (percentage) => this.debouncedUpdateProgress(issueId, tempId, percentage),
      });
      await this.fetchProjectFileAttachments(workspaceSlug, projectId, issueId);
    } finally {
      runInAction(() => {
        delete this.attachmentsUploadStatusMap[issueId][tempId];
      });
    }
  };

  /**
   * Detach one project file from this issue (AC-21).
   *
   * The file, its versions and its bytes are untouched: the link goes inactive and
   * the file stays listed, downloadable, and an orphan if nothing else links to it.
   */
  removeProjectFileAttachment = async (workspaceSlug: string, projectId: string, issueId: string, linkId: string) => {
    const attachment = this.getProjectFileAttachmentByLinkId(linkId);
    if (!attachment) return;
    await this.projectFileService.unlinkProjectFile(workspaceSlug, projectId, attachment.file.id, linkId);
    await this.fetchProjectFileAttachments(workspaceSlug, projectId, issueId);
  };

  // actions
  addAttachments = (issueId: string, attachments: TIssueAttachment[]) => {
    if (attachments && attachments.length > 0) {
      const newAttachmentIds = attachments.map((attachment) => attachment.id);
      runInAction(() => {
        update(this.attachments, [issueId], (attachmentIds = []) => uniq(concat(attachmentIds, newAttachmentIds)));
        attachments.forEach((attachment) => set(this.attachmentMap, attachment.id, attachment));
      });
    }
  };

  fetchAttachments = async (workspaceSlug: string, projectId: string, issueId: string) => {
    const response = await this.issueAttachmentService.getIssueAttachments(workspaceSlug, projectId, issueId);
    this.addAttachments(issueId, response);
    return response;
  };

  private debouncedUpdateProgress = debounce((issueId: string, tempId: string, progress: number) => {
    runInAction(() => {
      set(this.attachmentsUploadStatusMap, [issueId, tempId, "progress"], progress);
    });
  }, 16);

  createAttachment = async (workspaceSlug: string, projectId: string, issueId: string, file: File) => {
    const tempId = uuidv4();
    try {
      // update attachment upload status
      runInAction(() => {
        set(this.attachmentsUploadStatusMap, [issueId, tempId], {
          id: tempId,
          name: file.name,
          progress: 0,
          size: file.size,
          type: file.type,
        });
      });
      const response = await this.issueAttachmentService.uploadIssueAttachment(
        workspaceSlug,
        projectId,
        issueId,
        file,
        (progressEvent) => {
          const progressPercentage = Math.round((progressEvent.progress ?? 0) * 100);
          this.debouncedUpdateProgress(issueId, tempId, progressPercentage);
        }
      );

      if (response && response.id) {
        runInAction(() => {
          update(this.attachments, [issueId], (attachmentIds = []) => uniq(concat(attachmentIds, [response.id])));
          set(this.attachmentMap, response.id, response);
          this.rootIssueStore.issues.updateIssue(issueId, {
            // The field mirrors the server's own count, which is the legacy file
            // assets only - not the project files linked to the issue (AC-16).
            attachment_count: this.getAttachmentsByIssueId(issueId)?.length ?? 0,
          });
        });
      }

      return response;
    } catch (error) {
      console.error("Error in uploading issue attachment:", error);
      throw error;
    } finally {
      runInAction(() => {
        delete this.attachmentsUploadStatusMap[issueId][tempId];
      });
    }
  };

  removeAttachment = async (workspaceSlug: string, projectId: string, issueId: string, attachmentId: string) => {
    const response = await this.issueAttachmentService.deleteIssueAttachment(
      workspaceSlug,
      projectId,
      issueId,
      attachmentId
    );

    runInAction(() => {
      update(this.attachments, [issueId], (attachmentIds = []) => {
        if (attachmentIds.includes(attachmentId)) pull(attachmentIds, attachmentId);
        return attachmentIds;
      });
      delete this.attachmentMap[attachmentId];
      this.rootIssueStore.issues.updateIssue(issueId, {
        // The server's counter is the legacy file assets only (see above).
        attachment_count: this.getAttachmentsByIssueId(issueId)?.length ?? 0,
      });
    });

    return response;
  };
}
