import type { TMarkdownImportFile } from "./markdown-import";
import { readMarkdownImportSelection, resolveMarkdownImagePath, MAX_MARKDOWN_IMPORT_SIZE } from "./markdown-import";

/* oxlint-disable eslint/no-await-in-loop -- Read bounded HTML entries sequentially to limit peak memory. */

export type TConfluencePage = {
  path: string;
  title: string;
  content: HTMLElement;
  parentPath: string | null;
};

export const CONFLUENCE_MIME_TYPES: Record<string, string> = {
  png: "image/png",
  jpg: "image/jpeg",
  jpeg: "image/jpeg",
  gif: "image/gif",
  webp: "image/webp",
  svg: "image/svg+xml",
  bmp: "image/bmp",
  tif: "image/tiff",
  tiff: "image/tiff",
  mp4: "video/mp4",
  webm: "video/webm",
  ogv: "video/ogg",
  mov: "video/quicktime",
  avi: "video/x-msvideo",
  pdf: "application/pdf",
  txt: "text/plain",
  csv: "text/csv",
  zip: "application/zip",
  doc: "application/msword",
  docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  xls: "application/vnd.ms-excel",
  xlsx: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  ppt: "application/vnd.ms-powerpoint",
  pptx: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
};
export const INLINE_CONFLUENCE_IMAGES = new Set([
  "image/png",
  "image/jpeg",
  "image/gif",
  "image/webp",
  "image/bmp",
  "image/tiff",
]);
export const INLINE_CONFLUENCE_VIDEOS = new Set(["video/mp4", "video/webm", "video/ogg"]);

export const confluenceFile = (entry: TMarkdownImportFile) =>
  new File([entry.file], entry.file.name, {
    type:
      CONFLUENCE_MIME_TYPES[entry.path.split(".").pop()?.toLowerCase() ?? ""] ||
      entry.file.type ||
      "application/octet-stream",
  });

// Absolute download URLs in some HTML exports still point at the original site.
// Resolve only attachments present in the ZIP; never fetch from that site.
export const resolveConfluencePath = (pagePath: string, source: string): string | null => {
  if (!source || source.startsWith("#")) return null;
  if (/^https?:\/\//i.test(source) || source.startsWith("/")) {
    const attachment = source.match(/\/download\/attachments\/(.+?)(?:[?#]|$)/);
    if (!attachment) return null;
    const root = pagePath.includes("/") ? pagePath.slice(0, pagePath.lastIndexOf("/") + 1) : "";
    return resolveMarkdownImagePath(`${root}page.html`, `attachments/${attachment[1]}`);
  }
  if (/^[a-z][a-z\d+.-]*:/i.test(source) || source.startsWith("//")) return null;
  return resolveMarkdownImagePath(pagePath, source);
};

export const readConfluenceSpace = async (file: File) => {
  if (!file.name.toLowerCase().endsWith(".zip")) throw new Error("Choose a Confluence HTML space export (.zip).");
  const entries = await readMarkdownImportSelection([file]);
  if (entries.some((entry) => /(?:^|\/)entities\.xml$/i.test(entry.path)))
    throw new Error("This is an XML export. Export the Confluence space as HTML, then upload its ZIP file.");
  const pages: TConfluencePage[] = [];
  let spaceName = file.name.replace(/\.zip$/i, "");
  for (const entry of entries) {
    if (!/\.html?$/i.test(entry.path) || /(?:^|\/)attachments\//i.test(entry.path)) continue;
    if (entry.file.size > MAX_MARKDOWN_IMPORT_SIZE)
      throw new Error(`${entry.path}: HTML pages must be 5 MB or smaller.`);
    const doc = new DOMParser().parseFromString(await entry.file.text(), "text/html");
    const title = doc.querySelector("#title-text")?.textContent?.trim() || doc.title.trim() || entry.file.name;
    const content = doc.querySelector<HTMLElement>("#main-content, .wiki-content");
    if (/(?:^|\/)index\.html?$/i.test(entry.path) && !content) {
      spaceName = title || spaceName;
      continue;
    }
    if (!content) continue;
    const breadcrumbs = [...doc.querySelectorAll<HTMLAnchorElement>("#breadcrumbs a[href], .breadcrumbs a[href]")];
    const parentHref = breadcrumbs.at(-1)?.getAttribute("href");
    let parentPath: string | null = null;
    try {
      parentPath = parentHref ? resolveConfluencePath(entry.path, parentHref) : null;
    } catch {
      /* Missing ancestry falls back to the space root. */
    }
    pages.push({ path: entry.path, title, content, parentPath });
  }
  if (!pages.length) throw new Error("No Confluence pages found. Use an HTML space export with attachments.");
  const pagePaths = new Set(pages.map((page) => page.path));
  for (const page of pages)
    if (!pagePaths.has(page.parentPath ?? "") || page.parentPath === page.path) page.parentPath = null;
  return { entries, pages, spaceName };
};

const ALLOWED_TAGS = new Set(
  "p br h1 h2 h3 h4 h5 h6 strong b em i u s del code pre blockquote ul ol li table thead tbody tr th td hr a span div".split(
    " "
  )
);
const REMOVED_TAGS = new Set("script style iframe object embed form input button link meta base noscript".split(" "));

/** Rebuild inert editor markup; export chrome, event handlers and styles never enter a Page. */
export const confluenceToPageHtml = (
  page: TConfluencePage,
  replacements: Map<Element, HTMLElement>,
  pageUrls: Map<string, string>,
  warnings: string[]
) => {
  const doc = page.content.ownerDocument;
  const root = doc.createElement("div");
  const visit = (node: Node, target: HTMLElement) => {
    if (node.nodeType === 3) {
      target.append(doc.createTextNode(node.textContent ?? ""));
      return;
    }
    if (node.nodeType !== 1) return;
    const element = node as HTMLElement;
    if (replacements.has(element)) {
      target.append(replacements.get(element)!.cloneNode(true));
      return;
    }
    const tag = element.tagName.toLowerCase();
    if (element.matches(".page-metadata, .attachments, .toc-macro, #footer")) return;
    if (["img", "video", "source", "object", "embed"].includes(tag)) {
      const placeholder = doc.createElement("p");
      placeholder.textContent = `Media not imported: ${element.getAttribute("src") || element.getAttribute("data") || element.querySelector("source")?.getAttribute("src") || "missing source"}`;
      target.append(placeholder);
      return;
    }
    if (REMOVED_TAGS.has(tag)) return;
    const output = doc.createElement(ALLOWED_TAGS.has(tag) ? tag : "span");
    if (tag === "a") {
      const href = element.getAttribute("href") ?? "";
      let localPath: string | null = null;
      try {
        localPath = resolveConfluencePath(page.path, href);
      } catch {
        /* Unsafe links are omitted. */
      }
      const pageUrl = localPath ? pageUrls.get(localPath) : undefined;
      if (pageUrl) output.setAttribute("href", pageUrl + (href.includes("#") ? `#${href.split("#")[1]}` : ""));
      else if (/^(https?:\/\/|mailto:|tel:|#)/i.test(href)) output.setAttribute("href", href);
      else if (href) warnings.push(`${page.path}: link target not imported: ${href}`);
    }
    // Preserve section anchors, ordered-list numbering and table geometry.
    for (const attr of ["id", "colspan", "rowspan", "start", "language"]) {
      const value = element.getAttribute(attr);
      if (value) output.setAttribute(attr, value);
    }
    for (const child of node.childNodes) visit(child, output);
    target.append(output);
  };
  for (const node of page.content.childNodes) visit(node, root);
  return root.innerHTML || "<p></p>";
};
