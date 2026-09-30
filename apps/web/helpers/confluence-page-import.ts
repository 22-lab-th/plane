import type { TPage } from "@plane/types";
import { ProjectPageService } from "@/services/page/project-page.service";
import { ProjectFileService, uploadProjectFile, PROJECT_FILE_REF_PREFIX } from "@/services/project-file.service";
import {
  readConfluenceSpace,
  resolveConfluencePath,
  confluenceFile,
  confluenceToPageHtml,
  INLINE_CONFLUENCE_IMAGES,
  INLINE_CONFLUENCE_VIDEOS,
} from "./confluence-import";

/* oxlint-disable eslint/no-await-in-loop, eslint-plugin-unicorn/no-array-reverse -- Page ownership, ancestry, uploads and rollback must be ordered. */

export type TConfluenceImportReport = {
  pagesImported: number;
  filesUploaded: number;
  warnings: string[];
  pageIds: string[];
};

type TOptions = {
  file: File;
  workspaceSlug: string;
  projectId: string;
  parentId: string | null;
  access: number;
  createPage: (data: Partial<TPage>) => Promise<TPage | undefined>;
  createFolder: (data: Partial<TPage>) => Promise<TPage | undefined>;
  onProgress?: (message: string) => void;
};

export const importConfluenceSpace = async (options: TOptions): Promise<TConfluenceImportReport> => {
  options.onProgress?.("Reading space export");
  const { entries, pages, spaceName } = await readConfluenceSpace(options.file);
  const byPath = new Map(entries.map((entry) => [entry.path, entry]));
  if (byPath.size !== entries.length) throw new Error("The export contains duplicate file paths.");
  const pageService = new ProjectPageService();
  const fileService = new ProjectFileService();
  const createdIds: string[] = [];
  const pageIds = new Map<string, string>();
  const folderIds = new Map<string, string>();
  const uploaded = new Map<string, string>();
  const failedUploads = new Set<string>();
  const report: TConfluenceImportReport = { pagesImported: 0, filesUploaded: 0, warnings: [], pageIds: [] };
  const base = `/${options.workspaceSlug}/projects/${options.projectId}`;

  try {
    const spaceFolder = await options.createFolder({
      name: spaceName,
      access: options.access,
      parent: options.parentId,
    });
    if (!spaceFolder?.id) throw new Error("Could not create space folder.");
    createdIds.push(spaceFolder.id);
    const spaceFolderId = spaceFolder.id;
    // Plane CE parents must be folders. Give each Confluence ancestor a named folder
    // containing its page and descendants, retaining the source tree.
    const ensureFolder = async (path: string, ancestry = new Set<string>()): Promise<string> => {
      const existing = folderIds.get(path);
      if (existing) return existing;
      if (ancestry.has(path)) throw new Error("The export contains a circular page hierarchy.");
      const page = pages.find((candidate) => candidate.path === path);
      if (!page) return spaceFolderId;
      ancestry.add(path);
      const parent = page.parentPath ? await ensureFolder(page.parentPath, ancestry) : spaceFolderId;
      const folder = await options.createFolder({ name: page.title, access: options.access, parent });
      if (!folder?.id) throw new Error(`Could not create folder for ${page.title}.`);
      createdIds.push(folder.id);
      folderIds.set(path, folder.id);
      return folder.id;
    };
    const ancestors = new Set(pages.map((page) => page.parentPath).filter((path): path is string => Boolean(path)));
    for (const page of pages) {
      options.onProgress?.(`Creating page ${pageIds.size + 1}/${pages.length}`);
      const parent = ancestors.has(page.path)
        ? await ensureFolder(page.path)
        : page.parentPath
          ? await ensureFolder(page.parentPath)
          : spaceFolderId;
      const created = await options.createPage({
        name: page.title,
        access: options.access,
        parent,
        description_html: "<p></p>",
      });
      if (!created?.id) throw new Error(`Could not create ${page.title}.`);
      createdIds.push(created.id);
      pageIds.set(page.path, created.id);
    }
    const pageUrls = new Map([...pageIds].map(([path, id]) => [path, `${base}/pages/${id}`]));

    for (const page of pages) {
      options.onProgress?.(`Importing content ${report.pagesImported + 1}/${pages.length}: ${page.title}`);
      const pageId = pageIds.get(page.path)!;
      const replacements = new Map<Element, HTMLElement>();
      const doc = page.content.ownerDocument;
      const elements = [...page.content.querySelectorAll<HTMLElement>("img, video, object, embed, a[href]")];
      for (const element of elements) {
        if (element.tagName === "A" && element.querySelector("img, video, object, embed")) continue;
        // A video may have multiple sources; use the first local, supported one.
        const sources =
          element.tagName === "VIDEO"
            ? [
                element.getAttribute("src"),
                ...[...element.querySelectorAll("source")].map((source) => source.getAttribute("src")),
              ]
            : element.tagName === "OBJECT"
              ? [
                  element.getAttribute("data"),
                  element.querySelector('param[name="movie"], param[name="src"]')?.getAttribute("value"),
                ]
              : [element.getAttribute(element.tagName === "A" ? "href" : "src")];
        let path: string | null = null;
        for (const source of sources) {
          try {
            const candidate = source ? resolveConfluencePath(page.path, source) : null;
            if (candidate && byPath.has(candidate)) {
              path = candidate;
              break;
            }
          } catch {
            /* Unsafe media is reported below. */
          }
        }
        const entry = path ? byPath.get(path) : undefined;
        if (!entry || pageIds.has(path!)) {
          if (element.tagName !== "A")
            report.warnings.push(
              `${page.path}: media missing from export: ${sources.filter(Boolean).join(", ") || "missing source"}`
            );
          continue;
        }
        const file = confluenceFile(entry);
        let id = uploaded.get(path!);
        if (id) {
          try {
            await fileService.linkProjectFile(options.workspaceSlug, options.projectId, id, {
              entity_type: "page",
              entity_id: pageId,
            });
          } catch {
            report.warnings.push(`${page.path}: could not link shared file ${entry.path} to this page.`);
          }
        }
        if (!id && !failedUploads.has(path!)) {
          try {
            const result = await uploadProjectFile({
              workspaceSlug: options.workspaceSlug,
              projectId: options.projectId,
              file,
              link: { entity_type: "page", entity_id: pageId },
            });
            id = result.file.id;
            uploaded.set(path!, id);
            report.filesUploaded += 1;
          } catch (error) {
            failedUploads.add(path!);
            const message =
              error instanceof Error ? error.message : (error as { error?: string })?.error || "Upload failed";
            report.warnings.push(`${entry.path}: ${message}`);
          }
        }
        if (!id) continue;
        const image = element.tagName === "IMG" && INLINE_CONFLUENCE_IMAGES.has(file.type);
        const video =
          ["VIDEO", "A", "OBJECT", "EMBED"].includes(element.tagName) && INLINE_CONFLUENCE_VIDEOS.has(file.type);
        const replacement = doc.createElement(image ? "image-component" : video ? "video-component" : "a");
        if (image || video) {
          replacement.setAttribute("src", `${PROJECT_FILE_REF_PREFIX}${id}`);
          replacement.setAttribute("status", "uploaded");
          replacement.setAttribute("alignment", "center");
          for (const attr of ["width", "height"]) {
            const value = element.getAttribute(attr);
            if (value && /^\d+(?:px|%)?$/.test(value)) replacement.setAttribute(attr, value);
          }
        } else {
          replacement.setAttribute("href", `${base}/files?file=${id}`);
          replacement.textContent = element.textContent?.trim() || file.name;
        }
        replacements.set(element, replacement);
      }
      for (const [path, id] of uploaded) pageUrls.set(path, `${base}/files?file=${id}`);
      await pageService.update(options.workspaceSlug, options.projectId, pageId, {
        description_html: confluenceToPageHtml(page, replacements, pageUrls, report.warnings),
      });
      report.pagesImported += 1;
      report.pageIds.push(pageId);
    }
    // Import unreferenced attachments too. Source attachment folders carry the owning page id.
    for (const entry of entries.filter((candidate) => /(?:^|\/)attachments\//i.test(candidate.path))) {
      if (uploaded.has(entry.path) || failedUploads.has(entry.path)) continue;
      options.onProgress?.(`Importing attachment: ${entry.file.name}`);
      const sourceId = entry.path.match(/attachments\/(\d+)\//)?.[1];
      const owner = sourceId
        ? pages.find((page) => new RegExp(`(?:^|[_/])${sourceId}\\.html?$`, "i").test(page.path))
        : undefined;
      try {
        const result = await uploadProjectFile({
          workspaceSlug: options.workspaceSlug,
          projectId: options.projectId,
          file: confluenceFile(entry),
          link: owner ? { entity_type: "page", entity_id: pageIds.get(owner.path)! } : undefined,
        });
        uploaded.set(entry.path, result.file.id);
        report.filesUploaded += 1;
      } catch (error) {
        report.warnings.push(
          `${entry.path}: ${error instanceof Error ? error.message : (error as { error?: string })?.error || "Upload failed"}`
        );
      }
    }
    return report;
  } catch (error) {
    // Archive in reverse creation order. Uploaded Files remain available for recovery.
    for (const id of [...createdIds].reverse()) {
      try {
        await pageService.archive(options.workspaceSlug, options.projectId, id);
      } catch {
        /* Best effort; the original failure is more useful. */
      }
    }
    throw new Error(
      `${error instanceof Error ? error.message : (error as { error?: string })?.error || "Import failed"} Created pages were archived where possible; uploaded files remain in Files.`,
      { cause: error }
    );
  }
};
