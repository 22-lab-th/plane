/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 */
import { Buffer } from "node:buffer";
import { timingSafeEqual } from "node:crypto";
import { createServer } from "node:http";
import {
  getAllDocumentFormatsFromDocumentEditorBinaryData,
  getBinaryDataFromDocumentEditorHTMLString,
} from "@plane/editor";
import * as Y from "yjs";

export const MAX_REPLACEMENT_BODY_BYTES = 16 * 1024 * 1024;

/** Preserve deletion tombstones so a stale editor cannot resurrect replaced text. */
export function replaceDocument(baseBinary: string, html: string) {
  const document = new Y.Doc();
  const replacement = new Y.Doc();
  try {
    if (baseBinary) Y.applyUpdate(document, Buffer.from(baseBinary, "base64"));
    Y.applyUpdate(replacement, getBinaryDataFromDocumentEditorHTMLString(html));
    const target = document.getXmlFragment("default");
    const source = replacement.getXmlFragment("default");
    document.transact(() => {
      target.delete(0, target.length);
      target.insert(
        0,
        source.toArray().map((node) => {
          if (node instanceof Y.XmlElement || node instanceof Y.XmlText) return node.clone();
          throw new Error("Unsupported document node");
        })
      );
    });
    return getAllDocumentFormatsFromDocumentEditorBinaryData(Y.encodeStateAsUpdate(document), false);
  } finally {
    replacement.destroy();
    document.destroy();
  }
}

/** Internal API compatibility endpoint; never routed through the public proxy. */
export function createDocumentReplacementServer(secret: string) {
  if (!secret) throw new Error("Document replacement requires LIVE_SERVER_SECRET_KEY");
  const expected = Buffer.from(secret);
  return createServer((request, response) => {
    const reply = (status: number, value: unknown) => {
      response.writeHead(status, { "content-type": "application/json" });
      response.end(JSON.stringify(value));
    };
    if (request.method === "GET" && request.url === "/healthz") {
      reply(200, { status: "ok" });
      return;
    }
    if (request.method !== "POST" || request.url !== "/replace-document") {
      reply(404, { error: "not found" });
      return;
    }
    const suppliedHeader = request.headers["live-server-secret-key"];
    const supplied = Buffer.from(typeof suppliedHeader === "string" ? suppliedHeader : "");
    if (supplied.length !== expected.length || !timingSafeEqual(supplied, expected)) {
      reply(401, { error: "unauthorized" });
      request.resume();
      return;
    }
    let size = 0;
    let oversized = false;
    const chunks: Buffer[] = [];
    request.on("data", (chunk: Buffer) => {
      if (oversized) return;
      size += chunk.length;
      if (size > MAX_REPLACEMENT_BODY_BYTES) {
        oversized = true;
        chunks.length = 0;
        reply(413, { error: "document payload is too large" });
        return;
      }
      chunks.push(chunk);
    });
    request.on("end", () => {
      if (oversized) return;
      try {
        const payload: unknown = JSON.parse(Buffer.concat(chunks).toString("utf8"));
        if (!payload || typeof payload !== "object" || !("description_html" in payload)) {
          throw new Error("Missing document");
        }
        const baseBinary = "base_binary" in payload ? payload.base_binary : "";
        if (typeof payload.description_html !== "string" || typeof baseBinary !== "string") {
          throw new Error("Invalid document fields");
        }
        const result = replaceDocument(baseBinary, payload.description_html);
        reply(200, {
          description_binary: result.contentBinaryEncoded,
          description_json: result.contentJSON,
          description_html: result.contentHTML,
        });
      } catch {
        reply(400, { error: "invalid document" });
      }
    });
  });
}
