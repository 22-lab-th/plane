/**
 * T-112 — the Files tab: browsing, breadcrumbs, quick views and states.
 *
 * The harness this file runs on — the API base, the session, the live-response capture
 * and the `files-*` test ids — lives in `./support`, shared with `upload.spec.ts` (T-113).
 * Every assertion here compares the rendered DOM against the API response this test
 * consumed (ADV-001 §4.1/§4.2); `support.ts` states that rule in full.
 *
 * The run is single-worker (`workers: 1` in `playwright.files.config.ts`): the tests
 * share one project fixture and this file must not opt into parallel mode.
 */

// A browser is driven one step at a time: the loops below must await each step, and
// the retry helpers exist precisely to observe sequential state.
/* oxlint-disable no-await-in-loop */

import { expect, test } from "@playwright/test";
import {
  apiWrite,
  API_URL,
  APP_FILES_URL,
  createFolder,
  expectRowsMatchBody,
  expectStorageMatchesBody,
  expectViewMatchesBody,
  focusedFileId,
  getListBody,
  GUEST_EMAIL,
  GUEST_PASSWORD,
  isDetailResponse,
  isListUrl,
  listUrl,
  mutatedFileIds,
  openFilesTab,
  OWNER_EMAIL,
  OWNER_PASSWORD,
  readFocusDecoration,
  readRows,
  ROW_SELECTOR,
  signIn,
  snapshotList,
  tabUntilRowFocused,
  WEB_URL,
  WORKSPACE_SLUG,
  type TFileRow,
  type TFolderRow,
} from "./support";

/** The query a captured list URL carries, as the params `getListBody` re-issues it. */
function paramsOf(url: string): Record<string, string> {
  const params: Record<string, string> = {};
  new URL(url).searchParams.forEach((value, key) => {
    params[key] = value;
  });
  return params;
}

// --- the specs --------------------------------------------------------------

test.describe("Project files tab (T-112)", () => {
  test("browse_breadcrumbs_filters_and_states_match_the_api", async ({ page }) => {
    await signIn(page, OWNER_EMAIL, OWNER_PASSWORD);

    // The dev server compiles the Files route on its first hit and the app boots its
    // workspace and project stores before the view mounts, so the first navigation of a
    // run is far slower than any measured transition (observed well past the live
    // window on a cold server). Prime with one unmeasured visit so the measured root
    // snapshot is a warm, live fetch rather than a race with the compiler.
    await openFilesTab(page);
    await expect(page.getByTestId("files-root"), "the primed view is on screen before the measurement").toBeVisible();

    // The default view at the project root ("All", no filter, default ordering).
    const root = await snapshotList(page, "root-all", () => openFilesTab(page), { requireLive: true });
    await expectViewMatchesBody(page, root.live, "root-all");
    expect(root.live.breadcrumbs, "the root has no breadcrumb above it").toEqual([]);
    await expect(page.getByTestId("files-state-empty")).toBeHidden();
    await expect(page.getByTestId("files-state-loading")).toBeHidden();
    // DEC-003 / REQ-001 AC-21: Orphan (Unlinked) is P2, so the API has no filter for it
    // and the view must not offer a quick view that cannot be expressed.
    await expect(page.getByTestId("files-quick-orphan")).toHaveCount(0);
    expect(
      root.live.results.length,
      "the harness must seed more than one file so ordering and ArrowDown are observable"
    ).toBeGreaterThanOrEqual(2);
    // DEFECT-005: the root view asks for the project root's own files. The harness
    // seeds files into folders too, so a root request that omits `folder_id` - which
    // the endpoint reads as "every file in the project, at any depth" - brings those
    // folder files back with it and this fails.
    expect(
      root.live.results.filter((row) => row.folder_id !== null),
      "the root listing carries only the files that live at the root"
    ).toEqual([]);

    // Recent: the view's "last 30 days" window reaches the API as created_from.
    const recent = await snapshotList(page, "recent", () => page.getByTestId("files-quick-recent").click());
    expect(new URL(page.url()).searchParams.get("view"), "the recent quick view is a URL state").toBe("recent");
    const recentWindowStart = new URL(recent.url).searchParams.get("created_from") ?? "";
    const recentWindowDays = Math.round((Date.now() - Date.parse(`${recentWindowStart}T00:00:00`)) / 86_400_000);
    expect(
      Math.abs(recentWindowDays - 30) <= 1,
      `the recent quick view asks for the last 30 days, and it sent created_from=${recentWindowStart}`
    ).toBe(true);
    await expectRowsMatchBody(page, recent.live, "recent");
    await expectStorageMatchesBody(page, recent.live, "recent");
    expect(recent.live.results.length, "the seeded files are recent").toBeGreaterThan(0);

    // Pinned: the state is created through the API, then read back through the view.
    const pinTarget = recent.live.results[0];
    expect(pinTarget, "the recent view must have a row to pin").toBeDefined();
    mutatedFileIds.add(pinTarget.id);
    await apiWrite(page, "patch", `${listUrl()}${pinTarget.id}/`, { is_pinned: true });

    const pinned = await snapshotList(page, "pinned", () => page.getByTestId("files-quick-pinned").click());
    const pinnedQuery = new URL(pinned.url).searchParams;
    expect(new URL(page.url()).searchParams.get("view"), "the pinned quick view is a URL state").toBe("pinned");
    expect(pinnedQuery.get("pinned"), "the pinned quick view maps onto the API's pinned filter").toBe("true");
    expect(
      pinned.live.results.map((row) => row.id),
      "exactly the pinned file"
    ).toEqual([pinTarget.id]);
    expect(
      (await getListBody(page, { pinned: "true" })).results.map((row) => row.id),
      "?pinned=true agrees"
    ).toEqual([pinTarget.id]);
    await expectViewMatchesBody(page, pinned.live, "pinned");
    await apiWrite(page, "patch", `${listUrl()}${pinTarget.id}/`, { is_pinned: false });

    // Trash: exactly the rows the API returns for ?trashed=true, no more and no fewer.
    const trash = await snapshotList(page, "trash", () => page.getByTestId("files-quick-trash").click());
    const trashQuery = new URL(trash.url).searchParams;
    expect(new URL(page.url()).searchParams.get("view"), "the trash quick view is a URL state").toBe("trash");
    expect(trashQuery.get("trashed"), "the trash quick view maps onto the API's trashed filter").toBe("true");
    // The trash view is project-wide: a file trashed through its folder keeps a `folder_id`
    // whose folder no longer resolves, so a folder scope here would drop exactly the rows
    // R-DEL-3's restore surface exists for (T-118 F-1).
    expect(
      trashQuery.get("folder_id"),
      "the trash view asks for no folder, so every trashed row is reachable"
    ).toBeNull();
    expect(
      trash.live.results.length,
      "the harness leaves one file in the trash so this assertion is not vacuous"
    ).toBeGreaterThan(0);
    expect(
      trash.live.results.every((row) => row.trashed),
      "every trashed row is marked trashed"
    ).toBe(true);
    expect(
      root.live.results.filter((row) => row.trashed),
      "the default view must not show a trashed row"
    ).toEqual([]);
    await expectViewMatchesBody(page, trash.live, "trash");

    // The query the view actually issued, re-fetched: the assertion is against the view's
    // own request rather than a hand-written one that only agrees while the fixture's
    // trashed file happens to live at the root (T-118 F-2).
    const trashReference = await getListBody(page, paramsOf(trash.url));
    const trashReferenceIds = new Set(trashReference.results.map((row) => row.id));
    expect(new Set(trash.live.results.map((row) => row.id)), "the view asked the API for the trash").toEqual(
      trashReferenceIds
    );
    expect(
      new Set((await readRows(page)).map((row) => row.fileId)),
      "the DOM shows exactly the API's trashed rows"
    ).toEqual(trashReferenceIds);
    expect(
      trash.live.storage.project_used_bytes,
      "the indicator is the project's usage from this response, not a trash-filtered count"
    ).toBe(root.live.storage.project_used_bytes);

    // A file trashed *through its folder* keeps a `folder_id` whose folder no longer
    // resolves, and it must still be found here: the row is unrestorable from the UI if
    // this view loses it (T-118 F-1, the defect this block exists to catch). Created,
    // trashed and read back entirely through the API and the view's own request.
    const doomedFolderId = await createFolder(page, `Trashed Folder ${Date.now()}`);
    const doomedName = `folder-trashed-${Date.now()}.txt`;
    const doomedBytes = Buffer.from("a file whose folder is about to be trashed\n", "ascii");
    const initiated = await apiWrite(page, "post", `${listUrl()}initiate-upload/`, {
      file_name: doomedName,
      size_bytes: doomedBytes.length,
      mime_type: "text/plain",
      folder_id: doomedFolderId,
    });
    const initiation = (await initiated.json()) as {
      file: { id: string };
      version_no: number;
      upload: { url: string; headers: Record<string, string> };
    };
    const put = await page.request.put(initiation.upload.url, {
      data: doomedBytes,
      headers: initiation.upload.headers,
    });
    expect(put.status(), `PUT ${doomedName} to the store the presign named`).toBe(200);
    const finalized = await apiWrite(page, "post", `${listUrl()}${initiation.file.id}/complete-upload/`, {
      version_no: initiation.version_no,
      size_bytes: doomedBytes.length,
    });
    expect(finalized.status(), `the API verified ${doomedName}`).toBe(200);

    const folderTrashed = await apiWrite(page, "delete", `${listUrl()}folders/${doomedFolderId}/?recursive=true`);
    expect(folderTrashed.status(), "deleting the folder moves its file to the trash").toBe(204);

    // Back to All first, so the Trash click below is a real transition that refetches:
    // a repeat of the same key would leave the DOM on the rows fetched before the folder
    // was trashed, and comparing that stale DOM against a fresh body is not the claim.
    await snapshotList(page, "all-after-folder-trash", () => page.getByTestId("files-quick-all").click());
    const trashProjectWide = await snapshotList(page, "trash-project-wide", () =>
      page.getByTestId("files-quick-trash").click()
    );
    expect(
      new URL(trashProjectWide.url).searchParams.get("folder_id"),
      "the trash view still asks for no folder after the folder it was browsing is gone"
    ).toBeNull();
    const orphanRow = trashProjectWide.live.results.find((row) => row.id === initiation.file.id);
    expect(orphanRow, "a file trashed through its folder is on the trash surface").toBeDefined();
    expect(
      (orphanRow as TFileRow).folder_id,
      "and its row still names the folder that no longer resolves, which is why the scope matters"
    ).toBe(doomedFolderId);
    expect(
      (await getListBody(page, paramsOf(trashProjectWide.url))).results.map((row) => row.id),
      "the project-wide trash request the view issued carries that row"
    ).toContain(initiation.file.id);
    await expectViewMatchesBody(page, trashProjectWide.live, "trash-project-wide");

    // Back to All: the default view again, with the same rows.
    const all = await snapshotList(page, "back-to-all", () => page.getByTestId("files-quick-all").click());
    await expectViewMatchesBody(page, all.live, "back-to-all");
    expect(new Set(all.live.results.map((row) => row.id)), "All is the unfiltered view again").toEqual(
      new Set(root.live.results.map((row) => row.id))
    );

    // A search that does match: the term comes from the response, so the rows the
    // API returns for it are the only fixture.
    const searchToken = (all.live.results[0]?.name_display ?? "").slice(0, 4);
    expect(searchToken, "the root view must have a row to search for").not.toBe("");
    const matched = await snapshotList(page, "search-match", () => page.getByTestId("files-search").fill(searchToken));
    expect(new URL(matched.url).searchParams.get("q"), "the search reaches the API's q filter").toBe(searchToken);
    expect(matched.live.results.length, "the seeded names contain the term the response supplied").toBeGreaterThan(0);
    expect(
      matched.live.results.every((row) => row.name_display.toLowerCase().includes(searchToken.toLowerCase())),
      "q matches on the display name"
    ).toBe(true);
    await expectRowsMatchBody(page, matched.live, "search-match");

    const searchReset = await snapshotList(page, "search-reset", () => page.getByTestId("files-search").fill(""));
    expect(new URL(searchReset.url).searchParams.get("q"), "emptying the box drops the filter").toBeNull();
    await expectRowsMatchBody(page, searchReset.live, "search-reset");
    expect(new Set(searchReset.live.results.map((row) => row.id)), "emptying the box restores the list").toEqual(
      new Set(root.live.results.map((row) => row.id))
    );

    // Empty folder: a folder this test creates through the API, then opens in the view.
    const emptyFolderId = await createFolder(page, `Empty Folder ${Date.now()}`);
    const withEmptyFolder = await snapshotList(page, "root-with-empty-folder", () => page.goto(APP_FILES_URL));
    expect(
      withEmptyFolder.live.folders.map((folder) => folder.id),
      "the created folder is in the root listing the API returns"
    ).toContain(emptyFolderId);

    const empty = await snapshotList(page, "empty-folder", () =>
      page.getByTestId(`files-folder-${emptyFolderId}`).click()
    );
    expect(new URL(empty.url).searchParams.get("folder_id"), "the folder becomes the API's folder_id filter").toBe(
      emptyFolderId
    );
    expect(empty.live.results, "the folder the API just told us about has no files").toEqual([]);
    expect(empty.live.folders, "and no child folders").toEqual([]);
    expect(
      empty.live.breadcrumbs.map((crumb) => crumb.id),
      "its own breadcrumb"
    ).toEqual([emptyFolderId]);
    await expectViewMatchesBody(page, empty.live, "empty-folder");
    await expect(page.getByTestId("files-state-empty")).toBeVisible();
    await expect(page.getByTestId("files-state-empty")).toContainText("This folder is empty.");
    await expect(page.locator(ROW_SELECTOR)).toHaveCount(0);

    // No match at the root: `q` filters files only, so the root keeps its folders while
    // the file list comes back empty. The state under test is that the *file list*
    // reports the miss while the folders stay on screen — an empty folder is not
    // required, and requiring one would leave the real path uncovered (DESIGN §7).
    const noMatchToken = `zz-no-match-${Date.now()}`;
    await page.goto(APP_FILES_URL);
    await expect(page.getByTestId("files-root")).toBeVisible();
    await expect(page.locator(ROW_SELECTOR).first()).toBeVisible();

    const noMatch = await snapshotList(page, "search-no-match-root", () =>
      page.getByTestId("files-search").fill(noMatchToken)
    );
    expect(new URL(noMatch.url).searchParams.get("q"), "the search reaches the API's q filter").toBe(noMatchToken);
    expect(
      new URL(noMatch.url).searchParams.get("folder_id"),
      "the root request asks for the root folder explicitly, not for every folder"
    ).toBe("root");
    expect(noMatch.live.results, "the API answers an empty page for this query").toEqual([]);
    expect(
      noMatch.live.folders.length,
      "the root still has folders, so the no-match state cannot depend on an empty folder list"
    ).toBeGreaterThan(0);

    await expect(page.getByTestId("files-state-no-match")).toBeVisible();
    await expect(page.getByTestId("files-state-no-match")).toContainText("No files match your filters.");
    await expect(page.getByTestId("files-clear-filters")).toBeVisible();
    await expect(page.locator(ROW_SELECTOR)).toHaveCount(0);
    expect(
      await page.locator('[data-testid^="files-folder-"]').count(),
      "the folders stay visible while the file list reports the miss"
    ).toBeGreaterThan(0);

    const cleared = await snapshotList(page, "search-cleared-root", () =>
      page.getByTestId("files-clear-filters").click()
    );
    expect(new URL(cleared.url).searchParams.get("q"), "clearing drops the query from the request").toBeNull();
    await expect(page.getByTestId("files-state-no-match")).toBeHidden();
    await expect(page.getByTestId("files-search")).toHaveValue("");
    await expectViewMatchesBody(page, cleared.live, "search-cleared-root");
    expect(new Set(cleared.live.results.map((row) => row.id)), "clearing the filters restores the root list").toEqual(
      new Set(root.live.results.map((row) => row.id))
    );

    // Back to the root from the empty folder, then one folder down, one level
    // deeper, and back up through the breadcrumbs.
    const backAtRootFromEmpty = await snapshotList(page, "crumb-root-from-empty", () =>
      page.getByTestId("files-breadcrumb-root").click()
    );
    expect(backAtRootFromEmpty.live.breadcrumbs, "the root crumb walks out of the empty folder").toEqual([]);
    expect(new URL(backAtRootFromEmpty.url).searchParams.get("folder_id")).toBe("root");
    await expectViewMatchesBody(page, backAtRootFromEmpty.live, "crumb-root-from-empty");

    const outerFolder = withEmptyFolder.live.folders.find((folder) => folder.id !== emptyFolderId);
    expect(outerFolder, "the harness must seed a folder besides the empty one this test creates").toBeDefined();
    const outerFolderId = (outerFolder as TFolderRow).id;

    const outerView = await snapshotList(page, "folder-outer", () =>
      page.getByTestId(`files-folder-${outerFolderId}`).click()
    );
    expect(new URL(outerView.url).searchParams.get("folder_id")).toBe(outerFolderId);
    expect(
      outerView.live.breadcrumbs.map((crumb) => crumb.id),
      "one crumb below the root"
    ).toEqual([outerFolderId]);
    await expectViewMatchesBody(page, outerView.live, "folder-outer");

    const innerFolder = outerView.live.folders[0];
    expect(innerFolder, "the harness seeds a nested folder for the two-level walk").toBeDefined();
    const innerFolderId = (innerFolder as TFolderRow).id;

    const innerView = await snapshotList(page, "folder-inner", () =>
      page.getByTestId(`files-folder-${innerFolderId}`).click()
    );
    expect(
      innerView.live.breadcrumbs.map((crumb) => crumb.id),
      "the breadcrumbs run root-first from the API"
    ).toEqual([outerFolderId, innerFolderId]);
    await expectViewMatchesBody(page, innerView.live, "folder-inner");

    // The two scopes are disjoint, which is the rest of DEFECT-005: a file that lives
    // in a folder must not also be listed at the root (it used to be, because the root
    // request carried no folder at all). Derived from this run's own responses, so no
    // fixture name is written down here.
    const insideFolders = new Set([...outerView.live.results, ...innerView.live.results].map((row) => row.id));
    expect(insideFolders.size, "the folder views carry the seeded files, so this is not vacuous").toBeGreaterThan(0);
    expect(
      root.live.results.filter((row) => insideFolders.has(row.id)),
      "a file inside a folder is not also listed at the root"
    ).toEqual([]);

    const upOneLevel = await snapshotList(page, "crumb-outer", () =>
      page.getByTestId(`files-breadcrumb-${outerFolderId}`).click()
    );
    expect(
      upOneLevel.live.breadcrumbs.map((crumb) => crumb.id),
      "the middle crumb walks back one level"
    ).toEqual([outerFolderId]);
    await expectViewMatchesBody(page, upOneLevel.live, "crumb-outer");

    const backAtRoot = await snapshotList(page, "crumb-root", () => page.getByTestId("files-breadcrumb-root").click());
    expect(backAtRoot.live.breadcrumbs, "the root crumb walks all the way back").toEqual([]);
    expect(
      new URL(backAtRoot.url).searchParams.get("folder_id"),
      "the root request asks for the root folder explicitly"
    ).toBe("root");
    await expectViewMatchesBody(page, backAtRoot.live, "crumb-root");
    expect(new Set(backAtRoot.live.results.map((row) => row.id)), "the root list is unchanged").toEqual(
      new Set(root.live.results.map((row) => row.id))
    );

    // --- grid mode: a view change, not a data change -------------------------
    await page.getByTestId("files-view-toggle-grid").click();
    await expect(page.getByTestId("files-view-grid")).toBeVisible();
    await expect(page.getByTestId("files-view-table")).toHaveCount(0);
    expect(new URL(page.url()).searchParams.get("mode"), "the grid toggle is URL state").toBe("grid");
    expect(
      new Set((await readRows(page)).map((row) => row.fileId)),
      "the grid renders the same rows the table did"
    ).toEqual(new Set(backAtRoot.live.results.map((row) => row.id)));
    await page.getByTestId("files-view-toggle-table").click();
    await expect(page.getByTestId("files-view-table")).toBeVisible();

    // --- sorting: a real request, and the DOM follows the API's order ---------
    const sortedFirst = await snapshotList(page, "sort-name-first", () => page.getByTestId("files-sort-name").click());
    const firstOrdering = new URL(sortedFirst.url).searchParams.get("ordering");
    expect(firstOrdering, "the sort reaches the API's ordering filter").not.toBeNull();
    await expectViewMatchesBody(page, sortedFirst.live, "sort-name-first");

    const sortedSecond = await snapshotList(page, "sort-name-reversed", () =>
      page.getByTestId("files-sort-name").click()
    );
    const secondOrdering = new URL(sortedSecond.url).searchParams.get("ordering");
    expect(secondOrdering, "clicking the same column again reverses it").toBe(
      firstOrdering?.startsWith("-") ? firstOrdering.slice(1) : `-${firstOrdering}`
    );
    await expectViewMatchesBody(page, sortedSecond.live, "sort-name-reversed");

    // --- the loading state, on a cold load whose request is held --------------
    await page.route(isListUrl, async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 1_500));
      await route.continue();
    });
    await page.goto(APP_FILES_URL);
    await expect(page.getByTestId("files-state-loading"), "a cold load shows the loading state").toBeVisible();
    await expect(page.getByTestId("files-state-loading")).toBeHidden();
    await page.unroute(isListUrl);

    // --- a failure with no response at all: retryable, never an empty table ---
    await page.route(isListUrl, (route) => route.abort("failed"));
    await page.goto(APP_FILES_URL);
    await expect(page.getByTestId("files-state-error"), "a transport failure is visible").toBeVisible();
    await expect(page.getByTestId("files-retry")).toBeVisible();
    await expect(page.locator(ROW_SELECTOR), "and the table is not left empty in silence").toHaveCount(0);
    await expect(
      page.getByTestId("files-storage-text"),
      "the chip does not claim a usage it has no basis for"
    ).not.toHaveText(/0 B of 0 B used/);

    await page.unroute(isListUrl);
    const recoveredCold = await snapshotList(page, "error-retry-cold", () => page.getByTestId("files-retry").click());
    await expect(page.getByTestId("files-state-error")).toBeHidden();
    await expectViewMatchesBody(page, recoveredCold.live, "error-retry-cold");

    // --- a same-key refresh that fails keeps its rows and shows the banner -----
    await page.goto(APP_FILES_URL);
    await expect(page.locator(ROW_SELECTOR).first()).toBeVisible();
    const rowsBeforeFailedRefresh = await readRows(page);
    const chipBeforeFailedRefresh = await page.getByTestId("files-storage").getAttribute("data-used");

    // SWR dedupes a reconnect revalidation against the request that has just resolved, so
    // the trigger has to be outside that window and the request has to be observed going
    // out before anything is asserted about the banner.
    await page.waitForTimeout(2_500);
    const listRequests: string[] = [];
    const recordRequest = (request: Request) => {
      if (isListUrl(new URL(request.url()))) listRequests.push(request.url());
    };
    page.on("request", recordRequest);
    const requestsBefore = listRequests.length;
    await page.route(isListUrl, (route) => route.abort("failed"));
    await page.evaluate(() => window.dispatchEvent(new Event("online")));
    await expect
      .poll(() => listRequests.length, {
        message: "the revalidation must actually be issued before the banner is judged",
      })
      .toBeGreaterThan(requestsBefore);

    await expect(page.getByTestId("files-error-banner"), "a failed refresh is announced").toBeVisible();
    expect(await readRows(page), "and the rows it already had are kept, unchanged").toEqual(rowsBeforeFailedRefresh);
    expect(
      await page.getByTestId("files-storage").getAttribute("data-used"),
      "and the chip still reports the usage of the listing it is showing"
    ).toBe(chipBeforeFailedRefresh);

    await page.unroute(isListUrl);
    await page.getByTestId("files-retry").click();
    await expect(page.getByTestId("files-error-banner")).toBeHidden();
    page.off("request", recordRequest);

    // --- a filter whose request failed keeps no stale rows --------------------
    await page.route(isListUrl, (route) => route.abort("failed"));
    await page.getByTestId("files-quick-pinned").click();
    await expect(page.getByTestId("files-state-error"), "the failed filter is visible").toBeVisible();
    await expect(page.locator(ROW_SELECTOR), "the previous filter's rows are not left on screen").toHaveCount(0);
    expect(new URL(page.url()).searchParams.get("view"), "the filter asked for is still the one in the URL").toBe(
      "pinned"
    );

    await page.unroute(isListUrl);
    const recoveredPinned = await snapshotList(page, "error-retry-pinned", () =>
      page.getByTestId("files-retry").click()
    );
    await expect(page.getByTestId("files-state-error")).toBeHidden();
    expect(new URL(recoveredPinned.url).searchParams.get("pinned"), "the retry runs the filter that failed").toBe(
      "true"
    );
    await expectViewMatchesBody(page, recoveredPinned.live, "error-retry-pinned");

    // --- an empty project has its own copy (DES-001 §7), not the folder's -----
    const emptyProject = await apiWrite(page, "post", `${API_URL}/api/workspaces/${WORKSPACE_SLUG}/projects/`, {
      name: `Empty Files Project ${Date.now()}`,
      identifier: `EF${Date.now() % 10_000}`,
      network: 2,
    });
    const emptyProjectId = ((await emptyProject.json()) as { id: string }).id;
    await page.goto(`${WEB_URL}/${WORKSPACE_SLUG}/projects/${emptyProjectId}/files`);
    await expect(page.getByTestId("files-state-empty"), "a project with nothing in it is empty").toBeVisible();
    await expect(page.getByTestId("files-state-empty")).toHaveAttribute("data-variant", "project");
    await expect(page.getByTestId("files-state-empty")).toContainText("No files yet");
  });

  test("keyboard_focus_reaches_a_row_and_enter_opens_it", async ({ page }) => {
    await signIn(page, OWNER_EMAIL, OWNER_PASSWORD);

    const root = await snapshotList(page, "keyboard-root", () => openFilesTab(page));
    const rowIds = root.live.results.map((row) => row.id);
    expect(
      rowIds.length,
      "the root view needs at least two rows to prove ArrowDown moves focus"
    ).toBeGreaterThanOrEqual(2);
    await expectRowsMatchBody(page, root.live, "keyboard-root");

    // No pointer anywhere in this test: Tab reaches the first row.
    const firstFocused = await tabUntilRowFocused(page);
    expect(firstFocused, "Tab must land on a row the API returned").not.toBeNull();
    const focusedId = firstFocused ?? "";
    expect(rowIds).toContain(focusedId);
    const startIndex = rowIds.indexOf(focusedId);
    const nextId = rowIds[startIndex + 1] ?? "";
    expect(nextId, "there is a row below the first focused one").not.toBe("");

    const startRing = await readFocusDecoration(page);
    expect(startRing.focused, "the focused row must be the one carrying the ring").not.toBeNull();
    expect(
      startRing.paintsSomething,
      "the focused row must paint a focus indicator (an outline or a ring a user can see)"
    ).toBe(true);
    expect(startRing.focused, "the focused row must be visually distinct from an unfocused row").not.toBe(
      startRing.unfocused
    );

    await page.keyboard.press("ArrowDown");
    expect(await focusedFileId(page), "ArrowDown moves focus to the next row").toBe(nextId);
    const movedRing = await readFocusDecoration(page);
    expect(movedRing.paintsSomething, "the row ArrowDown moved to also paints its focus indicator").toBe(true);
    expect(movedRing.focused, "the row ArrowDown moved to keeps a visible focus ring").not.toBe(movedRing.unfocused);

    await page.keyboard.press("ArrowUp");
    expect(await focusedFileId(page), "ArrowUp moves focus back up").toBe(focusedId);

    await page.keyboard.press("ArrowDown");
    expect(await focusedFileId(page), "ArrowDown again").toBe(nextId);

    // Enter opens the focused row: a fresh detail request and the drawer.
    const [detail] = await Promise.all([
      page.waitForResponse(isDetailResponse, { timeout: 30_000 }),
      page.keyboard.press("Enter"),
    ]);
    expect(detail.status(), `GET ${detail.url()}`).toBe(200);
    const detailBody = (await detail.json()) as { file: TFileRow };
    expect(detailBody.file.id, "the drawer's request is for the focused row").toBe(nextId);

    const drawer = page.getByTestId("files-drawer");
    await expect(drawer).toBeVisible();
    await expect(drawer).toHaveAttribute("data-file-id", nextId);
    expect(new URL(page.url()).searchParams.get("file"), "the drawer is a URL state").toBe(nextId);
  });

  test("a_guest_sees_the_read_only_view_and_no_mutation_affordance", async ({ browser }) => {
    const guestContext = await browser.newContext({
      baseURL: WEB_URL,
      ignoreHTTPSErrors: true,
      viewport: { width: 1440, height: 900 },
    });
    const page = await guestContext.newPage();

    try {
      await signIn(page, GUEST_EMAIL, GUEST_PASSWORD);

      const guestRoot = await snapshotList(page, "guest-root", () => openFilesTab(page));

      // The notice is the contract's copy.
      const notice = page.getByTestId("files-readonly-notice");
      await expect(notice).toBeVisible();
      await expect(notice).toContainText("You have view-only access to this project.");

      // A guest browses the same rows the API returns — the notice is not an error page.
      await expectRowsMatchBody(page, guestRoot.live, "guest-root");
      await expectStorageMatchesBody(page, guestRoot.live, "guest-root");

      // No mutation affordance exists in this view for any role, so absence is
      // asserted structurally rather than against the guest's role, and scoped to
      // the Files view so the app shell cannot make the check vacuous.
      const filesView = page.getByTestId("files-root");
      const mutationTestIds = filesView.locator(
        '[data-testid*="upload" i], [data-testid*="rename" i], [data-testid*="delete" i], [data-testid*="move" i]'
      );
      expect(await mutationTestIds.count(), "no upload/move/rename/delete test id is rendered").toBe(0);

      const namedControls = await page.evaluate(() => {
        const root = document.querySelector('[data-testid="files-root"]');
        if (!root) return ["files-root is not rendered"];
        const controls = Array.from(root.querySelectorAll("button, a, [role='button'], [role='menuitem']"));
        return controls
          .filter((node) => !(node.getAttribute("data-testid") ?? "").startsWith("files-quick-"))
          .map((node) => ((node.textContent ?? "").trim() || node.getAttribute("aria-label") || "").toLowerCase())
          .filter((name) => /upload|rename|delete|remove|purge|move to/.test(name));
      });
      expect(namedControls, "no upload/move/rename/delete control is offered").toEqual([]);
    } finally {
      await guestContext.close();
    }
  });
});
