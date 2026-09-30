// @vitest-environment happy-dom
import { act } from "react";
import { createRoot } from "react-dom/client";
import type { Root } from "react-dom/client";
import { SWRConfig } from "swr";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ConfluenceService } from "@plane/services";
import type { ConfluenceRun, ConfluenceRunDetail } from "@plane/services";
import { ConfluenceAPIImport } from "./confluence-api-import";

const counts = { total: 3, completed: 1, failed: 1, skipped: 1, pending: 0, running: 0 };
const run: ConfluenceRun = {
  id: "run-1",
  source_id: "source-1",
  space_id: "100",
  space_name: "Engineering",
  mode: "changed",
  status: "partial",
  phase: "Finished with failures",
  inventory_complete: true,
  counts,
  types: { page: counts },
  error_code: "",
  error_message: "",
  created_at: "2026-09-30T00:00:00Z",
  updated_at: "2026-09-30T00:00:10Z",
  finished_at: "2026-09-30T00:00:10Z",
};
const detail: ConfluenceRunDetail = {
  run,
  count: 1,
  next_offset: null,
  results: [
    {
      id: "item-1",
      remote_id: "att1",
      kind: "attachment",
      category: "image",
      title: "diagram.png",
      version: 2,
      status: "failed",
      error_code: "atlassian_http_403",
      error_message: "Attachment permission denied.",
      page_id: null,
      file_id: null,
    },
  ],
};
let root: Root | undefined;
let container: HTMLDivElement;

afterEach(async () => {
  if (root) await act(async () => root?.unmount());
  container?.remove();
  root = undefined;
  vi.restoreAllMocks();
});

async function render(status: ConfluenceRun["status"] = "partial") {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.spyOn(ConfluenceService.prototype, "spaces").mockResolvedValue({
    results: [{ id: "100", key: "ENG", name: "Engineering" }],
    next_cursor: null,
    site_url: "https://team.atlassian.net",
  });
  vi.spyOn(ConfluenceService.prototype, "runs").mockResolvedValue([{ ...run, status }]);
  vi.spyOn(ConfluenceService.prototype, "detail").mockResolvedValue({ ...detail, run: { ...run, status } });
  const start = vi
    .spyOn(ConfluenceService.prototype, "start")
    .mockResolvedValue({ ...run, id: "run-2", status: "queued" });
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => {
    root?.render(
      <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0, shouldRetryOnError: false }}>
        <ConfluenceAPIImport
          workspaceSlug="team"
          projectId="project-1"
          parentId={null}
          access={0}
          onImported={async () => undefined}
        />
      </SWRConfig>
    );
  });
  return start;
}

describe("Confluence job progress and retry controls", () => {
  it("shows counts, failure reasons and retries selected items in the existing source", async () => {
    const start = await render();
    expect(container.textContent).toContain("3 discovered · 1 completed · 1 failed");
    expect(container.textContent).toContain("Attachment permission denied.");
    const checkbox = container.querySelector<HTMLInputElement>('input[aria-label="Retry diagram.png"]');
    expect(checkbox).not.toBeNull();
    await act(async () => checkbox?.click());
    const retry = [...container.querySelectorAll("button")].find((button) =>
      button.textContent?.includes("Retry selected (1)")
    );
    await act(async () => retry?.click());
    expect(start).toHaveBeenCalledWith("team", "project-1", {
      source_id: "source-1",
      mode: "selected",
      item_ids: ["item-1"],
    });
  });

  it("disables reruns while the worker is running", async () => {
    const start = await render("running");
    const overwrite = [...container.querySelectorAll("button")].find((button) =>
      button.textContent?.includes("Re-import all")
    );
    expect(overwrite?.disabled).toBe(true);
    overwrite?.click();
    expect(start).not.toHaveBeenCalled();
  });
});
