// @vitest-environment happy-dom
import { act } from "react";
import { createRoot } from "react-dom/client";
import type { Root } from "react-dom/client";
import { SWRConfig } from "swr";
import { afterEach, describe, expect, it, vi } from "vitest";
import { JiraService } from "@plane/services";
import type { JiraRun, JiraRunDetail } from "@plane/services";
import { JiraAPIImport } from "./jira-import";

const counts = { total: 3, completed: 1, failed: 1, skipped: 1, pending: 0, running: 0 };
const run: JiraRun = {
  id: "run-1",
  source_id: "source-1",
  remote_project_id: "100",
  project_name: "Engineering",
  project_key: "ENG",
  mode: "changed",
  status: "partial",
  phase: "Finished with failures",
  inventory_complete: true,
  counts,
  types: { sprint: counts },
  error_code: "",
  error_message: "",
  created_at: "2026-09-30T00:00:00Z",
  updated_at: "2026-09-30T00:00:10Z",
  finished_at: "2026-09-30T00:00:10Z",
};
const detail: JiraRunDetail = {
  run,
  count: 1,
  next_offset: null,
  user_mapping: {},
  results: [
    {
      id: "item-1",
      remote_id: "att1",
      kind: "attachment",
      category: "image",
      title: "diagram.png",
      revision: "revision-2",
      status: "failed",
      error_code: "atlassian_http_403",
      error_message: "Attachment permission denied.",
      warning: "Unmapped Jira author",
      issue_id: null,
      cycle_id: null,
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

async function render(status: JiraRun["status"] = "partial", inventoryComplete = true) {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.spyOn(JiraService.prototype, "projects").mockResolvedValue({
    results: [{ id: "100", key: "ENG", name: "Engineering" }],
    next_offset: null,
    site_url: "https://team.atlassian.net",
  });
  vi.spyOn(JiraService.prototype, "runs").mockResolvedValue([{ ...run, status }]);
  vi.spyOn(JiraService.prototype, "detail").mockResolvedValue({
    ...detail,
    run: { ...run, status, inventory_complete: inventoryComplete },
  });
  vi.spyOn(JiraService.prototype, "members").mockResolvedValue([{ id: "member-1", name: "Test member" }]);
  const start = vi.spyOn(JiraService.prototype, "start").mockResolvedValue({ ...run, id: "run-2", status: "queued" });
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => {
    root?.render(
      <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0, shouldRetryOnError: false }}>
        <JiraAPIImport workspaceSlug="team" projectId="project-1" onImported={async () => undefined} />
      </SWRConfig>
    );
  });
  return start;
}

describe("Jira job progress and retry controls", () => {
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

  it("saves an explicit user mapping when syncing updated items", async () => {
    const start = await render();
    const input = container.querySelector<HTMLInputElement>('input[aria-label="Jira account ID"]');
    const member = container.querySelector<HTMLSelectElement>('select[aria-label="Plane project member"]');
    await act(async () => {
      const setValue = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
      setValue?.call(input, "hidden-jira-account");
      input?.dispatchEvent(new Event("input", { bubbles: true }));
      if (member) member.value = "member-1";
      member?.dispatchEvent(new Event("change", { bubbles: true }));
    });
    const add = [...container.querySelectorAll("button")].find((button) => button.textContent === "Add mapping");
    await act(async () => add?.click());
    expect(container.textContent).toContain("hidden-jira-account → Test member");
    const sync = [...container.querySelectorAll("button")].find((button) =>
      button.textContent?.includes("Sync new / updated items")
    );
    await act(async () => sync?.click());
    expect(start).toHaveBeenCalledWith("team", "project-1", {
      source_id: "source-1",
      mode: "changed",
      user_mapping: { "hidden-jira-account": "member-1" },
    });
  });

  it("reports incomplete inventory and explains sprint to cycle placement", async () => {
    await render("partial", false);
    expect(container.textContent).toContain("Inventory is incomplete");
    expect(container.textContent).toContain("Sprints become cycles");
    expect(container.textContent).toContain("sprint");
  });
});
