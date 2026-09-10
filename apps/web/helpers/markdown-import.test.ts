import { describe, expect, it } from "vitest";
import { strToU8, zipSync } from "fflate";
// helpers
import {
  isSupportedMarkdownImage,
  markdownToPageHtml,
  normalizeMarkdownImportPath,
  readMarkdownImportSelection,
  resolveMarkdownImagePath,
} from "./markdown-import";

const PNG_HEADER = new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10, 0, 0, 0, 0]);

describe("Markdown import", () => {
  it("rejects paths that escape the import root", () => {
    expect(() => normalizeMarkdownImportPath("../secret.md")).toThrow("escapes the import root");
    expect(() => normalizeMarkdownImportPath("/secret.md")).toThrow("Unsafe path");
  });

  it("resolves images relative to the Markdown document", () => {
    expect(resolveMarkdownImagePath("guides/setup/install.md", "../images/screen%201.png")).toBe(
      "guides/images/screen 1.png"
    );
  });

  it("streams ZIP contents and removes one common packaging directory", async () => {
    const zip = zipSync({
      "documentation/guide.md": strToU8("# Guide\n\n![Diagram](images/diagram.png)"),
      "documentation/images/diagram.png": PNG_HEADER,
    });
    const entries = await readMarkdownImportSelection([new File([zip], "documentation.zip")]);

    expect(entries.map((entry) => entry.path)).toEqual(["guide.md", "images/diagram.png"]);
    expect(await isSupportedMarkdownImage(entries[1])).toBe(true);
  });

  it("renders uploaded Page image components in place of Markdown images", () => {
    const result = markdownToPageHtml("# Guide\n\n![Diagram](images/diagram.png)", {
      "images/diagram.png": '<image-component src="asset-1"></image-component>',
    });

    expect(result).toContain("<h1>Guide</h1>");
    expect(result).toContain('<image-component src="asset-1"></image-component>');
    expect(result).not.toContain("<img");
  });
});
