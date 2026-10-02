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

async function render(status: ConfluenceRun["status"] = "partial", overrides: Partial<ConfluenceRunDetail> = {}) {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.spyOn(ConfluenceService.prototype, "spaces").mockResolvedValue({
    results: [{ id: "100", key: "ENG", name: "Engineering" }],
    next_cursor: null,
    site_url: "https://team.atlassian.net",
  });
  vi.spyOn(ConfluenceService.prototype, "runs").mockResolvedValue([{ ...run, status }]);
  vi.spyOn(ConfluenceService.prototype, "detail").mockResolvedValue({
    ...detail,
    run: { ...run, status },
    ...overrides,
  });
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

function button(label: string) {
  const result = [...container.querySelectorAll("button")].find((element) => element.textContent?.trim() === label);
  expect(result, `Button ${label}`).toBeDefined();
  return result!;
}

describe("Confluence job progress and retry controls", () => {
  it("shows counts, failure reasons and retries selected items in the existing source", async () => {
    const start = await render();
    expect(container.textContent).toContain("3Total discovered");
    expect(container.textContent).toContain("1Completed");
    expect(container.textContent).toContain("1Failed");
    expect(container.textContent).toContain("Needs attention");
    expect(container.textContent).toContain("Attachment permission denied.");
    const checkbox = container.querySelector<HTMLInputElement>('input[aria-label="Retry diagram.png"]');
    expect(checkbox).not.toBeNull();
    await act(async () => checkbox?.click());
    await act(async () => button("Retry selected (1)").click());
    expect(start).toHaveBeenCalledWith("team", "project-1", {
      source_id: "source-1",
      mode: "selected",
      item_ids: ["item-1"],
    });
  });

  it("disables reruns while the worker is running", async () => {
    const start = await render("running");
    expect(container.textContent).toContain("Import is running in the background");
    expect(container.querySelector<HTMLInputElement>('input[aria-label="Retry diagram.png"]')?.disabled).toBe(true);
    const space = container.querySelector<HTMLSelectElement>("#confluence-space")!;
    await act(async () => {
      space.value = "100";
      space.dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(button("Import space").disabled).toBe(true);
    expect(
      [...container.querySelectorAll("button")].some((element) => element.textContent?.includes("Re-import all"))
    ).toBe(false);
    button("Import space").click();
    expect(start).not.toHaveBeenCalled();
  });

  it("imports the chosen space with the current destination and access", async () => {
    const start = await render();
    expect(button("Import space").disabled).toBe(true);
    const space = container.querySelector<HTMLSelectElement>("#confluence-space")!;
    await act(async () => {
      space.value = "100";
      space.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await act(async () => button("Import space").click());
    expect(start).toHaveBeenCalledWith("team", "project-1", {
      space_id: "100",
      parent_id: null,
      access: 0,
      mode: "changed",
    });
  });

  it("requires an explicit confirmation before overwriting imported content", async () => {
    const start = await render();
    await act(async () => button("Re-import all…").click());
    expect(container.textContent).toContain("Edits made in Plane will be overwritten");
    expect(document.activeElement).toBe(button("Cancel"));
    expect(start).not.toHaveBeenCalled();
    await act(async () => button("Cancel").click());
    expect(document.activeElement).toBe(button("Re-import all…"));
    expect(start).not.toHaveBeenCalled();
    await act(async () => button("Re-import all…").click());
    await act(async () => button("Confirm overwrite").click());
    expect(start).toHaveBeenCalledWith("team", "project-1", { source_id: "source-1", mode: "all" });
  });

  it("filters failed results without losing progress and clears selected items", async () => {
    await render();
    await act(async () => container.querySelector<HTMLInputElement>('input[aria-label="Retry diagram.png"]')?.click());
    expect(container.textContent).toContain("1 selected");
    await act(async () => button("Failed (1)").click());
    expect(ConfluenceService.prototype.detail).toHaveBeenCalledWith("team", "project-1", "run-1", 0, "failed");
    expect(button("Failed (1)").getAttribute("aria-pressed")).toBe("true");
    expect(container.textContent).toContain("3Total discovered");
    await act(async () => button("Clear selection").click());
    expect(
      [...container.querySelectorAll("button")].some((element) => element.textContent?.includes("Retry selected"))
    ).toBe(false);
    await act(async () => button("All items").click());
    expect(ConfluenceService.prototype.detail).toHaveBeenLastCalledWith("team", "project-1", "run-1", 0, undefined);
  });

  it("syncs updates against the existing source", async () => {
    const start = await render();
    await act(async () => button("Sync updates").click());
    expect(start).toHaveBeenCalledWith("team", "project-1", { source_id: "source-1", mode: "changed" });
  });

  it("retries all failures against the existing source", async () => {
    const start = await render();
    await act(async () => button("Retry failed (1)").click());
    expect(start).toHaveBeenCalledWith("team", "project-1", { source_id: "source-1", mode: "failed" });
  });

  it("keeps the original media failure details alongside recovery guidance", async () => {
    const message = "Page media not available in Plane Files: /wiki/images/missing.png";
    await render("partial", {
      results: [
        {
          ...detail.results[0],
          kind: "page",
          category: "page",
          error_code: "media_dependency_failed",
          error_message: message,
        },
      ],
    });
    expect(container.textContent).toContain("Retry the failed files, then retry this page.");
    const errorDetails = [...container.querySelectorAll("details")].find(
      (element) => element.querySelector("summary")?.textContent === "Error details"
    );
    expect(errorDetails?.textContent).toContain(message);
    expect(errorDetails?.textContent).toContain("media_dependency_failed");
  });

  it("does not show a final percentage while discovery is incomplete", async () => {
    await render("discovering", { run: { ...run, status: "discovering", inventory_complete: false } });
    expect(container.textContent).toContain("counts reflect items discovered so far");
    expect(container.querySelector('[role="progressbar"]')).toBeNull();
  });
});
