/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import type { EditorRefApi, CollaborationState } from "@plane/editor";
// plane editor
import { persistPageFallback } from "@/helpers/page-fallback";
// plane types
import type { TDocumentPayload, TPageDescriptionSnapshot } from "@plane/types";
// hooks
import useAutoSave from "@/hooks/use-auto-save";

type TArgs = {
  editorRef: React.RefObject<EditorRefApi | null>;
  fetchPageDescription: () => Promise<TPageDescriptionSnapshot>;
  collaborationState: CollaborationState | null;
  updatePageDescription: (data: TDocumentPayload, etag: string) => Promise<void>;
};

export const usePageFallback = (args: TArgs) => {
  const { editorRef, fetchPageDescription, collaborationState, updatePageDescription } = args;
  const hasShownFallbackToast = useRef(false);
  const isSaving = useRef(false);

  const [isFetchingFallbackBinary, setIsFetchingFallbackBinary] = useState(false);

  // Derive connection failure from collaboration state
  const hasConnectionFailed = collaborationState?.stage.kind === "disconnected";

  const handleUpdateDescription = useCallback(async () => {
    if (!hasConnectionFailed || isSaving.current) return;
    const editor = editorRef.current;
    if (!editor) return;

    // Show toast notification when fallback mechanism kicks in (only once)
    if (!hasShownFallbackToast.current) {
      console.warn("Websocket Connection lost, your changes are being saved using backup mechanism.");
      hasShownFallbackToast.current = true;
    }

    try {
      isSaving.current = true;
      setIsFetchingFallbackBinary(true);

      await persistPageFallback(editor, fetchPageDescription, updatePageDescription);
    } catch (error: any) {
      console.error(error);
    } finally {
      isSaving.current = false;
      setIsFetchingFallbackBinary(false);
    }
  }, [editorRef, fetchPageDescription, hasConnectionFailed, updatePageDescription]);

  useEffect(() => {
    if (hasConnectionFailed) {
      handleUpdateDescription();
    } else {
      // Reset toast flag when connection is restored
      hasShownFallbackToast.current = false;
    }
  }, [handleUpdateDescription, hasConnectionFailed]);

  useAutoSave(handleUpdateDescription);

  return { isFetchingFallbackBinary };
};
