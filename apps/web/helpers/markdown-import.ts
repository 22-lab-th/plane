import MarkdownIt from "markdown-it";
import { Unzip, UnzipInflate } from "fflate";

export const MAX_MARKDOWN_IMPORT_SIZE = 5 * 1024 * 1024;
export const MAX_MARKDOWN_BUNDLE_SIZE = 50 * 1024 * 1024;
export const MAX_MARKDOWN_BUNDLE_ENTRIES = 500;
export const MAX_MARKDOWN_BUNDLE_EXTRACTED_SIZE = 100 * 1024 * 1024;
export const MAX_MARKDOWN_IMAGE_SIZE = 5 * 1024 * 1024;

const MARKDOWN_EXTENSIONS = new Set(["md", "markdown"]);
const IMAGE_MIME_BY_EXTENSION: Record<string, string> = {
  gif: "image/gif",
  jpeg: "image/jpeg",
  jpg: "image/jpeg",
  png: "image/png",
  webp: "image/webp",
};

export type TMarkdownImportFile = {
  file: File;
  path: string;
};

const markdownParser = new MarkdownIt({
  breaks: false,
  html: false,
  linkify: true,
  typographer: false,
});

export const isMarkdownFile = (file: File) => {
  const extension = file.name.split(".").pop()?.toLowerCase();
  return extension ? MARKDOWN_EXTENSIONS.has(extension) : false;
};

export const isMarkdownBundle = (file: File) => file.name.toLowerCase().endsWith(".zip");

export const getMarkdownPageName = (fileName: string) => {
  const name = fileName.replace(/\.(?:md|markdown)$/i, "").trim();
  return (name || "Imported page").slice(0, 255);
};

export const normalizeMarkdownImportPath = (path: string) => {
  const normalized = path.replaceAll("\\", "/").replace(/^\.\//, "");
  if (normalized.startsWith("/") || /^[a-z]:\//i.test(normalized)) throw new Error(`Unsafe path: ${path}`);
  const parts: string[] = [];
  for (const part of normalized.split("/")) {
    if (!part || part === ".") continue;
    if (part === "..") {
      if (!parts.length) throw new Error(`Path escapes the import root: ${path}`);
      parts.pop();
    } else {
      parts.push(part);
    }
  }
  return parts.join("/");
};

const stripCommonRoot = (entries: TMarkdownImportFile[]) => {
  if (!entries.length) return entries;
  const firstSegments = entries.map((entry) => entry.path.split("/"));
  const commonRoot = firstSegments[0]?.[0];
  if (!commonRoot || !firstSegments.every((segments) => segments.length > 1 && segments[0] === commonRoot))
    return entries;
  return entries.map((entry) => ({ ...entry, path: entry.path.slice(commonRoot.length + 1) }));
};

const fileMimeType = (path: string) => IMAGE_MIME_BY_EXTENSION[path.split(".").pop()?.toLowerCase() || ""] || "";

const extractMarkdownBundle = (compressed: Uint8Array) => {
  const entries: TMarkdownImportFile[] = [];
  let entryCount = 0;
  let extractedSize = 0;
  const unzipper = new Unzip((archiveFile) => {
    entryCount += 1;
    if (entryCount > MAX_MARKDOWN_BUNDLE_ENTRIES)
      throw new Error(`ZIP bundles may contain at most ${MAX_MARKDOWN_BUNDLE_ENTRIES} entries.`);
    const path = normalizeMarkdownImportPath(archiveFile.name);
    if (archiveFile.name.endsWith("/")) {
      archiveFile.ondata = () => undefined;
      archiveFile.start();
      return;
    }

    const chunks: Uint8Array[] = [];
    let fileSize = 0;
    archiveFile.ondata = (error, chunk, final) => {
      if (error) throw error;
      fileSize += chunk.byteLength;
      extractedSize += chunk.byteLength;
      if (extractedSize > MAX_MARKDOWN_BUNDLE_EXTRACTED_SIZE) {
        archiveFile.terminate();
        throw new Error("ZIP bundle expands beyond the 100 MB limit.");
      }
      chunks.push(chunk);
      if (!final) return;
      const bytes = new Uint8Array(fileSize);
      let offset = 0;
      for (const part of chunks) {
        bytes.set(part, offset);
        offset += part.byteLength;
      }
      const name = path.split("/").pop() || "file";
      entries.push({ path, file: new File([bytes], name, { type: fileMimeType(path) }) });
    };
    archiveFile.start();
  });
  unzipper.register(UnzipInflate);
  unzipper.push(compressed, true);
  return entries;
};

export const readMarkdownImportSelection = async (files: File[]): Promise<TMarkdownImportFile[]> => {
  if (!files.length) throw new Error("Choose a Markdown file, ZIP bundle, or folder.");
  if (files.length === 1 && isMarkdownBundle(files[0])) {
    const bundle = files[0];
    if (bundle.size > MAX_MARKDOWN_BUNDLE_SIZE) throw new Error("ZIP bundles must be 50 MB or smaller.");
    return stripCommonRoot(extractMarkdownBundle(new Uint8Array(await bundle.arrayBuffer())));
  }

  const entries = files.map((file) => ({
    file,
    path: normalizeMarkdownImportPath(file.webkitRelativePath || file.name),
  }));
  return stripCommonRoot(entries);
};

export const isMarkdownImportEntry = (entry: TMarkdownImportFile) => {
  const extension = entry.path.split(".").pop()?.toLowerCase();
  return extension ? MARKDOWN_EXTENSIONS.has(extension) : false;
};

export const isSupportedMarkdownImage = async (entry: TMarkdownImportFile) => {
  const mimeType = entry.file.type || fileMimeType(entry.path);
  if (!Object.values(IMAGE_MIME_BY_EXTENSION).includes(mimeType) || entry.file.size > MAX_MARKDOWN_IMAGE_SIZE)
    return false;
  const bytes = new Uint8Array(await entry.file.slice(0, 12).arrayBuffer());
  if (mimeType === "image/png")
    return bytes.length >= 8 && [137, 80, 78, 71, 13, 10, 26, 10].every((v, i) => bytes[i] === v);
  if (mimeType === "image/jpeg") return bytes.length >= 3 && bytes[0] === 255 && bytes[1] === 216 && bytes[2] === 255;
  if (mimeType === "image/gif") return new TextDecoder().decode(bytes.slice(0, 6)).match(/^GIF8[79]a$/) !== null;
  return (
    bytes.length >= 12 &&
    new TextDecoder().decode(bytes.slice(0, 4)) === "RIFF" &&
    new TextDecoder().decode(bytes.slice(8, 12)) === "WEBP"
  );
};

export const getMarkdownImageSources = (markdown: string) => {
  const sources = new Set<string>();
  const visit = (tokens: ReturnType<typeof markdownParser.parse>) => {
    for (const token of tokens) {
      if (token.type === "image") {
        const source = token.attrGet("src");
        if (source) sources.add(source);
      }
      if (token.children) visit(token.children);
    }
  };
  visit(markdownParser.parse(markdown, {}));
  return [...sources];
};

export const resolveMarkdownImagePath = (markdownPath: string, source: string) => {
  const cleanSource = source.split(/[?#]/, 1)[0];
  let decodedSource = cleanSource;
  try {
    decodedSource = decodeURIComponent(cleanSource);
  } catch {
    // Keep the original text when percent-encoding is malformed.
  }
  const parent = markdownPath.includes("/") ? markdownPath.slice(0, markdownPath.lastIndexOf("/")) : "";
  return normalizeMarkdownImportPath(parent ? `${parent}/${decodedSource}` : decodedSource);
};

export const markdownToPageHtml = (markdown: string, imageHtmlBySource: Record<string, string> = {}) => {
  const normalizedMarkdown = markdown.replace(/^\uFEFF/, "").replace(/\r\n?/g, "\n");
  if (!normalizedMarkdown.trim()) return "<p></p>";
  const parser = new MarkdownIt({ breaks: false, html: false, linkify: true, typographer: false });
  const defaultImageRenderer = parser.renderer.rules.image;
  parser.renderer.rules.image = (tokens, index, options, env, self) => {
    const source = tokens[index].attrGet("src") || "";
    const replacement = imageHtmlBySource[source];
    if (replacement) return replacement;
    return defaultImageRenderer
      ? defaultImageRenderer(tokens, index, options, env, self)
      : self.renderToken(tokens, index, options);
  };
  return parser.render(normalizedMarkdown);
};
