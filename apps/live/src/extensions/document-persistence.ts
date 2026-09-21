/* eslint-disable no-await-in-loop -- Each CAS retry depends on the preceding conflict and fresh snapshot. */
/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 */

import * as Y from "yjs";
import { getAllDocumentFormatsFromDocumentEditorBinaryData } from "@plane/editor";
import { AppError } from "@/lib/errors";
import type { PageCoreService } from "@/services/page/core.service";

/** Merge API replacements (including deletion tombstones) before every conditional save. */
export async function persistMergedDocument(
  service: Pick<PageCoreService, "fetchDescriptionSnapshot" | "updateDescriptionBinary">,
  pageId: string,
  document: Y.Doc
): Promise<void> {
  for (let attempt = 0; attempt < 3; attempt++) {
    const snapshot = await service.fetchDescriptionSnapshot(pageId);
    if (snapshot.binary.byteLength > 0) {
      // Applying an already-known update is a no-op; new state is broadcast to connected editors.
      Y.applyUpdate(document, snapshot.binary, "database-reconciliation");
    }
    const { contentBinaryEncoded, contentHTML, contentJSON } = getAllDocumentFormatsFromDocumentEditorBinaryData(
      Y.encodeStateAsUpdate(document),
      true
    );
    try {
      await service.updateDescriptionBinary(
        pageId,
        {
          description_binary: contentBinaryEncoded,
          description_html: contentHTML,
          description_json: contentJSON,
        },
        snapshot.etag
      );
      return;
    } catch (error) {
      // Never put old state under a newly fetched token: each retry must merge and re-encode.
      if (new AppError(error).statusCode !== 412 || attempt === 2) throw error;
    }
  }
}
