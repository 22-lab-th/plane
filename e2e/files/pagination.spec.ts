/* oxlint-disable no-await-in-loop */
import { expect, test } from "@playwright/test";
import {
  APP_FILES_URL,
  OWNER_EMAIL,
  OWNER_PASSWORD,
  apiWrite,
  createFolder,
  getListBody,
  listUrl,
  openFilesTab,
  signIn,
} from "./support";

test("all 51 files are reachable and changing filters resets the cursor", async ({ page }) => {
  await signIn(page, OWNER_EMAIL, OWNER_PASSWORD);
  const source = (await getListBody(page, { folder_id: "root" })).results[0];
  expect(source).toBeDefined();
  const folderId = await createFolder(page, `Pagination-${Date.now()}`);
  // Real copies preserve the upload/storage lifecycle and exercise the real
  // paginator. Give the default per-project rate budget a fresh minute first.
  await page.waitForTimeout(61_000);
  const ids: string[] = [];
  for (let index = 0; index < 51; index += 1) {
    const response = await apiWrite(page, "post", `${listUrl()}${source.id}/copy/`, {
      folder_id: folderId,
      name_display: `Page-${String(index).padStart(2, "0")}.pdf`,
    });
    ids.push((await response.json()).file.id);
  }
  await openFilesTab(page, `${APP_FILES_URL}?folder=${folderId}`);
  const nav = page.getByRole("navigation", { name: "File pages" });
  await expect(nav.getByRole("button", { name: "Previous", exact: true })).toBeDisabled();
  await expect(nav.getByRole("button", { name: "Next", exact: true })).toBeEnabled();
  const first = await getListBody(page, { folder_id: folderId });
  expect(first.results).toHaveLength(50);
  await nav.getByRole("button", { name: "Next", exact: true }).click();
  await expect.poll(() => new URL(page.url()).searchParams.get("cursor")).toBe(first.page.next_cursor);
  const second = await getListBody(page, { folder_id: folderId, cursor: first.page.next_cursor! });
  expect(second.results).toHaveLength(1);
  await expect(page.getByText(second.results[0].name_display, { exact: true })).toBeVisible();
  await expect(nav.getByRole("button", { name: "Next", exact: true })).toBeDisabled();
  await expect(nav.getByRole("button", { name: "Previous", exact: true })).toBeEnabled();
  expect(new Set([...first.results, ...second.results].map((file) => file.id))).toEqual(new Set(ids));
  await nav.getByRole("button", { name: "Previous", exact: true }).click();
  await expect(page.getByText(first.results[0].name_display, { exact: true })).toBeVisible();
  await nav.getByRole("button", { name: "Next", exact: true }).click();
  await page.getByTestId("files-quick-pinned").click();
  await expect.poll(() => new URL(page.url()).searchParams.get("cursor")).toBeNull();
  await expect(page.getByTestId("files-state-no-match")).toBeVisible();
});
