import { describe, expect, it, vi } from "vitest";
import { ProjectFileService } from "./project-file.service";

const folder = {
  id: "folder-1",
  name: "Design",
  parent_id: null,
  depth: 0,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

describe("ProjectFileService folder mutations", () => {
  it("creates a folder and returns the API folder payload", async () => {
    const service = new ProjectFileService();
    const post = vi.spyOn(service, "post").mockResolvedValue({ data: { folder } } as never);

    await expect(service.createProjectFolder("acme", "project-1", { name: "Design" })).resolves.toEqual(folder);
    expect(post).toHaveBeenCalledWith("/api/workspaces/acme/projects/project-1/files/folders/", { name: "Design" });
  });

  it("renames or moves a folder through the update endpoint", async () => {
    const service = new ProjectFileService();
    const patch = vi.spyOn(service, "patch").mockResolvedValue({ data: { folder } } as never);

    await expect(
      service.updateProjectFolder("acme", "project-1", "folder-1", { name: "Design Files", parent_id: "archive" })
    ).resolves.toEqual(folder);
    expect(patch).toHaveBeenCalledWith("/api/workspaces/acme/projects/project-1/files/folders/folder-1/", {
      name: "Design Files",
      parent_id: "archive",
    });
  });

  it("deletes a folder recursively when requested", async () => {
    const service = new ProjectFileService();
    const remove = vi.spyOn(service, "delete").mockResolvedValue(undefined as never);

    await expect(
      service.deleteProjectFolder("acme", "project-1", "folder-1", { recursive: true })
    ).resolves.toBeUndefined();
    expect(remove).toHaveBeenCalledWith(
      "/api/workspaces/acme/projects/project-1/files/folders/folder-1/?recursive=true"
    );
  });

  it("surfaces structured API errors instead of the transport wrapper", async () => {
    const service = new ProjectFileService();
    const apiError = { code: "folder_name_exists", error: "A folder with this name already exists." };
    vi.spyOn(service, "post").mockRejectedValue({ response: { data: apiError } });

    await expect(service.createProjectFolder("acme", "project-1", { name: "Design" })).rejects.toEqual(apiError);
  });
});
