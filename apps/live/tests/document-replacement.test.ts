import { Buffer } from "node:buffer";
import type { AddressInfo } from "node:net";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import {
  getAllDocumentFormatsFromDocumentEditorBinaryData,
  getBinaryDataFromDocumentEditorHTMLString,
} from "@plane/editor";
import * as Y from "yjs";
import {
  createDocumentReplacementServer,
  MAX_REPLACEMENT_BODY_BYTES,
  replaceDocument,
} from "../src/document-replacement";

describe("internal document replacement", () => {
  const secret = "rehearsal-replacement-secret";
  const server = createDocumentReplacementServer(secret);
  let url: string;
  beforeAll(async () => {
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    url = `http://127.0.0.1:${(server.address() as AddressInfo).port}/replace-document`;
  });
  afterAll(async () => {
    server.closeAllConnections();
    await new Promise<void>((resolve) => server.close(() => resolve()));
  });
  it("retains replacement tables/images and deletion tombstones when stale state merges", () => {
    const original = replaceDocument("", "<p>obsolete pipe text | A | B |</p>");
    const replaced = replaceDocument(
      original.contentBinaryEncoded,
      '<table><tbody><tr><td>A</td><td>B</td></tr></tbody></table><image-component src="00000000-0000-0000-0000-000000000001" alignment="center" status="uploaded"></image-component>'
    );
    const stale = new Y.Doc();
    try {
      Y.applyUpdate(stale, Buffer.from(original.contentBinaryEncoded, "base64"));
      Y.applyUpdate(stale, Buffer.from(replaced.contentBinaryEncoded, "base64"));
      const merged = getAllDocumentFormatsFromDocumentEditorBinaryData(Y.encodeStateAsUpdate(stale), false);
      expect(merged.contentHTML).toContain("<table");
      expect(merged.contentHTML).toContain("<image-component");
      expect(merged.contentHTML).toContain('src="00000000-0000-0000-0000-000000000001"');
      expect(merged.contentHTML).not.toContain("obsolete pipe text");
      expect(merged.contentHTML).toBe(replaced.contentHTML);
    } finally {
      stale.destroy();
    }
  });
  it("preserves the separate title fragment", () => {
    const before = new Y.Doc();
    const after = new Y.Doc();
    try {
      const binary = getBinaryDataFromDocumentEditorHTMLString("<p>old body</p>", "Existing title");
      Y.applyUpdate(before, binary);
      const result = replaceDocument(Buffer.from(binary).toString("base64"), "<p>new body</p>");
      Y.applyUpdate(after, Buffer.from(result.contentBinaryEncoded, "base64"));
      expect(before.getXmlFragment("title").toString()).toContain("Existing title");
      expect(after.getXmlFragment("title").toString()).toBe(before.getXmlFragment("title").toString());
    } finally {
      before.destroy();
      after.destroy();
    }
  });
  it("requires the secret and serves the backend response contract", async () => {
    const body = JSON.stringify({ base_binary: "", description_html: "<p>Replacement</p>" });
    expect((await fetch(url, { method: "POST", body })).status).toBe(401);
    expect((await fetch(url, { method: "POST", body, headers: { "live-server-secret-key": "wrong" } })).status).toBe(
      401
    );
    const response = await fetch(url, { method: "POST", body, headers: { "live-server-secret-key": secret } });
    expect(response.status).toBe(200);
    const result = (await response.json()) as Record<string, unknown>;
    expect(result.description_html).toContain("Replacement");
    expect(result.description_binary).toEqual(expect.any(String));
    expect(result.description_json).toBeTruthy();
  });
  it("rejects malformed input and bounds authenticated request size", async () => {
    await Promise.all(
      [
        "{",
        "null",
        JSON.stringify({ description_html: 12 }),
        JSON.stringify({ description_html: "<p>x</p>", base_binary: 12 }),
      ].map(async (body) => {
        expect((await fetch(url, { method: "POST", body, headers: { "live-server-secret-key": secret } })).status).toBe(
          400
        );
      })
    );
    expect(
      (
        await fetch(url, {
          method: "POST",
          body: "x".repeat(MAX_REPLACEMENT_BODY_BYTES + 1),
          headers: { "live-server-secret-key": secret },
        })
      ).status
    ).toBe(413);
  });
  it("fails closed without a configured secret", () => {
    expect(() => createDocumentReplacementServer("")).toThrow("LIVE_SERVER_SECRET_KEY");
  });
});
