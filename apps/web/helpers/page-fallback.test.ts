import { describe, expect, it, vi } from "vitest";
import {
  applyUpdates,
  getAllDocumentFormatsFromDocumentEditorBinaryData,
  getBinaryDataFromDocumentEditorHTMLString,
} from "@plane/editor";
import { persistPageFallback } from "./page-fallback";

function editorFor(initial?: Uint8Array) {
  let state = initial ?? new Uint8Array([0, 0]);
  return {
    setProviderDocument: vi.fn((binary: Uint8Array) => {
      state = applyUpdates(state, binary);
    }),
    getDocument: () => {
      const binary = state;
      const formats = getAllDocumentFormatsFromDocumentEditorBinaryData(binary, true);
      return { binary, html: formats.contentHTML, json: formats.contentJSON };
    },
  };
}

describe("fallback conditional persistence", () => {
  it("never merges rejected HTML initialization into the editor", async () => {
    const editor = editorFor();
    const current = getBinaryDataFromDocumentEditorHTMLString("<p>replacement</p>", "title");
    const fetchSnapshot = vi
      .fn()
      .mockResolvedValueOnce({
        binary: new ArrayBuffer(0),
        etag: '"old"',
        initialContent: { name: "title", description_html: "<p>obsolete</p>" },
      })
      .mockResolvedValueOnce({ binary: current.buffer, etag: '"new"' });
    const save = vi
      .fn()
      .mockRejectedValueOnce({ response: { status: 412 } })
      .mockResolvedValueOnce(undefined);
    await persistPageFallback(editor, fetchSnapshot, save);
    expect(editor.setProviderDocument).toHaveBeenCalledTimes(1);
    expect(save.mock.calls[1][1]).toBe('"new"');
    expect(save.mock.calls[1][0].description_html).toContain("replacement");
    expect(save.mock.calls[1][0].description_html).not.toContain("obsolete");
  });
  it("commits initialization before merging it, then saves under the new revision", async () => {
    const editor = editorFor();
    let stored = new ArrayBuffer(0);
    const fetchSnapshot = vi.fn().mockImplementation(async () => ({
      binary: stored,
      etag: stored.byteLength ? '"initialized"' : '"empty"',
      initialContent: { name: "title", description_html: "<p>initial</p>" },
    }));
    const save = vi.fn().mockImplementation(async (payload) => {
      if (!stored.byteLength) expect(editor.setProviderDocument).not.toHaveBeenCalled();
      stored = Uint8Array.from(Buffer.from(payload.description_binary, "base64")).buffer;
    });
    await persistPageFallback(editor, fetchSnapshot, save);
    expect(save).toHaveBeenCalledTimes(2);
    expect(save.mock.calls[1][1]).toBe('"initialized"');
    expect(editor.getDocument().html.match(/initial/g)).toHaveLength(1);
  });
  it("surfaces conflicts without discarding local state", async () => {
    const current = getBinaryDataFromDocumentEditorHTMLString("<p>pending text</p>");

    const editor = editorFor(current);
    const fetchSnapshot = vi.fn().mockResolvedValue({ binary: current.buffer, etag: '"revision"' });
    const save = vi.fn().mockRejectedValue({ response: { status: 412 } });
    await expect(persistPageFallback(editor, fetchSnapshot, save)).rejects.toMatchObject({ response: { status: 412 } });
    expect(save).toHaveBeenCalledTimes(3);
    expect(editor.getDocument().html).toContain("pending text");
  });
});
