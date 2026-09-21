/* eslint-disable no-await-in-loop -- Each conditional retry requires a new snapshot. */
import type { EditorRefApi } from "@plane/editor";
import {
  convertBinaryDataToBase64String,
  getBinaryDataFromDocumentEditorHTMLString,
  getAllDocumentFormatsFromDocumentEditorBinaryData,
} from "@plane/editor";
import type { TDocumentPayload, TPageDescriptionSnapshot } from "@plane/types";

export async function persistPageFallback(
  editor: Pick<EditorRefApi, "setProviderDocument" | "getDocument">,
  fetchSnapshot: () => Promise<TPageDescriptionSnapshot>,
  save: (payload: TDocumentPayload, etag: string) => Promise<void>
): Promise<void> {
  for (let attempt = 0; attempt < 3; attempt++) {
    const snapshot = await fetchSnapshot();
    try {
      if (snapshot.binary.byteLength === 0) {
        if (!snapshot.initialContent) throw new Error("Missing initial page content");
        const initial = getBinaryDataFromDocumentEditorHTMLString(
          snapshot.initialContent.description_html,
          snapshot.initialContent.name
        );
        const formats = getAllDocumentFormatsFromDocumentEditorBinaryData(initial, true);
        // Do not pollute the live document with speculative new Yjs IDs before CAS succeeds.
        await save(
          {
            description_binary: formats.contentBinaryEncoded,
            description_html: formats.contentHTML,
            description_json: formats.contentJSON,
          },
          snapshot.etag
        );
        continue; // Fetch the committed initialization and its new revision before merging local edits.
      }
      editor.setProviderDocument(new Uint8Array(snapshot.binary));
      const { binary, html, json } = editor.getDocument();
      if (!binary || !json) throw new Error("Editor document is not ready to save");
      await save(
        { description_binary: convertBinaryDataToBase64String(binary), description_html: html, description_json: json },
        snapshot.etag
      );
      return;
    } catch (error) {
      const status = (error as { response?: { status?: number } })?.response?.status;
      if (status !== 412 || attempt === 2) throw error;
    }
  }
  throw new Error("Page changed repeatedly; local edits remain pending");
}
