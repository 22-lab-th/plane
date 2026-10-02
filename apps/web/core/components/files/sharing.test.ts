import { afterEach, describe, expect, it, vi } from "vitest";

import { buildFilesDeepLink, copyFilesDeepLink } from "./sharing";

const fileTarget = {
  workspaceSlug: "workspace",
  projectId: "project-1",
  folderId: "folder-1",
  fileId: "file-1",
} as const;

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("buildFilesDeepLink", () => {
  it("builds a file link with its containing folder", () => {
    expect(buildFilesDeepLink(fileTarget)).toBe("/workspace/projects/project-1/files/?folder=folder-1&file=file-1");
  });

  it("omits folder for a file at the project root", () => {
    expect(buildFilesDeepLink({ workspaceSlug: "workspace", projectId: "project-1", fileId: "file-1" })).toBe(
      "/workspace/projects/project-1/files/?file=file-1"
    );
  });

  it("builds a folder-only link", () => {
    expect(buildFilesDeepLink({ workspaceSlug: "workspace", projectId: "project-1", folderId: "folder-1" })).toBe(
      "/workspace/projects/project-1/files/?folder=folder-1"
    );
  });

  it("encodes path segments and query values", () => {
    expect(
      buildFilesDeepLink({
        workspaceSlug: "design team",
        projectId: "project/1",
        folderId: "folder 1",
        fileId: "file/1",
      })
    ).toBe("/design%20team/projects/project%2F1/files/?folder=folder+1&file=file%2F1");
  });
});

describe("copyFilesDeepLink", () => {
  it("copies the absolute authenticated route", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("window", { location: { origin: "https://plane.example" } });
    vi.stubGlobal("navigator", { clipboard: { writeText } });

    await copyFilesDeepLink(fileTarget);

    expect(writeText).toHaveBeenCalledOnce();
    expect(writeText).toHaveBeenCalledWith(
      "https://plane.example/workspace/projects/project-1/files/?folder=folder-1&file=file-1"
    );
  });

  it("propagates clipboard failures so the UI can show recovery feedback", async () => {
    const writeText = vi.fn().mockRejectedValue(new Error("Clipboard permission denied"));
    vi.stubGlobal("window", { location: { origin: "https://plane.example" } });
    vi.stubGlobal("navigator", { clipboard: { writeText } });

    await expect(copyFilesDeepLink(fileTarget)).rejects.toThrow("Clipboard permission denied");
  });
});
