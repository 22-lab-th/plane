// @vitest-environment happy-dom
import { File as NodeFile } from "node:buffer";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { strToU8, zipSync } from "fflate";
import type { Window as HappyWindow } from "happy-dom";
import { readConfluenceSpace, resolveConfluencePath, confluenceToPageHtml } from "./confluence-import";
import { importConfluenceSpace } from "./confluence-page-import";
import {
  getBinaryDataFromDocumentEditorHTMLString,
  getAllDocumentFormatsFromDocumentEditorBinaryData,
} from "@plane/editor/lib";

const api = vi.hoisted(() => ({ update: vi.fn(), archive: vi.fn(), upload: vi.fn(), link: vi.fn() }));
vi.mock("@/services/page/project-page.service", () => ({
  ProjectPageService: class {
    update = api.update;
    archive = api.archive;
  },
}));
vi.mock("@/services/project-file.service", () => ({
  PROJECT_FILE_REF_PREFIX: "project-file:",
  uploadProjectFile: api.upload,
  ProjectFileService: class {
    linkProjectFile = api.link;
  },
}));

const html = (title: string, body: string, breadcrumbs = "") =>
  `<html><head><title>${title}</title></head><body><div id="breadcrumbs">${breadcrumbs}</div><h1 id="title-text">${title}</h1><div id="main-content" class="wiki-content">${body}</div><div id="footer">Export footer</div></body></html>`;
const bundle = (files: Record<string, string | Uint8Array>) =>
  new NodeFile(
    [
      zipSync(
        Object.fromEntries(
          Object.entries(files).map(([path, content]) => [
            path,
            typeof content === "string" ? strToU8(content) : content,
          ])
        )
      ),
    ],
    "Space.zip"
  ) as unknown as File;

beforeEach(() => {
  const happyWindow = window as unknown as HappyWindow;
  happyWindow.happyDOM.settings.disableCSSFileLoading = true;
  happyWindow.happyDOM.settings.disableJavaScriptFileLoading = true;
  // Use Node's Blob implementation for streaming ZIP/File APIs, and a DOM for parsing.
  vi.stubGlobal("File", NodeFile);
  vi.clearAllMocks();
  api.update.mockResolvedValue({});
  api.archive.mockResolvedValue(undefined);
  api.link.mockResolvedValue({});
  api.upload.mockImplementation(async ({ file }: { file: File }) => ({ file: { id: `stored-${file.name}` } }));
});

describe("Confluence HTML space import", () => {
  it("keeps media in document order through the actual editor HTML/Yjs round trip", () => {
    const binary = getBinaryDataFromDocumentEditorHTMLString(
      '<p>Before <strong>image</strong><image-component src="project-file:pic" status="uploaded"></image-component>Between</p><video-component src="project-file:clip" width="640"></video-component><p>After</p>'
    );
    const { contentHTML } = getAllDocumentFormatsFromDocumentEditorBinaryData(binary, false);
    expect(contentHTML).toMatch(/Before.*project-file:pic.*Between.*project-file:clip.*After/);
    expect(contentHTML).toContain("video-component");
  });
  it("parses titles and hierarchy while excluding index chrome and attachment HTML", async () => {
    const result = await readConfluenceSpace(
      bundle({
        "export/index.html": "<title>Knowledge Base</title><h1>Contents</h1>",
        "export/Parent_10.html": html("Parent", "<p>Parent text</p>"),
        "export/Child_20.html": html(
          "Child",
          "<p>Child text</p>",
          '<a href="index.html">Space</a><a href="Parent_10.html">Parent</a>'
        ),
        "export/attachments/10/embed.html": "<script>evil()</script>",
      })
    );
    expect(result.spaceName).toBe("Knowledge Base");
    expect(result.pages.map(({ path, title, parentPath }) => ({ path, title, parentPath }))).toEqual([
      { path: "Parent_10.html", title: "Parent", parentPath: null },
      { path: "Child_20.html", title: "Child", parentPath: "Parent_10.html" },
    ]);
  });

  it("rejects XML exports, non-exports and unsafe relative paths", async () => {
    await expect(readConfluenceSpace(bundle({ "entities.xml": "<root/>" }))).rejects.toThrow("XML export");
    await expect(readConfluenceSpace(bundle({ "test.html": "<h1>Hello</h1>" }))).rejects.toThrow("No Confluence pages");
    expect(() => resolveConfluencePath("10.html", "../secret.png")).toThrow("escapes");
    expect(resolveConfluencePath("10.html", "attachments/10/screen%201.png?version=2")).toBe(
      "attachments/10/screen 1.png"
    );
    expect(
      resolveConfluencePath("10.html", "https://site.atlassian.net/wiki/download/attachments/10/a.png?version=2")
    ).toBe("attachments/10/a.png");
    expect(resolveConfluencePath("10.html", "javascript:alert(1)")).toBeNull();
  });

  it("preserves content order, tables and internal links while dropping active markup", async () => {
    const { pages } = await readConfluenceSpace(
      bundle({
        "10.html": html(
          "Page",
          '<p onclick="evil()">Before</p><img src="attachments/10/pic.png"><p>Middle</p><video src="attachments/10/movie.mp4"></video><p>After</p><table><tr><td colspan="2">Cell</td></tr></table><a href="20.html">Next</a><a href="javascript:evil()">Bad</a><script>evil()</script><iframe></iframe>'
        ),
      })
    );
    const image = pages[0].content.ownerDocument.createElement("image-component");
    image.setAttribute("src", "project-file:pic");
    const video = pages[0].content.ownerDocument.createElement("video-component");
    video.setAttribute("src", "project-file:movie");
    const replacements = new Map<Element, HTMLElement>([
      [pages[0].content.querySelector("img")!, image],
      [pages[0].content.querySelector("video")!, video],
    ]);
    const output = confluenceToPageHtml(pages[0], replacements, new Map([["20.html", "/w/projects/p/pages/20"]]), []);
    expect(output).toMatch(/Before.*image-component.*Middle.*video-component.*After/);
    expect(output).toContain('colspan="2"');
    expect(output).toContain('href="/w/projects/p/pages/20"');
    expect(output).not.toMatch(/onclick|javascript:|<script|<iframe|Export footer/);
  });

  it("uploads shared media once into Files, links both pages and rewrites media in place", async () => {
    const createPage = vi.fn().mockImplementation(async (data) => ({ ...data, id: `page-${data.name}` }));
    const createFolder = vi.fn().mockImplementation(async (data) => ({ ...data, id: `folder-${data.name}` }));
    const report = await importConfluenceSpace({
      file: bundle({
        "index.html": "<title>Space</title>",
        "Parent_10.html": html(
          "Parent",
          '<p>Before</p><img src="attachments/10/pic.png"><p>Between</p><video><source src="attachments/10/movie.mp4"></video><p>After</p><a href="Child_20.html">Child</a>'
        ),
        "Child_20.html": html(
          "Child",
          '<img src="attachments/10/pic.png"><img src="missing.png">',
          '<a href="Parent_10.html">Parent</a>'
        ),
        "attachments/10/pic.png": new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10]),
        "attachments/10/movie.mp4": new Uint8Array([0, 0, 0, 16, 102, 116, 121, 112]),
        "attachments/10/manual.pdf": strToU8("%PDF-1.7"),
      }),
      workspaceSlug: "w",
      projectId: "p",
      parentId: null,
      access: 1,
      createPage,
      createFolder,
    });
    expect(report.pagesImported).toBe(2);
    expect(report.filesUploaded).toBe(3);
    expect(api.upload).toHaveBeenCalledTimes(3);
    expect(api.upload.mock.calls[1][0].file.type).toBe("video/mp4");
    expect(api.upload.mock.calls[2][0].link).toEqual({ entity_type: "page", entity_id: "page-Parent" });
    expect(api.link).toHaveBeenCalledWith("w", "p", "stored-pic.png", { entity_type: "page", entity_id: "page-Child" });
    expect(createPage.mock.calls[1][0]).toMatchObject({ parent: "folder-Parent", access: 1 });
    const saved = api.update.mock.calls[0][3].description_html;
    expect(saved).toMatch(/Before.*project-file:stored-pic.png.*Between.*project-file:stored-movie.mp4.*After/);
    expect(saved).toContain("/w/projects/p/pages/page-Child");
    expect(report.warnings.join("\n")).toContain("missing.png");
    expect(api.update.mock.calls[1][3].description_html).toContain("Media not imported");
  });

  it("reports failed uploads and retains surrounding content", async () => {
    api.upload.mockRejectedValue({ error: "Quota exceeded" });
    const report = await importConfluenceSpace({
      file: bundle({
        "10.html": html("Page", '<p>Before</p><img src="attachments/10/pic.png"><p>After</p>'),
        "attachments/10/pic.png": strToU8("png"),
      }),
      workspaceSlug: "w",
      projectId: "p",
      parentId: null,
      access: 0,
      createPage: async () => ({ id: "page" }) as never,
      createFolder: async () => ({ id: "folder" }) as never,
    });
    expect(report.filesUploaded).toBe(0);
    expect(report.warnings).toContain("attachments/10/pic.png: Quota exceeded");
    expect(api.update.mock.calls[0][3].description_html).toMatch(/Before.*Media not imported.*After/);
    expect(api.upload).toHaveBeenCalledTimes(1);
  });

  it("preserves linked images and imports legacy video object embeds in their original positions", async () => {
    const report = await importConfluenceSpace({
      file: bundle({
        "10.html": html(
          "Page",
          '<p>Before</p><a href="attachments/10/pic.png"><img src="attachments/10/pic.png"></a><p>Between</p><object data="attachments/10/clip.mp4"><embed src="attachments/10/clip.mp4"></object><p>After</p>'
        ),
        "attachments/10/pic.png": strToU8("png"),
        "attachments/10/clip.mp4": strToU8("video"),
      }),
      workspaceSlug: "w",
      projectId: "p",
      parentId: null,
      access: 0,
      createPage: async () => ({ id: "page" }) as never,
      createFolder: async () => ({ id: "folder" }) as never,
    });
    expect(report.filesUploaded).toBe(2);
    const saved = api.update.mock.calls[0][3].description_html;
    expect(saved).toMatch(/Before.*image-component.*Between.*video-component.*After/);
    expect(saved).not.toContain("<object");
    expect(saved).not.toContain("<embed");
  });

  it("archives created pages in reverse order after a content-save failure", async () => {
    api.update.mockRejectedValue(new Error("Save failed"));
    await expect(
      importConfluenceSpace({
        file: bundle({ "10.html": html("Page", "<p>Content</p>") }),
        workspaceSlug: "w",
        projectId: "p",
        parentId: null,
        access: 0,
        createPage: async () => ({ id: "page" }) as never,
        createFolder: async () => ({ id: "folder" }) as never,
      })
    ).rejects.toThrow("Save failed");
    expect(api.archive.mock.calls.map((call) => call[2])).toEqual(["page", "folder"]);
  });
});
