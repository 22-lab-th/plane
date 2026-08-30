import MarkdownIt from "markdown-it";

export const MAX_MARKDOWN_IMPORT_SIZE = 5 * 1024 * 1024;

const markdownParser = new MarkdownIt({
  breaks: false,
  html: false,
  linkify: true,
  typographer: false,
});

export const isMarkdownFile = (file: File) => {
  const extension = file.name.split(".").pop()?.toLowerCase();
  return extension === "md" || extension === "markdown";
};

export const getMarkdownPageName = (fileName: string) => {
  const name = fileName.replace(/\.(?:md|markdown)$/i, "").trim();
  return (name || "Imported page").slice(0, 255);
};

export const markdownToPageHtml = (markdown: string) => {
  const normalizedMarkdown = markdown.replace(/^\uFEFF/, "").replace(/\r\n?/g, "\n");
  return normalizedMarkdown.trim() ? markdownParser.render(normalizedMarkdown) : "<p></p>";
};
