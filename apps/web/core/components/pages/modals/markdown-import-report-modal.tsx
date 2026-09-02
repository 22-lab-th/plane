/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { Button } from "@plane/propel/button";
import { EModalPosition, EModalWidth, ModalCore } from "@plane/ui";
// helpers
import type { TMarkdownPageImportReport } from "@/helpers/markdown-page-import";

type Props = {
  report: TMarkdownPageImportReport | null;
  onClose: () => void;
  onOpenPage: (pageId: string) => void;
};

export function MarkdownImportReportModal({ report, onClose, onOpenPage }: Props) {
  const warnings = report ? [...new Set(report.warnings)] : [];
  return (
    <ModalCore isOpen={Boolean(report)} handleClose={onClose} position={EModalPosition.CENTER} width={EModalWidth.SM}>
      <div>
        <div className="space-y-4 p-5">
          <div>
            <h3 className="text-18 font-medium text-secondary">Markdown import complete</h3>
            <p className="mt-1 text-13 text-tertiary">
              {report?.pagesImported ?? 0} pages and {report?.assetsUploaded ?? 0} images were imported.
            </p>
          </div>
          {warnings.length ? (
            <div className="space-y-2">
              <p className="text-13 font-medium text-secondary">
                {warnings.length} warning{warnings.length === 1 ? "" : "s"}
              </p>
              <ul className="max-h-56 list-disc space-y-1 overflow-y-auto pl-5 text-12 text-tertiary">
                {warnings.map((warning) => (
                  <li key={warning}>{warning}</li>
                ))}
              </ul>
            </div>
          ) : (
            <p className="text-13 text-success-primary">All referenced local images were stored as Page assets.</p>
          )}
        </div>
        <div className="flex items-center justify-end gap-2 border-t-[0.5px] border-subtle px-5 py-4">
          <Button variant="secondary" size="lg" onClick={onClose}>
            Close
          </Button>
          {report?.pageIds.length === 1 && (
            <Button variant="primary" size="lg" onClick={() => onOpenPage(report.pageIds[0])}>
              Open page
            </Button>
          )}
        </div>
      </div>
    </ModalCore>
  );
}
