import { useRef, useState } from "react";
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
    <ModalCore isOpen={props.isOpen} handleClose={close} position={EModalPosition.CENTER} width={EModalWidth.MD}>
      <div className="space-y-4 p-5">
        <h3 className="text-18 font-medium text-secondary">Import Confluence space</h3>
        <div className="flex gap-2" role="group" aria-label="Import method">
          <Button
            variant={method === "api" ? "primary" : "secondary"}
            size="sm"
            disabled={busy}
            onClick={() => setMethod("api")}
          >
            Atlassian API
          </Button>
          <Button
            variant={method === "zip" ? "primary" : "secondary"}
            size="sm"
            disabled={busy}
            onClick={() => setMethod("zip")}
          >
            HTML ZIP export
          </Button>
        </div>
        {method === "api" ? (
          props.isOpen && <ConfluenceAPIImport {...props} />
        ) : report ? (
          <>
            <p role="status" className="text-13 text-secondary">
              {report.pagesImported} pages and {report.filesUploaded} files imported.
            </p>
            <p className="text-13 text-tertiary">
              Pages are in a folder named after the space. Attachments are in Project Files.
            </p>
            {report.warnings.length > 0 && (
              <div>
                <p className="text-13 font-medium text-secondary">Import warnings</p>
                <ul className="max-h-64 list-disc space-y-1 overflow-y-auto pl-5 text-12 text-tertiary">
                  {[...new Set(report.warnings)].map((warning) => (
                    <li key={warning}>{warning}</li>
                  ))}
                </ul>
              </div>
            )}
          </>
        ) : (
          <>
            <p className="text-13 text-tertiary">
              Export your Confluence space as HTML with attachments, then choose the ZIP file. Images and videos are
              stored in Project Files and inserted at their original position in the page.
            </p>
            <p className="text-12 text-tertiary">
              Up to 50 MB ZIP, 100 MB extracted, 500 entries. Remote media must be included in the export. MP4, WebM and
              Ogg videos can play in Pages; other formats open in Files. Keep this window open while importing.
            </p>
            <input
              ref={input}
              type="file"
              accept=".zip,application/zip"
              disabled={busy}
              aria-label="Confluence HTML space export"
              onChange={(event) => setFile(event.target.files?.[0] ?? null)}
              className="w-full text-13 text-secondary"
            />
            {progress && (
              <p role="status" aria-live="polite" className="text-13 text-tertiary">
                {progress}
              </p>
            )}
            {error && (
              <p role="alert" className="text-13 text-danger-primary">
                {error}
              </p>
            )}
          </>
        )}
      </div>
      <div className="flex justify-end gap-2 border-t border-subtle px-5 py-4">
        <Button variant="secondary" size="lg" onClick={close} disabled={busy}>
          Close
        </Button>
        {method === "zip" && !report && (
          <Button variant="primary" size="lg" onClick={() => void start()} disabled={!file || busy} loading={busy}>
            {busy ? "Importing" : "Import space"}
          </Button>
        )}
      </div>
    </ModalCore>
  );
}
