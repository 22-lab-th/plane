import { describe, expect, it, vi } from "vitest";
import * as Y from "yjs";
import { persistMergedDocument } from "../src/extensions/document-persistence";
import { replaceDocument } from "../src/document-replacement";
import { AppError } from "../src/lib/errors";

describe("conditional collaborative persistence", () => {
  it("merges replacement tombstones into stale memory while preserving concurrent edits", async () => {
    const original = replaceDocument("", "<p>obsolete body</p>");
    const replacement = replaceDocument(original.contentBinaryEncoded, "<p>API replacement</p>");
    const document = new Y.Doc();
    try {
      Y.applyUpdate(document, Buffer.from(original.contentBinaryEncoded, "base64"));
      const paragraph = new Y.XmlElement("paragraph");
      const text = new Y.XmlText();
      text.insert(0, "offline addition");
      paragraph.insert(0, [text]);
      document.getXmlFragment("default").push([paragraph]);
      const service = {
        fetchDescriptionSnapshot: vi.fn().mockResolvedValue({
          binary: Buffer.from(replacement.contentBinaryEncoded, "base64"),
          etag: '"current"',
        }),
        updateDescriptionBinary: vi.fn().mockResolvedValue(undefined),
      };
      await persistMergedDocument(service, "page", document);
      const [, payload, etag] = service.updateDescriptionBinary.mock.calls[0];
      expect(etag).toBe('"current"');
      expect(payload.description_html).toContain("API replacement");
      expect(payload.description_html).toContain("offline addition");
      expect(payload.description_html).not.toContain("obsolete body");
      expect(document.getXmlFragment("default").toString()).toContain("API replacement");
    } finally {
      document.destroy();
    }
  });
  it("re-fetches and re-encodes after a concurrent replacement wins the CAS", async () => {
    const first = replaceDocument("", "<p>first body</p>");
    const second = replaceDocument(first.contentBinaryEncoded, "<p>second body</p>");
    const document = new Y.Doc();
    try {
      const service = {
        fetchDescriptionSnapshot: vi
          .fn()
          .mockResolvedValueOnce({ binary: Buffer.from(first.contentBinaryEncoded, "base64"), etag: '"one"' })
          .mockResolvedValueOnce({ binary: Buffer.from(second.contentBinaryEncoded, "base64"), etag: '"two"' }),
        updateDescriptionBinary: vi
          .fn()
          .mockRejectedValueOnce(new AppError("conflict", { statusCode: 412 }))
          .mockResolvedValueOnce(undefined),
      };
      await persistMergedDocument(service, "page", document);
      expect(service.updateDescriptionBinary).toHaveBeenCalledTimes(2);
      const [, payload, etag] = service.updateDescriptionBinary.mock.calls[1];
      expect(etag).toBe('"two"');
      expect(payload.description_html).toContain("second body");
      expect(payload.description_html).not.toContain("first body");
    } finally {
      document.destroy();
    }
  });
  it("surfaces exhausted conflicts and keeps in-memory edits", async () => {
    const document = new Y.Doc();
    try {
      document.getText("unsaved").insert(0, "keep me");
      const service = {
        fetchDescriptionSnapshot: vi.fn().mockResolvedValue({ binary: Buffer.alloc(0), etag: '"same"' }),
        updateDescriptionBinary: vi.fn().mockRejectedValue(new AppError("conflict", { statusCode: 412 })),
      };
      await expect(persistMergedDocument(service, "page", document)).rejects.toMatchObject({ statusCode: 412 });
      expect(service.updateDescriptionBinary).toHaveBeenCalledTimes(3);
      expect(document.getText("unsaved").toString()).toBe("keep me");
    } finally {
      document.destroy();
    }
  });
});
