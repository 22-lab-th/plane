import { describe, expect, it } from "vitest";
import type { IProjectFolder } from "@/services/project-file.service";
import { getFolderMoveDestinations } from "./folder-management";

const makeFolder = (id: string, name: string, parent_id: string | null, depth: number): IProjectFolder => ({
  id,
  name,
  parent_id,
  depth,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
});

describe("getFolderMoveDestinations", () => {
  it("excludes the moving folder and every descendant from destinations", () => {
    const folders = [
      makeFolder("moving", "Moving", null, 0),
      makeFolder("child", "Child", "moving", 1),
      makeFolder("grandchild", "Grandchild", "child", 2),
      makeFolder("sibling", "Sibling", null, 0),
    ];

    expect(getFolderMoveDestinations(folders, "moving").map((folder) => folder.id)).toEqual(["sibling"]);
  });

  it("orders valid destinations by depth and then name", () => {
    const folders = [
      makeFolder("deep", "Deep", "parent", 2),
      makeFolder("zulu", "Zulu", null, 0),
      makeFolder("parent", "Parent", null, 0),
      makeFolder("alpha", "Alpha", null, 0),
    ];

    expect(getFolderMoveDestinations(folders, "moving").map((folder) => folder.id)).toEqual([
      "alpha",
      "parent",
      "zulu",
      "deep",
    ]);
  });
});
