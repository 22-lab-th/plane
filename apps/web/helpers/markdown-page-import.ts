import { EFileAssetType } from "@plane/types";
import type { TPage } from "@plane/types";
// services
import { FileService } from "@/services/file.service";
import { ProjectPageService } from "@/services/page/project-page.service";
// helpers
import {
  getMarkdownImageSources,
  getMarkdownPageName,
  isMarkdownImportEntry,
  isSupportedMarkdownImage,
  markdownToPageHtml,
  MAX_MARKDOWN_IMPORT_SIZE,
  readMarkdownImportSelection,
  resolveMarkdownImagePath,
  type TMarkdownImportFile,
} from "@/helpers/markdown-import";

/* oxlint-disable eslint/no-await-in-loop, eslint-plugin-unicorn/no-array-sort, eslint-plugin-unicorn/no-array-reverse -- Page creation, folder ancestry, asset ownership, and rollback are ordered; copied arrays are sorted/reversed without mutating source state. */

type TCreatePage = (data: Partial<TPage>) => Promise<TPage | undefined>;

export type TMarkdownPageImportReport = {
  pagesImported: number;
  assetsUploaded: number;
  warnings: string[];
  pageIds: string[];
};

type TMarkdownPageImportOptions = {
  files: File[];
  workspaceSlug: string;
  projectId: string;
  parentId: string | null;
  access: number;
  createPage: TCreatePage;
  createFolder: TCreatePage;
};

const escapeHtml = (value: string) =>
  value.replaceAll("&", "&amp;").replaceAll('"', "&quot;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");

const imageComponent = (source: string) =>
  `<image-component src="${escapeHtml(source)}" alignment="center" status="uploaded"></image-component>`;

const imagePlaceholder = (source: string) => `<p><em>Image not imported: ${escapeHtml(source)}</em></p>`;

const dirnameParts = (path: string) => {
  const parts = path.split("/");
  parts.pop();
  return parts;
};

export const importMarkdownPages = async (options: TMarkdownPageImportOptions): Promise<TMarkdownPageImportReport> => {
  const entries = await readMarkdownImportSelection(options.files);
  const markdownEntries = entries.filter(isMarkdownImportEntry).sort((a, b) => a.path.localeCompare(b.path));
  if (!markdownEntries.length) throw new Error("No Markdown files were found.");
  if (markdownEntries.some((entry) => entry.file.size > MAX_MARKDOWN_IMPORT_SIZE))
    throw new Error("Markdown files must be 5 MB or smaller.");

  const entriesByPath = new Map(entries.map((entry) => [entry.path, entry]));
  const fileService = new FileService();
  const pageService = new ProjectPageService();
  const folders = new Map<string, string | null>([["", options.parentId]]);
  const createdIds: string[] = [];
  const report: TMarkdownPageImportReport = {
    pagesImported: 0,
    assetsUploaded: 0,
    warnings: [],
    pageIds: [],
  };

  try {
    for (const markdownEntry of markdownEntries) {
      let folderPath = "";
      let currentParent = options.parentId;
      for (const folderName of dirnameParts(markdownEntry.path)) {
        folderPath = folderPath ? `${folderPath}/${folderName}` : folderName;
        if (!folders.has(folderPath)) {
          const folder = await options.createFolder({
            access: options.access,
            name: folderName,
            node_type: "folder",
            parent: currentParent,
          });
          if (!folder?.id) throw new Error(`Could not create folder ${folderPath}.`);
          folders.set(folderPath, folder.id);
          createdIds.push(folder.id);
        }
        currentParent = folders.get(folderPath) ?? options.parentId;
      }

      const page = await options.createPage({
        access: options.access,
        description_html: "<p></p>",
        name: getMarkdownPageName(markdownEntry.file.name),
        node_type: "page",
        parent: currentParent,
      });
      if (!page?.id) throw new Error(`Could not create Page for ${markdownEntry.path}.`);
      createdIds.push(page.id);

      const markdown = await markdownEntry.file.text();
      const imageHtmlBySource: Record<string, string> = {};
      for (const source of getMarkdownImageSources(markdown)) {
        if (/^https?:\/\//i.test(source)) {
          imageHtmlBySource[source] = imageComponent(source);
          report.warnings.push(`${markdownEntry.path}: kept remote image URL ${source}`);
          continue;
        }
        if (/^(?:data|file):/i.test(source)) {
          imageHtmlBySource[source] = imagePlaceholder(source);
          report.warnings.push(`${markdownEntry.path}: unsupported image URL ${source}`);
          continue;
        }

        let imageEntry: TMarkdownImportFile | undefined;
        try {
          imageEntry = entriesByPath.get(resolveMarkdownImagePath(markdownEntry.path, source));
        } catch (error) {
          imageHtmlBySource[source] = imagePlaceholder(source);
          report.warnings.push(`${markdownEntry.path}: ${error instanceof Error ? error.message : String(error)}`);
          continue;
        }
        if (!imageEntry) {
          imageHtmlBySource[source] = imagePlaceholder(source);
          report.warnings.push(`${markdownEntry.path}: image not found ${source}`);
          continue;
        }
        if (!(await isSupportedMarkdownImage(imageEntry))) {
          imageHtmlBySource[source] = imagePlaceholder(source);
          report.warnings.push(`${markdownEntry.path}: unsupported or oversized image ${source}`);
          continue;
        }

        try {
          const uploaded = await fileService.uploadProjectAsset(
            options.workspaceSlug,
            options.projectId,
            {
              entity_identifier: page.id,
              entity_type: EFileAssetType.PAGE_DESCRIPTION,
            },
            imageEntry.file
          );
          imageHtmlBySource[source] = imageComponent(uploaded.asset_id);
          report.assetsUploaded += 1;
        } catch (error) {
          imageHtmlBySource[source] = imagePlaceholder(source);
          report.warnings.push(
            `${markdownEntry.path}: failed to upload ${source}: ${error instanceof Error ? error.message : String(error)}`
          );
        }
      }

      await pageService.update(options.workspaceSlug, options.projectId, page.id, {
        description_html: markdownToPageHtml(markdown, imageHtmlBySource),
      });
      report.pagesImported += 1;
      report.pageIds.push(page.id);
    }
    return report;
  } catch (error) {
    for (const id of [...createdIds].reverse()) {
      try {
        await pageService.archive(options.workspaceSlug, options.projectId, id);
      } catch {
        // Best-effort rollback; preserve the original import error.
      }
    }
    throw error;
  }
};
