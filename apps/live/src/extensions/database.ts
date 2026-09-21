/* eslint-disable no-await-in-loop -- Each CAS retry depends on the preceding conflict and fresh snapshot. */
/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { Database as HocuspocusDatabase } from "@hocuspocus/extension-database";
import * as Y from "yjs";
import { persistMergedDocument } from "./document-persistence";
// plane imports
import {
  getAllDocumentFormatsFromDocumentEditorBinaryData,
  getBinaryDataFromDocumentEditorHTMLString,
} from "@plane/editor";
import type { TDocumentPayload } from "@plane/types";
import { logger } from "@plane/logger";
// lib
import { AppError } from "@/lib/errors";
// services
import { getPageService } from "@/services/page/handler";
// type
import type { FetchPayloadWithContext, StorePayloadWithContext } from "@/types";
import { ForceCloseReason, CloseCode } from "@/types/admin-commands";
import { broadcastError } from "@/utils/broadcast-error";
// force close utility
import { forceCloseDocumentAcrossServers } from "./force-close-handler";

const fetchDocument = async ({ context, documentName: pageId, instance }: FetchPayloadWithContext) => {
  try {
    const service = getPageService(context.documentType, context);
    // A concurrent API replacement must not be overwritten during first conversion.
    for (let attempt = 0; attempt < 3; attempt++) {
      const snapshot = await service.fetchDescriptionSnapshot(pageId);
      const binaryData = new Uint8Array(snapshot.binary);
      if (binaryData.byteLength > 0) return binaryData;
      const pageDetails = await service.fetchDetails(pageId);
      const convertedBinaryData = getBinaryDataFromDocumentEditorHTMLString(
        pageDetails.description_html ?? "<p></p>",
        pageDetails.name
      );
      if (!convertedBinaryData) return binaryData;
      const { contentBinaryEncoded, contentHTML, contentJSON } = getAllDocumentFormatsFromDocumentEditorBinaryData(
        convertedBinaryData,
        true
      );
      const payload: TDocumentPayload = {
        description_binary: contentBinaryEncoded,
        description_html: contentHTML,
        description_json: contentJSON,
      };
      try {
        await service.updateDescriptionBinary(pageId, payload, snapshot.etag);
        return convertedBinaryData;
      } catch (error) {
        if (new AppError(error).statusCode !== 412 || attempt === 2) throw error;
      }
    }
    throw new Error("Page changed repeatedly during initial conversion");
  } catch (error) {
    const appError = new AppError(error, { context: { pageId } });
    logger.error("Error in fetching document", appError);

    // Broadcast error to frontend for user document types
    await broadcastError(instance, pageId, "Unable to load the page. Please try refreshing.", "fetch", context);

    throw appError;
  }
};

const storeDocument = async ({
  context,
  state: pageBinaryData,
  documentName: pageId,
  instance,
}: StorePayloadWithContext) => {
  try {
    const service = getPageService(context.documentType, context);
    const activeDocument = instance.documents.get(pageId);
    const document = activeDocument ?? new Y.Doc();
    try {
      // Use current memory, not the earlier hook snapshot, whenever it is available.
      if (!activeDocument) Y.applyUpdate(document, pageBinaryData);
      await persistMergedDocument(service, pageId, document);
    } finally {
      if (!activeDocument) document.destroy();
    }
  } catch (error) {
    const appError = new AppError(error, { context: { pageId } });
    logger.error("Error in updating document:", appError);

    // Check error types
    const isContentTooLarge = appError.statusCode === 413;

    // Determine if we should disconnect and unload
    const shouldDisconnect = isContentTooLarge;

    // Determine error message and code
    let errorMessage: string;
    let errorCode: "content_too_large" | "page_locked" | "page_archived" | undefined;

    if (isContentTooLarge) {
      errorMessage = "Document is too large to save. Please reduce the content size.";
      errorCode = "content_too_large";
    } else {
      errorMessage = "Unable to save the page. Please try again.";
    }

    // Broadcast error to frontend for user document types
    await broadcastError(instance, pageId, errorMessage, "store", context, errorCode, shouldDisconnect);

    // If we should disconnect, close connections and unload document
    if (shouldDisconnect) {
      // Map error code to ForceCloseReason with proper types
      const reason =
        errorCode === "content_too_large" ? ForceCloseReason.DOCUMENT_TOO_LARGE : ForceCloseReason.CRITICAL_ERROR;

      const closeCode = errorCode === "content_too_large" ? CloseCode.DOCUMENT_TOO_LARGE : CloseCode.FORCE_CLOSE;

      // force close connections and unload document
      await forceCloseDocumentAcrossServers(instance, pageId, reason, closeCode);

      // Don't throw after force close - document is already unloaded
      // Throwing would cause hocuspocus's finally block to access the null document
      return;
    }

    throw appError;
  }
};

export class Database extends HocuspocusDatabase {
  constructor() {
    super({ fetch: fetchDocument, store: storeDocument });
  }
}
