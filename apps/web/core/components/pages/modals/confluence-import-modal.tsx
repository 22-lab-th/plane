import { useRef, useState } from "react";
import { Dialog } from "@headlessui/react";
import { CheckCircle2, CloudDownload, FileArchive, X } from "lucide-react";
import type { TPage } from "@plane/types";
import { Button } from "@plane/propel/button";
import { EModalPosition, EModalWidth, ModalCore } from "@plane/ui";
import { importConfluenceSpace } from "@/helpers/confluence-page-import";
import type { TConfluenceImportReport } from "@/helpers/confluence-page-import";
import { ConfluenceAPIImport } from "./confluence-api-import";

type Props = {
  isOpen: boolean;
  onClose: () => void;
  workspaceSlug: string;
  projectId: string;
  parentId: string | null;
  access: number;
  createPage: (data: Partial<TPage>) => Promise<TPage | undefined>;
  createFolder: (data: Partial<TPage>) => Promise<TPage | undefined>;
  onImported: () => Promise<unknown>;
};

export function ConfluenceImportModal(props: Props) {
  const [method, setMethod] = useState<"api" | "zip">("api");
  const [file, setFile] = useState<File | null>(null);
  const [progress, setProgress] = useState<string | null>(null);
  const [report, setReport] = useState<TConfluenceImportReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const input = useRef<HTMLInputElement>(null);
  const busy = progress !== null;
  const close = () => {
    if (busy) return;
    setFile(null);
    setReport(null);
    setError(null);
    if (input.current) input.current.value = "";
    props.onClose();
  };
  const start = async () => {
    if (!file || busy) return;
    setError(null);
    setReport(null);
    setProgress("Reading export");
    try {
      const result = await importConfluenceSpace({ ...props, file, onProgress: setProgress });
      setReport(result);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Confluence import failed.");
    } finally {
      await props.onImported().catch(() => undefined);
      setProgress(null);
    }
  };
  return (
    <ModalCore
      isOpen={props.isOpen}
      handleClose={close}
      position={EModalPosition.CENTER}
      width={EModalWidth.XXXXL}
      className="flex max-h-[calc(100dvh-2rem)] flex-col overflow-hidden"
    >
      <div className="flex shrink-0 items-start justify-between gap-4 border-b border-subtle px-5 py-4 sm:px-6">
        <div>
          <Dialog.Title as="h3" className="text-18 font-semibold text-primary">
            Import from Confluence
          </Dialog.Title>
          <Dialog.Description className="mt-1 text-13 text-tertiary">
            Bring your space’s pages and files into this project.
          </Dialog.Description>
        </div>
        <Button
          variant="ghost"
          size="xl"
          className="size-9 shrink-0 focus-visible:ring-2 focus-visible:ring-accent-strong"
          aria-label="Close import"
          onClick={close}
          disabled={busy}
        >
          <X className="size-4" aria-hidden="true" />
        </Button>
      </div>
      <div className="min-h-0 flex-1 space-y-6 overflow-y-auto px-5 py-5 sm:px-6">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2" role="group" aria-label="Import method">
          {(
            [
              {
                id: "api",
                title: "Connect to Confluence",
                description: "Import directly, track progress and sync updates.",
                Icon: CloudDownload,
              },
              {
                id: "zip",
                title: "Upload an HTML export",
                description: "Import once from a Confluence ZIP file.",
                Icon: FileArchive,
              },
            ] as const
          ).map(({ id, title, description, Icon }) => (
            <button
              key={id}
              type="button"
              aria-pressed={method === id}
              disabled={busy}
              onClick={() => setMethod(id)}
              className={`flex items-start gap-3 rounded-lg border p-3 text-left focus-visible:ring-2 focus-visible:ring-accent-strong focus-visible:outline-none disabled:opacity-50 ${method === id ? "border-accent-strong bg-accent-subtle" : "border-subtle hover:bg-surface-2"}`}
            >
              <Icon
                aria-hidden="true"
                className={`mt-0.5 size-5 shrink-0 ${method === id ? "text-accent-primary" : "text-tertiary"}`}
              />
              <span>
                <span className="block text-13 font-medium text-primary">{title}</span>
                <span className="mt-1 block text-12 text-tertiary">{description}</span>
              </span>
            </button>
          ))}
        </div>
        {method === "api" ? (
          props.isOpen && <ConfluenceAPIImport {...props} />
        ) : report ? (
          <>
            <div role="status" className="flex items-start gap-3 rounded-md bg-success-subtle p-4 text-success-primary">
              <CheckCircle2 aria-hidden="true" className="size-5 shrink-0" />
              <div>
                <p className="text-14 font-medium">Import complete</p>
                <p className="mt-1 text-13">
                  {report.pagesImported} pages and {report.filesUploaded} files imported.
                </p>
              </div>
            </div>
            <p className="text-13 text-tertiary">
              Pages are in a folder named after the space. Attachments are in Project Files.
            </p>
            {report.warnings.length > 0 && (
              <div>
                <p className="text-13 font-medium text-secondary">Import warnings</p>
                <ul className="mt-2 list-disc space-y-2 pl-5 text-13 [overflow-wrap:anywhere] text-tertiary">
                  {[...new Set(report.warnings)].map((warning) => (
                    <li key={warning}>{warning}</li>
                  ))}
                </ul>
              </div>
            )}
          </>
        ) : (
          <>
            <div className="space-y-4">
              <div className="space-y-2 text-13 text-secondary">
                <h4 className="text-14 font-medium text-primary">Upload your space export</h4>
                <ol className="list-decimal space-y-1 pl-5">
                  <li>In Confluence, export the space as HTML with attachments.</li>
                  <li>Choose the ZIP file below, then select Import space.</li>
                </ol>
                <p className="text-tertiary">
                  Images and videos are saved in Project Files and placed in their original page positions.
                </p>
              </div>
              <div className="space-y-3 rounded-lg border border-dashed border-strong bg-surface-2 p-5">
                <label htmlFor="confluence-export-file" className="block text-13 font-medium text-secondary">
                  Confluence HTML export (.zip)
                </label>
                <input
                  id="confluence-export-file"
                  ref={input}
                  type="file"
                  accept=".zip,application/zip"
                  disabled={busy}
                  aria-label="Confluence HTML space export"
                  onChange={(event) => setFile(event.target.files?.[0] ?? null)}
                  className="w-full min-w-0 text-13 text-secondary file:mr-3 file:cursor-pointer file:rounded-md file:border file:border-strong file:bg-surface-1 file:px-3 file:py-2 file:text-13 file:text-secondary focus-visible:outline-accent-strong"
                />
                <p className="text-12 text-tertiary">
                  ZIP up to 50 MB. Keep this window open until the import finishes.
                </p>
              </div>
              <details className="text-12 text-tertiary">
                <summary className="w-fit cursor-pointer py-2 font-medium text-secondary focus-visible:outline-accent-strong">
                  Export requirements
                </summary>
                <p className="mt-1">
                  Up to 100 MB extracted and 500 entries. Include remote images and videos in the export. MP4, WebM and
                  Ogg videos play in Pages; other formats open in Files.
                </p>
              </details>
              {progress && (
                <p role="status" aria-live="polite" className="rounded-md bg-accent-subtle p-3 text-13 text-secondary">
                  {progress}
                </p>
              )}
              {error && (
                <p
                  role="alert"
                  className="rounded-md bg-danger-subtle p-3 text-13 [overflow-wrap:anywhere] text-danger-primary"
                >
                  {error}
                </p>
              )}
            </div>
          </>
        )}
      </div>
      <div className="flex shrink-0 flex-wrap items-center justify-end gap-2 border-t border-subtle px-5 py-4 sm:px-6">
        {method === "api" && <p className="mr-auto text-12 text-tertiary">Imports continue in the background.</p>}
        <Button
          variant="secondary"
          size="xl"
          className="min-h-9 px-4 focus-visible:ring-2 focus-visible:ring-accent-strong"
          onClick={close}
          disabled={busy}
        >
          Close
        </Button>
        {method === "zip" && !report && (
          <Button
            variant="primary"
            size="xl"
            className="min-h-9 px-4 focus-visible:ring-2 focus-visible:ring-accent-strong"
            onClick={() => void start()}
            disabled={!file || busy}
            loading={busy}
          >
            {busy ? "Importing" : "Import space"}
          </Button>
        )}
      </div>
    </ModalCore>
  );
}
