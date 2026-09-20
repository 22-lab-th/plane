/**
 * T-114 — the file detail drawer: the deep link, the preview, the metadata, the links,
 * the version history with its activation question, the file's own audit history, and
 * the rename/move/restore/purge the drawer offers.
 *
 * The harness this file runs on — the API base, the session, the live-response capture
 * and the `files-*` test ids — lives in `./support`, shared with `browse.spec.ts` (T-112)
 * and `upload.spec.ts` (T-113). Every assertion here compares the rendered DOM against
 * the API response **this test consumed**: the drawer's own `GET .../files/<id>/`, the
 * signed preview it asked for, the listing a mutation left behind, and the `page.request`
 * reads taken for the same URLs. Nothing is compared against a fixture written down in
 * this file; the literals it carries are the copies the contract fixes (DESIGN §7) and
 * the test ids the `files-drawer-*` contract freezes.
 *
 * Every file this spec needs is created through the API before it is looked at (the same
 * upload lifecycle the harness seeds with), so the fixture is one the product produced.
 * The one response this file rewrites is the empty-activity case, which no legitimate path
 * produces; it is marked as forced where it happens.
 *
 * The app runs under `StrictMode`, so a dev-mode mount fires a drawer effect twice: every
 * request a test watches is counted **relative to the count before its own action**, and
 * the body it compares against is read once the view has stopped asking.
 *
 * The run is single-worker (`workers: 1` in `playwright.files.config.ts`): the specs share
 * one project fixture and this file must not opt into parallel mode.
 */

// A browser is driven one step at a time: the loops below must await each step, and the
// focus walk exists precisely to observe sequential state.
/* oxlint-disable no-await-in-loop */

// Node imports
import { createHash } from "node:crypto";
// Playwright imports
import { expect, test, type Page } from "@playwright/test";
// harness
import {
  apiWrite,
  APP_FILES_URL,
  createFolder,
  expectViewMatchesBody,
  folderQueryFromAppUrl,
  getListBody,
  GUEST_EMAIL,
  GUEST_PASSWORD,
  listUrl,
  mutatedFileIds,
  openFilesTab,
  OWNER_EMAIL,
  OWNER_PASSWORD,
  readRows,
  signIn,
  snapshotList,
  WEB_URL,
  type TFileRow,
  type TSnapshot,
} from "./support";

// --- the drawer's own test ids, as the component contract freezes them ------

const DRAWER = "files-drawer";
const DRAWER_VERSION_ROW = '[data-testid^="files-drawer-version-"][data-version]';
const DRAWER_FOCUSABLE = 'button, [href], input, [tabindex]:not([tabindex="-1"])';

// --- the API's own payloads -------------------------------------------------

type TUser = { id: string | null; display_name: string | null };

type TDetailFile = TFileRow & {
  object_key: string;
  category: string;
  checksum_sha256: string;
  link_count: number;
};

type TVersion = {
  id: string;
  version_no: number;
  status: string;
  is_active: boolean;
  can_activate: boolean;
  size_bytes: number;
  mime_type: string;
  client_checksum_sha256: string;
  etag: string;
  uploaded_by: TUser;
  created_at: string;
};

type TLink = {
  id: string;
  entity_type: string;
  entity_id: string;
  entity_identifier: string;
  created_at: string;
};

type TActivity = {
  id: string;
  action: string;
  actor: TUser;
  actor_display: string | null;
  file_id: string;
  version_no: number | null;
  created_at: string;
};

type TDetail = {
  file: TDetailFile;
  version: TVersion | null;
  versions: TVersion[];
  links: TLink[];
  link_count: number;
  permissions: { can_edit: boolean; can_delete: boolean; can_download: boolean };
  activity: TActivity[];
};

type TAccessUrl = {
  url: string;
  expires_at: string;
  disposition: string;
  file_name: string;
  version_no: number;
};

type TInitiation = {
  file: { id: string; name_display: string; object_key: string; folder_id: string | null };
  version_no: number;
  upload: { url: string; method: string; headers: Record<string, string>; expires_at: string };
};

type TCompletion = {
  file: { id: string; name_display: string };
  version: { version_no: number; size_bytes: number; status: string; etag: string | null };
  activation_required: boolean;
};

/** One response the API answered for a path this test watches, read as it arrives. */
type TCaptured = { status: number; url: string; body: Promise<unknown> };

// --- reading what the drawer consumed ---------------------------------------

/** The list endpoint's path, as `support.ts` builds it (`.../files/`). */
const FILES_PATH = new URL(listUrl()).pathname;

/**
 * Capture every response for a path, of one method: a preflight, a second render or a
 * navigation must not be mistaken for the request the assertion is about. The body is read
 * as the response arrives, because a later navigation discards it.
 */
function captureResponses(page: Page, matches: (url: URL) => boolean, method = "GET"): TCaptured[] {
  const captured: TCaptured[] = [];
  page.on("response", (response) => {
    if (response.request().method() !== method) return;
    const url = new URL(response.url());
    if (!matches(url)) return;
    captured.push({ status: response.status(), url: response.url(), body: response.json().catch(() => null) });
  });
  return captured;
}

/**
 * The body of the last response a view asked for since `since`, once it has stopped
 * asking. The settle window covers the dev-mode double mount: the panel's state ends on
 * the last response, so that is the one the DOM is compared against.
 */
async function settledBody<T>(page: Page, captured: TCaptured[], since: number, label: string): Promise<T> {
  await expect
    .poll(() => captured.length, { message: `${label}: the view must ask for this path` })
    .toBeGreaterThan(since);
  await expect
    .poll(
      async () => {
        const count = captured.length;
        await page.waitForTimeout(250);
        return captured.length === count;
      },
      { message: `${label}: the view must stop asking before it is compared` }
    )
    .toBe(true);

  const entry = captured[captured.length - 1];
  expect(entry, `${label}: a response was captured`).toBeDefined();
  return (entry ? await entry.body : null) as T;
}

/** The API's own detail payload for a file, read directly (an end state, not a DOM source). */
async function getDetail(page: Page, fileId: string, options: { trashed?: boolean } = {}): Promise<TDetail> {
  const url = `${listUrl()}${fileId}/${options.trashed ? "?trashed=true" : ""}`;
  const response = await page.request.get(url);
  expect(response.ok(), `GET ${url}`).toBe(true);
  return (await response.json()) as TDetail;
}

// --- fixtures, created through the API --------------------------------------

/**
 * Create one file through the upload lifecycle the app itself uses: initiate (the presign
 * names the key and the type), PUT the bytes straight to the store, finalize (the server
 * verifies what it stored). The returned bodies are the API's own word about the file, so
 * the tests below compare the drawer against them instead of against a guess.
 *
 * The finalize declares the digest of the bytes it sent, which is what makes the drawer's
 * checksum line carry a value rather than its empty-history dash: the declaration is
 * advisory to the server (AD-16) and is recorded as the version's own evidence.
 */
async function createFile(
  page: Page,
  spec: { name: string; mimeType: string; content: Buffer }
): Promise<{ initiation: TInitiation; completion: TCompletion; fileId: string }> {
  const initiationResponse = await apiWrite(page, "post", `${listUrl()}initiate-upload/`, {
    file_name: spec.name,
    size_bytes: spec.content.length,
    mime_type: spec.mimeType,
  });
  const initiation = (await initiationResponse.json()) as TInitiation;

  const put = await page.request.put(initiation.upload.url, {
    data: spec.content,
    headers: initiation.upload.headers,
  });
  expect(put.status(), `PUT ${spec.name} to the store the presign named`).toBe(200);

  const completionResponse = await apiWrite(page, "post", `${listUrl()}${initiation.file.id}/complete-upload/`, {
    version_no: initiation.version_no,
    size_bytes: spec.content.length,
    checksum_sha256: createHash("sha256").update(spec.content).digest("hex"),
  });
  const completion = (await completionResponse.json()) as TCompletion;
  expect(completion.version.size_bytes, `${spec.name}: the server verified the stored bytes`).toBe(spec.content.length);

  mutatedFileIds.add(initiation.file.id);
  return { initiation, completion, fileId: initiation.file.id };
}

/** A real 1×1 PNG: its opening bytes are the signature the server's magic-byte check reads. */
const PNG_PIXEL = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg==",
  "base64"
);

/** Bytes for a text fixture, and the marker they carry so the upload is identifiable. */
function textFixture(marker: string, sizeBytes = 512): { content: Buffer; marker: string } {
  const unit = `${marker}:`;
  return {
    content: Buffer.from(unit.repeat(Math.ceil(sizeBytes / unit.length)).slice(0, sizeBytes), "utf8"),
    marker,
  };
}

// --- reading the drawer -----------------------------------------------------

/** The `<dd>` the given `<dt>` labels, as the metadata block renders it. */
async function metaValue(page: Page, label: string): Promise<string> {
  return page.evaluate(
    ({ panelId, wanted }) => {
      const panel = document.querySelector(`[data-testid="${panelId}"]`);
      const terms = Array.from(panel?.querySelectorAll("dt") ?? []);
      const term = terms.find((node) => (node.textContent ?? "").trim() === wanted);
      const value = term?.nextElementSibling ?? null;
      return (value?.textContent ?? "").replace(/\s+/g, " ").trim();
    },
    { panelId: DRAWER, wanted: label }
  );
}

type TRenderedVersion = {
  versionNo: string;
  status: string;
  text: string;
  etag: string;
  hasDownload: boolean;
  hasActivate: boolean;
};

async function renderedVersions(page: Page): Promise<TRenderedVersion[]> {
  return page.$$eval(
    DRAWER_VERSION_ROW,
    (nodes, prefixes) =>
      nodes.map((node) => {
        const versionNo = node.getAttribute("data-version") ?? "";
        const inner = (prefix: string): string =>
          (node.querySelector(`[data-testid="${prefix}${versionNo}"]`)?.textContent ?? "").replace(/\s+/g, " ").trim();
        return {
          versionNo,
          status: node.getAttribute("data-status") ?? "",
          text: (node.textContent ?? "").replace(/\s+/g, " ").trim(),
          etag: inner(prefixes.etag),
          hasDownload: node.querySelector(`[data-testid="${prefixes.download}${versionNo}"]`) !== null,
          hasActivate: node.querySelector(`[data-testid="${prefixes.activate}${versionNo}"]`) !== null,
        };
      }),
    {
      etag: "files-drawer-version-etag-",
      download: "files-drawer-version-download-",
      activate: "files-drawer-version-activate-",
    }
  );
}

type TRenderedRow = { id: string; text: string };

/** One list of the drawer's rows (links, activity) as `id` + collapsed text. */
async function renderedRows(page: Page, selector: string, idPrefix: string): Promise<TRenderedRow[]> {
  return page.$$eval(
    selector,
    (nodes, prefix) =>
      nodes.map((node) => ({
        id: (node.getAttribute("data-testid") ?? "").replace(prefix, ""),
        text: (node.textContent ?? "").replace(/\s+/g, " ").trim(),
      })),
    idPrefix
  );
}

// --- the drawer's focus behaviour -------------------------------------------

/** Where the document's focus is, and whether the drawer panel holds it. */
async function focusState(page: Page): Promise<{ inside: boolean; name: string }> {
  return page.evaluate((panelId) => {
    const panel = document.querySelector(`[data-testid="${panelId}"]`);
    const active = document.activeElement as HTMLElement | null;
    return {
      inside: panel !== null && active !== null && panel.contains(active),
      name: active?.getAttribute("data-testid") ?? active?.getAttribute("aria-label") ?? active?.tagName ?? "",
    };
  }, DRAWER);
}

/** The last element the drawer's own trap wraps to (`panel.querySelectorAll` order). */
async function lastFocusable(page: Page): Promise<string> {
  return page.evaluate(
    ({ panelId, selector }) => {
      const panel = document.querySelector(`[data-testid="${panelId}"]`);
      if (!panel) return "";
      const nodes = Array.from(panel.querySelectorAll<HTMLElement>(selector));
      const last = nodes[nodes.length - 1];
      return last?.getAttribute("data-testid") ?? last?.getAttribute("aria-label") ?? last?.tagName ?? "";
    },
    { panelId: DRAWER, selector: DRAWER_FOCUSABLE }
  );
}

/**
 * Open the drawer the way the URL does: `?file=<id>` is the state the panel is mounted
 * from, so a deep link and a row click land on the same drawer. `openFilesTab` carries the
 * boot tolerance for the route; the panel is then waited for by its own contract.
 */
async function openDrawerByDeepLink(page: Page, fileId: string, query = ""): Promise<void> {
  await openFilesTab(page, `${APP_FILES_URL}?file=${fileId}${query}`);
  const drawer = page.getByTestId(DRAWER);
  await expect(drawer, "the deep link must open the drawer").toBeVisible();
  await expect(drawer).toHaveAttribute("data-file-id", fileId);
}

/**
 * A live snapshot of the listing one app URL names, checked to be that listing.
 *
 * Every drawer mutation revalidates the listing behind it, and that response can still be
 * in flight when the next snapshot starts watching — `snapshotList` captures whatever
 * listing answers first, which would then be compared against a view it does not describe.
 * The query the capture came from is therefore checked against the URL asked for, and a
 * capture that is not that listing is retried (a navigation is idempotent).
 */
async function snapshotListing(page: Page, label: string, url: string): Promise<TSnapshot> {
  for (let attempt = 0; attempt < 3; attempt += 1) {
    const snapshot = await snapshotList(page, label, () => openFilesTab(page, url));
    const app = new URL(url).searchParams;
    const asked = new URL(snapshot.url).searchParams;
    const expectedFolder = folderQueryFromAppUrl(url);
    const expectedTrashed = app.get("view") === "trash";

    if ((asked.get("folder_id") ?? null) === expectedFolder && (asked.get("trashed") === "true") === expectedTrashed) {
      return snapshot;
    }
    console.log(
      `[drawer-spec] ${label}: the capture was the listing ${snapshot.url}, not the one ${url} names — retrying`
    );
  }

  throw new Error(`${label}: three captures in a row were for another listing than ${url}`);
}

// --- the copies the contract fixes (DESIGN §7) ------------------------------

const SVG_TILE_COPY = "SVG and HTML are always downloads — this type is never rendered inline.";
const NO_PREVIEW_COPY = "Preview is not available for this type. Download to open it.";
const NO_LIVE_LINKS_COPY = "No live links — this file appears under Orphan until it is attached to an issue or page.";
const NO_ACTIVITY_COPY = "No recorded activity.";
const NOT_DOWNLOADABLE_COPY = "This file is not available for download.";

/** The audit actions in the words the activity list uses (the drawer's copy map). */
const ACTIVITY_COPY: Record<string, string> = {
  upload_initiated: "started an upload",
  upload_completed: "completed an upload",
  upload_failed: "had an upload fail",
  version_created: "uploaded a version",
  version_activated: "made a version active",
  downloaded: "downloaded",
  previewed: "previewed",
  renamed: "renamed",
  moved: "moved",
  copied: "copied",
  linked: "linked",
  unlinked: "unlinked",
  trashed: "moved to trash",
  restored: "restored",
  purged: "purged",
  permission_denied: "was refused for lack of permission",
  quota_rejected: "was refused for quota",
};

/** The audit row as the activity list composes it, from the row the API returned. */
const activityLine = (entry: TActivity): string => {
  const actor = entry.actor?.display_name || entry.actor_display || "Unknown actor";
  const label = ACTIVITY_COPY[entry.action] ?? entry.action;
  return `${actor} ${entry.version_no === null ? label : `${label} v${entry.version_no}`}`;
};

/**
 * A human size for the display rule the metadata block and the version history share.
 * The value is the response's own `size_bytes`; only the unit choice is a rule.
 */
const displaySize = (bytes: number): string => {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const rounded = Number.isInteger(value) || value >= 10 ? Math.round(value).toString() : value.toFixed(1);
  return `${rounded} ${units[unit]}`;
};

/**
 * The date the panel prints for an API timestamp. `renderFormattedDate` reads a string's
 * first ten characters (its UTC calendar date) and formats them, so this expected copy is a
 * function of the response's own timestamp and of nothing else.
 */
const displayDate = (iso: string): string => {
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const [year, month, day] = iso.slice(0, 10).split("-").map(Number);
  return `${months[(month ?? 1) - 1]} ${String(day).padStart(2, "0")}, ${year}`;
};

/** The `X-Amz-Signature` a presigned URL carries: the part a re-signing open changes. */
const signatureOf = (url: string): string => new URL(url).searchParams.get("X-Amz-Signature") ?? "";

/**
 * Wait (bounded, at most 1.1 s) until the wall clock has crossed the next second boundary.
 *
 * A SigV4 presign is a pure function of the key, the expiry and `X-Amz-Date`, and that date
 * has **second** granularity, so two signing requests inside one second legitimately return
 * the identical URL. Spacing an open past a boundary is what lets "a second open signs its
 * own URL" be a claim about the signing rather than about the clock (DEFECT-008).
 */
async function waitPastSecondBoundary(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 1_100 - (Date.now() % 1_000)));
}

// --- the specs -------------------------------------------------------------------------------------------------------------------------

test.describe("File detail drawer (T-114)", () => {
  test("the_deep_link_opens_the_drawer_and_escape_closes_it", async ({ page }) => {
    await signIn(page, OWNER_EMAIL, OWNER_PASSWORD);

    // The listing the view at the root shows, so the target is a row the DOM behind the
    // drawer actually carries: an unfiltered `getListBody()` answers with every file in
    // the project at any depth, including rows the root view does not show (DEFECT-005).
    const listing = await getListBody(page, { folder_id: "root", ordering: "-created" });
    const row = listing.results[0];
    expect(row, "the harness must seed a file for the deep link to name").toBeDefined();
    const target = row as TFileRow;

    const details = captureResponses(page, (url) => url.pathname === `${FILES_PATH}${target.id}/`);
    const previews = captureResponses(page, (url) => url.pathname === `${FILES_PATH}${target.id}/preview/`);

    // A cold navigation to the deep link: the drawer is the URL's state, so the panel is on
    // screen, for the file the URL names, before anything is clicked.
    await openDrawerByDeepLink(page, target.id);
    expect(new URL(page.url()).searchParams.get("file"), "the URL names the file the drawer shows").toBe(target.id);
    const detail = await settledBody<TDetail>(page, details, 0, "the drawer's own detail request");
    expect(detail.file.id, "the payload the drawer consumed is for the deep-linked file").toBe(target.id);
    expect(
      (await readRows(page)).map((rendered) => rendered.fileId),
      "the deep-linked file is a row of the listing behind the drawer"
    ).toContain(target.id);
    await expect(page.getByTestId(DRAWER).locator("h2"), "and the header names it").toHaveText(
      detail.file.name_display
    );
    await expect
      .poll(() => previews.length, { message: "opening a drawer signs a presigned preview of its own" })
      .toBeGreaterThan(0);

    // Escape closes it, and the URL drops the state it was opened from.
    await page.keyboard.press("Escape");
    await expect(page.getByTestId(DRAWER), "Escape closes the drawer").toBeHidden();
    expect(new URL(page.url()).searchParams.get("file"), "and the closed drawer leaves the URL").toBeNull();

    // Opened from a row instead: a keyboard path, not a programmatic focus.
    await page.getByTestId(`files-row-${target.id}`).click();
    const drawer = page.getByTestId(DRAWER);
    await expect(drawer).toBeVisible();
    await expect(drawer).toHaveAttribute("data-file-id", target.id);
    expect((await focusState(page)).name, "the panel takes focus when it opens").toBe(DRAWER);

    // Shift+Tab from the panel is the trap's wrap: it lands on the last control inside.
    const last = await lastFocusable(page);
    expect(last, "the drawer must render focusable controls for the trap to wrap to").not.toBe("");
    await page.keyboard.press("Shift+Tab");
    const wrapped = await focusState(page);
    expect(wrapped.inside, "Shift+Tab from the panel stays inside the drawer").toBe(true);
    expect(wrapped.name, "and wraps to the drawer's last control").toBe(last);

    // Tab must never leave the panel, however many times it is pressed.
    for (let press = 0; press < 12; press += 1) {
      await page.keyboard.press("Tab");
      const state = await focusState(page);
      expect(state.inside, `Tab press ${press + 1} must keep focus inside the drawer, not on ${state.name}`).toBe(true);
    }

    await page.keyboard.press("Escape");
    await expect(drawer).toBeHidden();
    await expect
      .poll(async () => page.evaluate(() => (document.activeElement as HTMLElement | null)?.dataset?.testid ?? ""), {
        message: "closing returns focus to the row that opened the drawer (DESIGN §6)",
      })
      .toBe(`files-row-${target.id}`);
  });

  test("the_preview_signs_a_fresh_url_per_open_and_never_inlines_an_svg", async ({ page }) => {
    const stamp = Date.now();
    const marker = `drawer-preview-${stamp}`;
    await signIn(page, OWNER_EMAIL, OWNER_PASSWORD);

    const png = await createFile(page, { name: `${marker}.png`, mimeType: "image/png", content: PNG_PIXEL });
    const svg = await createFile(page, {
      name: `${marker}.svg`,
      mimeType: "image/svg+xml",
      content: Buffer.from(
        `<svg xmlns="http://www.w3.org/2000/svg" width="4" height="4"><title>${marker}</title><rect width="4" height="4" fill="#f00"/></svg>`,
        "utf8"
      ),
    });
    const plain = await createFile(page, {
      name: `${marker}.txt`,
      mimeType: "text/plain",
      content: textFixture(marker).content,
    });

    // --- a raster image renders from the URL the drawer was handed -------------
    const previews = captureResponses(page, (url) => url.pathname === `${FILES_PATH}${png.fileId}/preview/`);
    await openDrawerByDeepLink(page, png.fileId);
    const first = await settledBody<TAccessUrl>(page, previews, 0, "the image preview the drawer asked for");

    const image = page.getByTestId("files-drawer-preview-image");
    await expect(image, "a raster image is rendered inline").toBeVisible();
    await expect(image, "from exactly the URL this response signed").toHaveAttribute("src", first.url);
    expect(first.disposition, "the server signs a raster image inline").toBe("inline");
    expect(first.file_name, "the signed link carries the API's own display name").toBe(`${marker}.png`);
    expect(first.version_no, "and the version the payload reported").toBe(png.initiation.version_no);

    // The URL really serves the stored bytes: fetched here, not through the drawer.
    const fetched = await page.request.get(first.url, { failOnStatusCode: false });
    expect(fetched.status(), "the signed preview URL answers the object").toBe(200);
    expect(fetched.headers()["content-type"], "pinned to the type the server verified").toBe("image/png");
    expect(fetched.headers()["content-disposition"], "an image is served inline").toContain("inline");
    expect(Buffer.from(await fetched.body()).equals(PNG_PIXEL), "and the bytes are the ones this test uploaded").toBe(
      true
    );
    expect(
      await image.evaluate((node) => (node as HTMLImageElement).naturalWidth),
      "the browser decoded the image the panel handed it"
    ).toBeGreaterThan(0);

    // --- every open signs its own URL -----------------------------------------
    // Spaced past a second boundary: a same-second re-sign is legitimately identical, and
    // what this asserts is that the drawer re-signed at all (DEFECT-008).
    await waitPastSecondBoundary();
    await page.keyboard.press("Escape");
    await expect(page.getByTestId(DRAWER)).toBeHidden();
    await page.getByTestId(`files-row-${png.fileId}`).click();
    await expect(page.getByTestId(DRAWER)).toBeVisible();
    const second = await settledBody<TAccessUrl>(page, previews, 1, "the preview the second open signed");
    expect(previews.length, "the second open issued a preview request of its own").toBeGreaterThan(1);
    expect(
      signatureOf(second.url),
      "and re-signed: its URL carries a different X-Amz-Signature from the first open's"
    ).not.toBe(signatureOf(first.url));
    expect(second.url, "a presigned URL belongs to one rendering, so a second open signs a different one").not.toBe(
      first.url
    );
    await expect(page.getByTestId("files-drawer-preview-image")).toHaveAttribute("src", second.url);

    // --- and a deep link survives a reload (AC-17) ----------------------------
    await waitPastSecondBoundary();
    const beforeReload = previews.length;
    await page.reload();
    await expect(page.getByTestId(DRAWER), "the reloaded deep link re-opens the drawer").toBeVisible();
    const third = await settledBody<TAccessUrl>(page, previews, beforeReload, "the preview the reload signed");
    expect(previews.length, "the reload issued a preview request of its own").toBeGreaterThan(beforeReload);
    expect(signatureOf(third.url), "the reload signs a URL of its own").not.toBe(signatureOf(second.url));
    const reloadedImage = page.getByTestId("files-drawer-preview-image");
    await expect(reloadedImage).toBeVisible();
    await expect(reloadedImage).toHaveAttribute("src", third.url);
    expect(
      await reloadedImage.evaluate((node) => (node as HTMLImageElement).naturalWidth),
      "and the image still renders after the reload"
    ).toBeGreaterThan(0);

    // --- an SVG is never rendered inline, whatever the response says ----------
    const svgPreviews = captureResponses(page, (url) => url.pathname === `${FILES_PATH}${svg.fileId}/preview/`);
    await openDrawerByDeepLink(page, svg.fileId);
    const signedSvg = await settledBody<TAccessUrl>(page, svgPreviews, 0, "the SVG preview the drawer asked for");
    expect(signedSvg.disposition, "the server signs a script-capable type as a download").toBe("attachment");
    await expect(
      page.getByTestId("files-drawer-preview-image"),
      "the drawer renders nothing that could run the SVG"
    ).toHaveCount(0);
    await expect(page.getByTestId("files-drawer-preview-tile"), "it shows a type tile instead").toBeVisible();
    await expect(
      page.getByTestId("files-drawer-preview-tile"),
      "and says why this type is never inlined"
    ).toContainText(SVG_TILE_COPY);

    const fetchedSvg = await page.request.get(signedSvg.url, { failOnStatusCode: false });
    expect(fetchedSvg.status(), "the URL the drawer was handed is live").toBe(200);
    expect(
      fetchedSvg.headers()["content-disposition"],
      "and even fetched directly it is a download, not a render"
    ).toContain("attachment");

    // --- a type the server would sign inline that the panel still refuses -----
    const plainPreviews = captureResponses(page, (url) => url.pathname === `${FILES_PATH}${plain.fileId}/preview/`);
    await openDrawerByDeepLink(page, plain.fileId);
    const signedText = await settledBody<TAccessUrl>(page, plainPreviews, 0, "the text preview the drawer asked for");
    expect(
      signedText.disposition,
      "the server signs plain text inline: the tile is the client's own, stricter decision"
    ).toBe("inline");
    await expect(page.getByTestId("files-drawer-preview-image")).toHaveCount(0);
    await expect(page.getByTestId("files-drawer-preview-tile")).toContainText(NO_PREVIEW_COPY);
  });

  test("the_metadata_links_and_version_history_match_the_detail_payload", async ({ page }) => {
    const stamp = Date.now();
    const marker = `drawer-payload-${stamp}`;
    await signIn(page, OWNER_EMAIL, OWNER_PASSWORD);

    const created = await createFile(page, {
      name: `${marker}.txt`,
      mimeType: "text/plain",
      content: textFixture(marker).content,
    });

    // The project a link points at is read from a listing URL the API answered, so no id
    // is written down here.
    const root = await snapshotList(page, "drawer-root", () => openFilesTab(page), { requireLive: true });
    const [, projectSegment] = new URL(root.url).pathname.split("/projects/");
    const projectId = (projectSegment ?? "").split("/")[0];
    expect(projectId, "the listing URL names the project the file belongs to").not.toBe("");

    const details = captureResponses(page, (url) => url.pathname === `${FILES_PATH}${created.fileId}/`);
    await openDrawerByDeepLink(page, created.fileId);
    const beforeLink = await settledBody<TDetail>(page, details, 0, "the drawer's own detail request");

    // The metadata block names the version the preview is following, so its numbers come
    // from the payload: the bytes, the version number and the uploader, all live values.
    const shownVersion = beforeLink.version;
    expect(shownVersion, "an uploaded file has a version the preview can follow").not.toBeNull();
    const version = shownVersion as TVersion;

    // --- the header and the metadata block ------------------------------------
    expect(beforeLink.links, "a file that has just been uploaded is attached to nothing").toEqual([]);
    await expect(page.getByTestId(DRAWER).locator("h2"), "the header names the file the payload carries").toHaveText(
      beforeLink.file.name_display
    );
    expect(await metaValue(page, "Type"), "Type is the API's mime_type").toBe(beforeLink.file.mime_type);
    expect(await metaValue(page, "Category"), "Category is the API's storage class").toBe(beforeLink.file.category);
    const recordedChecksum = version.client_checksum_sha256 || beforeLink.file.checksum_sha256;
    expect(
      recordedChecksum,
      "this fixture declares its digest, so the checksum line carries a value rather than the empty dash"
    ).not.toBe("");
    expect(await metaValue(page, "Checksum (SHA-256)"), "the checksum is the one the API recorded").toBe(
      recordedChecksum || "—"
    );
    expect(await metaValue(page, "Object key"), "the object key is the API's own key").toBe(beforeLink.file.object_key);
    expect(await metaValue(page, "Links"), "the link count is the API's count").toBe(
      String(beforeLink.file.link_count)
    );
    expect(await metaValue(page, "Created"), "Created is the API's created_at, as the panel dates it").toBe(
      displayDate(beforeLink.file.created_at)
    );
    expect(await metaValue(page, "Updated"), "Updated is the API's updated_at").toBe(
      displayDate(beforeLink.file.updated_at)
    );
    expect(await metaValue(page, "Size"), "Size is the served version's bytes").toBe(
      `${displaySize(version.size_bytes)} (v${version.version_no})`
    );
    expect(await metaValue(page, "Uploader"), "Uploader is the version's own uploader").toBe(
      version.uploaded_by?.display_name ?? beforeLink.file.uploader?.display_name ?? "—"
    );

    // --- no links: the contract's own copy, against an empty payload list -----
    await expect(page.getByTestId("files-drawer-links")).toContainText(NO_LIVE_LINKS_COPY);
    await expect(page.getByTestId("files-drawer-links")).toContainText(`Links (${beforeLink.link_count})`);
    expect(
      await renderedRows(page, '[data-testid^="files-drawer-link-"]', "files-drawer-link-"),
      "an empty link list renders no link rows"
    ).toEqual([]);

    // --- the version history: one version, read against the payload -----------
    const rows = await renderedVersions(page);
    expect(
      rows.map((entry) => entry.versionNo),
      "one row per version, in the API's order"
    ).toEqual(beforeLink.versions.map((entry) => String(entry.version_no)));
    beforeLink.versions.forEach((entry, index) => {
      const rendered = rows[index] as TRenderedVersion;
      expect(rendered.status, `v${entry.version_no}: the row's status is the API's status`).toBe(entry.status);
      expect(
        rendered.text.toLowerCase(),
        `v${entry.version_no}: the chip names the status the payload reports`
      ).toContain(entry.status.replace("_", " "));
      expect(rendered.text, `v${entry.version_no}: the row names the uploader the payload carries`).toContain(
        entry.uploaded_by?.display_name ?? "Unknown uploader"
      );
      expect(rendered.text, `v${entry.version_no}: the row shows the version's own bytes`).toContain(
        displaySize(entry.size_bytes)
      );
      expect(rendered.text, `v${entry.version_no}: the row dates the version`).toContain(displayDate(entry.created_at));
      expect(rendered.etag, `v${entry.version_no}: the observed ETag is the one the payload recorded`).toBe(
        `ETag ${entry.etag || "—"}`
      );
      expect(rendered.hasDownload, `v${entry.version_no}: download follows the payload's can_download`).toBe(
        beforeLink.permissions.can_download
      );
      expect(
        rendered.hasActivate,
        `v${entry.version_no}: Make active is offered only where can_edit and can_activate agree`
      ).toBe(beforeLink.permissions.can_edit && entry.can_activate && !entry.is_active);
    });
    await expect(page.getByTestId("files-drawer-versions")).toContainText(
      `Version history (${beforeLink.versions.length})`
    );

    // --- the links list, once the API says the file is attached ---------------
    const linkResponse = await apiWrite(page, "post", `${listUrl()}${created.fileId}/links/`, {
      entity_type: "project",
      entity_id: projectId,
    });
    const link = ((await linkResponse.json()) as { link: TLink }).link;

    await openDrawerByDeepLink(page, created.fileId);
    const afterLink = await settledBody<TDetail>(page, details, 1, "the detail the re-opened drawer consumed");
    expect(
      afterLink.links.map((entry) => entry.id),
      "the API attaches the file to the project"
    ).toEqual([link.id]);
    await expect(page.getByTestId("files-drawer-links")).toContainText(`Links (${afterLink.link_count})`);

    const linkRows = await renderedRows(page, '[data-testid^="files-drawer-link-"]', "files-drawer-link-");
    expect(
      linkRows.map((entry) => entry.id),
      "one row per link the payload carries, in its order"
    ).toEqual(afterLink.links.map((entry) => entry.id));
    afterLink.links.forEach((entry, index) => {
      const rendered = linkRows[index] as TRenderedRow;
      expect(rendered.text, `link ${entry.id}: the row names the entity type`).toContain(entry.entity_type);
      expect(rendered.text, `link ${entry.id}: and the identifier the API resolved`).toContain(
        entry.entity_identifier || entry.entity_id
      );
    });
  });

  test("the_activity_list_matches_the_audit_payload", async ({ page }) => {
    const stamp = Date.now();
    const marker = `drawer-activity-${stamp}`;
    await signIn(page, OWNER_EMAIL, OWNER_PASSWORD);

    const created = await createFile(page, {
      name: `${marker}.txt`,
      mimeType: "text/plain",
      content: textFixture(marker).content,
    });

    // A mutation written through the API is an audit row the detail view must show (AC-18):
    // the rename is the row this test looks for by name.
    const renamed = `${marker}-renamed.txt`;
    await apiWrite(page, "patch", `${listUrl()}${created.fileId}/`, { name_display: renamed });

    const details = captureResponses(page, (url) => url.pathname === `${FILES_PATH}${created.fileId}/`);
    await openDrawerByDeepLink(page, created.fileId);
    const detail = await settledBody<TDetail>(page, details, 0, "the drawer's own detail request");

    expect(detail.activity.length, "the upload itself writes audit rows for the file").toBeGreaterThan(0);
    expect(
      detail.activity.map((entry) => entry.action),
      "the rename this test wrote is in the file's own history"
    ).toContain("renamed");
    expect(
      detail.activity.every((entry) => entry.file_id === created.fileId),
      "every row the detail reports is this file's"
    ).toBe(true);

    const rows = await renderedRows(page, '[data-testid^="files-drawer-activity-"]', "files-drawer-activity-");
    expect(
      rows.map((entry) => entry.id),
      "one row per audit entry the payload carries, as the API orders them"
    ).toEqual(detail.activity.map((entry) => entry.id));

    const now = Date.now();
    detail.activity.forEach((entry, index) => {
      const rendered = rows[index] as TRenderedRow;
      expect(rendered.text, `audit ${entry.id}: the actor the payload carries`).toContain(
        entry.actor?.display_name || entry.actor_display || "Unknown actor"
      );
      expect(rendered.text, `audit ${entry.id}: the action in the list's words`).toContain(activityLine(entry));
      expect(
        now - Date.parse(entry.created_at),
        `audit ${entry.id}: this run's own mutation, so its time-ago copy must be a fresh one`
      ).toBeLessThan(60 * 60 * 1000);
      expect(rendered.text, `audit ${entry.id}: the time-ago copy of a fresh row`).toMatch(
        /(less than a minute|1 minute|\d+ minutes|\d+ hours) ago$/
      );
    });

    // --- the empty branch, forced ---------------------------------------------
    // No legitimate path produces a file with no audit rows: uploading one writes rows, and
    // so does every mutation. The payload is therefore rewritten to carry an empty history —
    // everything else in it is the API's own response — and the copy that branch renders is
    // observed. This is the one response this file does not consume as it arrived.
    const detailPattern = (url: URL): boolean => url.pathname === `${FILES_PATH}${created.fileId}/`;
    await page.route(detailPattern, async (route) => {
      // A preflight is not the read under test; only the GET carries the history.
      if (route.request().method() !== "GET") {
        await route.continue();
        return;
      }
      const response = await route.fetch();
      const body = (await response.json()) as TDetail;
      await route.fulfill({ response, json: { ...body, activity: [] } satisfies TDetail });
    });
    await openDrawerByDeepLink(page, created.fileId);
    await expect(page.getByTestId("files-drawer-activity"), "the emptied history renders its own copy").toContainText(
      NO_ACTIVITY_COPY
    );
    expect(
      await renderedRows(page, '[data-testid^="files-drawer-activity-"]', "files-drawer-activity-"),
      "and no audit rows"
    ).toEqual([]);
    await page.unroute(detailPattern);
  });

  test("a_revision_asks_before_it_becomes_active_and_both_answers_hold", async ({ page }) => {
    const stamp = Date.now();
    const marker = `drawer-version-${stamp}`;
    await signIn(page, OWNER_EMAIL, OWNER_PASSWORD);

    const name = `${marker}.txt`;
    const created = await createFile(page, {
      name,
      mimeType: "text/plain",
      content: textFixture(marker, 512).content,
    });

    const details = captureResponses(page, (url) => url.pathname === `${FILES_PATH}${created.fileId}/`);
    const previews = captureResponses(page, (url) => url.pathname === `${FILES_PATH}${created.fileId}/preview/`);
    const completions = captureResponses(
      page,
      (url) => url.pathname === `${FILES_PATH}${created.fileId}/complete-upload/`,
      "POST"
    );
    const downloads = captureResponses(page, (url) => url.pathname === `${FILES_PATH}${created.fileId}/download/`);

    await openDrawerByDeepLink(page, created.fileId);
    const start = await settledBody<TDetail>(page, details, 0, "the drawer's own detail request");
    const activeNo = start.versions.find((entry) => entry.is_active)?.version_no ?? null;
    expect(activeNo, "the uploaded file has an active version").not.toBeNull();
    expect(
      (await settledBody<TAccessUrl>(page, previews, 0, "the preview the open signed")).version_no,
      "the preview follows the active version"
    ).toBe(activeNo);

    // Upload a revision through the drawer's own control, exactly as a user would.
    const revisionBytes = textFixture(`${marker}-rev`, 900).content;
    await page.getByTestId("files-drawer-version-input").setInputFiles({
      name,
      mimeType: "text/plain",
      buffer: revisionBytes,
    });

    const dialog = page.getByTestId("files-drawer-activation-modal");
    await expect(dialog, "a verified revision ends in the activation question (AC-43)").toBeVisible();
    const completion = await settledBody<TCompletion>(page, completions, 0, "the finalize the drawer asked for");
    expect(completion.activation_required, "a revision never becomes active on its own").toBe(true);
    const newNo = completion.version.version_no;
    expect(newNo, "the revision is the next version number").toBeGreaterThan(activeNo as number);
    expect(completion.version.size_bytes, "with the bytes this test sent").toBe(revisionBytes.length);
    await expect(dialog, "the question names the version the API numbered").toContainText(
      `Make v${newNo} the active version? The current version stays available in the history.`
    );

    // While the question is open, the pointer has not moved.
    const awaitingAnswer = await getDetail(page, created.fileId);
    expect(
      awaitingAnswer.versions.filter((entry) => entry.is_active).map((entry) => entry.version_no),
      "the stored revision is not active while the question is open"
    ).toEqual([activeNo]);

    // --- Keep current: the new revision stays, superseded and activatable -----
    await page.getByTestId("files-drawer-activation-decline").click();
    await expect(dialog).toBeHidden();
    await expect(page.getByTestId("files-drawer-notice"), "the decline says what it left").toHaveText(
      `Version ${newNo} saved — v${activeNo} is still the active version.`
    );

    const declined = await getDetail(page, created.fileId);
    expect(declined.versions.find((entry) => entry.version_no === newNo)?.status, "stored as superseded").toBe(
      "superseded"
    );
    expect(
      declined.versions.filter((entry) => entry.is_active).map((entry) => entry.version_no),
      "and the active pointer is exactly where it was"
    ).toEqual([activeNo]);

    const afterDecline = await settledBody<TDetail>(page, details, 1, "the detail the drawer refetched");
    const declinedRows = await renderedVersions(page);
    expect(
      declinedRows.map((entry) => entry.versionNo),
      "the history lists both versions, as the payload orders them"
    ).toEqual(afterDecline.versions.map((entry) => String(entry.version_no)));
    const newRow = declinedRows.find((entry) => entry.versionNo === String(newNo)) as TRenderedVersion;
    expect(newRow.status, "the new revision's chip carries its status").toBe("superseded");
    expect(newRow.text.toLowerCase(), "and the chip says the word").toContain("superseded");
    expect(
      declinedRows.filter((entry) => entry.hasActivate).map((entry) => entry.versionNo),
      "Make active is offered on exactly the versions the payload allows it on"
    ).toEqual(
      afterDecline.versions
        .filter((entry) => afterDecline.permissions.can_edit && entry.can_activate && !entry.is_active)
        .map((entry) => String(entry.version_no))
    );

    // --- and it is still activatable later, from the history (AC-43) ---------
    await page.getByTestId(`files-drawer-version-activate-${newNo}`).click();
    await expect(page.getByTestId("files-drawer-notice")).toHaveText(`Version ${newNo} is now active.`);
    const afterActivation = await settledBody<TDetail>(page, details, 2, "the detail after activating");
    expect(
      afterActivation.versions.filter((entry) => entry.is_active).map((entry) => entry.version_no),
      "activating moves the pointer to the version this drawer chose"
    ).toEqual([newNo]);
    expect(
      afterActivation.versions.find((entry) => entry.version_no === activeNo)?.status,
      "and demotes the version that was active"
    ).toBe("superseded");
    const signedAfter = await settledBody<TAccessUrl>(page, previews, 1, "the preview signed after activation");
    expect(signedAfter.version_no, "the preview follows the newly active version").toBe(newNo);
    await expect(page.getByTestId(DRAWER)).toContainText(`Signed link for v${newNo}`);
    expect(
      (await renderedVersions(page)).map((entry) => entry.status),
      "the history's statuses follow the payload the drawer just consumed"
    ).toEqual(afterActivation.versions.map((entry) => entry.status));

    // --- per-version download, signed for the version the row names -----------
    await page.getByTestId(`files-drawer-version-download-${activeNo}`).click();
    const download = await settledBody<TAccessUrl>(page, downloads, 0, "the download the drawer asked for");
    expect(new URL(downloads[0]?.url ?? "").searchParams.get("version"), "for the version the row names").toBe(
      String(activeNo)
    );
    expect(download.disposition, "and forced to attachment (R-DL-1)").toBe("attachment");
    expect(download.version_no, "the signed URL names that version").toBe(activeNo);
    expect(download.file_name, "with the file's own display name").toBe(
      (await getDetail(page, created.fileId)).file.name_display
    );

    // --- the other answer: Make active, from a fresh revision -----------------
    const thirdBytes = textFixture(`${marker}-rev2`, 1_200).content;
    await page.getByTestId("files-drawer-version-input").setInputFiles({
      name,
      mimeType: "text/plain",
      buffer: thirdBytes,
    });
    await expect(dialog, "the second revision asks again").toBeVisible();
    const secondCompletion = await settledBody<TCompletion>(page, completions, 1, "the second finalize");
    const thirdNo = secondCompletion.version.version_no;
    expect(secondCompletion.activation_required, "this revision is not active either").toBe(true);
    await expect(dialog).toContainText(`Make v${thirdNo} the active version?`);

    await page.getByTestId("files-drawer-activation-confirm").click();
    await expect(dialog).toBeHidden();
    await expect(page.getByTestId("files-drawer-notice")).toHaveText(`Version ${thirdNo} is now active.`);
    const madeActive = await getDetail(page, created.fileId);
    expect(
      madeActive.versions.filter((entry) => entry.is_active).map((entry) => entry.version_no),
      "Make active moves the pointer to the revision the question named"
    ).toEqual([thirdNo]);
    expect(madeActive.file.current_version_no, "and the file's own pointer follows it").toBe(thirdNo);
    expect(
      (await settledBody<TAccessUrl>(page, previews, 3, "the preview after making the revision active")).version_no,
      "the preview signs the version the answer chose"
    ).toBe(thirdNo);

    // --- previewing another version changes nothing but the signed URL --------
    // Last, because choosing a version pins the preview to it: the panel keeps showing the
    // version the user picked, labelled as not active, until they pick again.
    await page.getByTestId("files-drawer-preview-version").selectOption(String(activeNo));
    const other = await settledBody<TAccessUrl>(page, previews, 4, "the preview for the chosen version");
    expect(other.version_no, "the chosen version is the one signed").toBe(activeNo);
    expect(other.url, "and it is a URL of its own").not.toBe(signedAfter.url);
    await expect(page.getByTestId("files-drawer-preview")).toContainText(`Previewing v${activeNo} (not active)`);
    expect(
      (await getDetail(page, created.fileId)).versions
        .filter((entry) => entry.is_active)
        .map((entry) => entry.version_no),
      "previewing a version never moves the active pointer"
    ).toEqual([thirdNo]);
  });

  test("rename_move_restore_and_purge_follow_the_api", async ({ page }) => {
    const stamp = Date.now();
    const marker = `drawer-ops-${stamp}`;
    await signIn(page, OWNER_EMAIL, OWNER_PASSWORD);

    const created = await createFile(page, {
      name: `${marker}.txt`,
      mimeType: "text/plain",
      content: textFixture(marker).content,
    });
    const folderName = `Drawer Ops ${stamp}`;
    const folderId = await createFolder(page, folderName);

    const details = captureResponses(page, (url) => url.pathname === `${FILES_PATH}${created.fileId}/`);
    const patches = captureResponses(page, (url) => url.pathname === `${FILES_PATH}${created.fileId}/`, "PATCH");

    await openDrawerByDeepLink(page, created.fileId);
    const opened = await settledBody<TDetail>(page, details, 0, "the drawer's own detail request");
    const objectKey = opened.file.object_key;
    expect(objectKey, "the API reports the key the file is stored under").not.toBe("");

    // --- rename: display name only --------------------------------------------
    const renamed = `${marker}-renamed.txt`;
    await page.getByTestId("files-drawer-rename").click();
    const renameModal = page.getByTestId("files-drawer-rename-modal");
    await expect(renameModal, "Rename opens its own question").toBeVisible();
    await expect(page.getByTestId("files-drawer-rename-input"), "prefilled with the current name").toHaveValue(
      opened.file.name_display
    );
    await page.getByTestId("files-drawer-rename-input").fill(renamed);
    await page.getByTestId("files-drawer-rename-confirm").click();
    await expect(renameModal).toBeHidden();
    await settledBody<TDetail>(page, details, 1, "the detail the drawer refetched after renaming");
    expect(patches.length, "the rename is the drawer's own write").toBeGreaterThan(0);
    expect((await getDetail(page, created.fileId)).file.name_display, "the API holds the name this test typed").toBe(
      renamed
    );
    await expect(page.getByTestId(DRAWER).locator("h2"), "and the drawer shows what it refetched").toHaveText(renamed);
    expect(await metaValue(page, "Object key"), "a rename never rewrites the key").toBe(objectKey);

    const renamedListing = await snapshotListing(page, "drawer-renamed", APP_FILES_URL);
    await expectViewMatchesBody(page, renamedListing.live, "drawer-renamed");
    expect(
      renamedListing.live.results.find((entry) => entry.id === created.fileId)?.name_display,
      "the listing the API answers carries the new name"
    ).toBe(renamed);

    // --- move: the picker walks the listing, the API moves the metadata -------
    await openDrawerByDeepLink(page, created.fileId);
    await page.getByTestId("files-drawer-move").click();
    const moveModal = page.getByTestId("files-drawer-move-modal");
    await expect(moveModal, "Move opens its own picker").toBeVisible();
    const destination = page.getByTestId(`files-drawer-move-folder-${folderId}`);
    await expect(destination, "the picker offers the folders the listing answers for this level").toBeVisible();
    await expect(destination).toContainText(folderName);
    await destination.click();
    await expect(page.getByTestId("files-drawer-move-destination"), "choosing names the destination").toHaveText(
      `Destination: ${folderName}`
    );
    await page.getByTestId("files-drawer-move-confirm").click();
    await expect(moveModal).toBeHidden();
    await expect(page.getByTestId("files-drawer-notice")).toHaveText(`Moved to ${folderName}.`);

    const moved = await getDetail(page, created.fileId);
    expect(moved.file.folder_id, "the API holds the folder the picker chose").toBe(folderId);
    expect(moved.file.object_key, "a move within the project changes metadata only").toBe(objectKey);
    expect(await metaValue(page, "Object key"), "which the drawer's own metadata block shows").toBe(objectKey);

    const destinationListing = await snapshotListing(page, "drawer-moved", `${APP_FILES_URL}?folder=${folderId}`);
    await expectViewMatchesBody(page, destinationListing.live, "drawer-moved");
    expect(
      destinationListing.live.results.map((entry) => entry.id),
      "the file is in the listing of the folder it was moved to"
    ).toContain(created.fileId);
    expect(
      (await getListBody(page, { folder_id: "root" })).results.map((entry) => entry.id),
      "and has left the listing of the project root it was moved out of"
    ).not.toContain(created.fileId);
    expect(
      (await getListBody(page)).results.find((entry) => entry.id === created.fileId)?.folder_id,
      "every listing that still carries the row reports the folder it moved to"
    ).toBe(folderId);

    // --- trash, then restore from the drawer ----------------------------------
    const trashedResponse = await apiWrite(page, "delete", `${listUrl()}${created.fileId}/`);
    expect(trashedResponse.status(), "the API moves the file to the trash").toBe(204);
    const trashedDetail = await getDetail(page, created.fileId, { trashed: true });
    expect(trashedDetail.file.trashed, "and reports it as trashed").toBe(true);
    expect(trashedDetail.permissions.can_download, "a trashed file has nothing to serve, so the payload says so").toBe(
      false
    );

    const trashedDetails = captureResponses(page, (url) => url.pathname === `${FILES_PATH}${created.fileId}/`);
    await openDrawerByDeepLink(page, created.fileId, "&view=trash");
    const inTrash = await settledBody<TDetail>(
      page,
      trashedDetails,
      0,
      "the drawer's detail request in the trash view"
    );
    expect(inTrash.file.trashed, "the trash deep link opens the trashed file").toBe(true);
    await expect(page.getByTestId(DRAWER), "and the drawer says it is not downloadable").toContainText(
      NOT_DOWNLOADABLE_COPY
    );
    await expect(page.getByTestId("files-drawer-download"), "so no download is offered").toHaveCount(0);
    const restoreButton = page.getByTestId("files-drawer-restore");
    await expect(restoreButton, "Restore is the affordance a trashed file has").toBeVisible();
    await expect(page.getByTestId("files-drawer-rename"), "and editing is not offered while trashed").toHaveCount(0);
    await expect(page.getByTestId("files-drawer-purge"), "the purge is offered to this project's ADMIN").toBeVisible();

    await restoreButton.click();
    await expect(page.getByTestId("files-drawer-notice")).toHaveText(`Restored ${renamed}.`);
    const restored = await getDetail(page, created.fileId);
    expect(restored.file.trashed, "the API's own row is live again").toBe(false);
    expect(restored.file.folder_id, "restored to the folder it was in").toBe(folderId);
    await expect(page.getByTestId("files-drawer-rename"), "and the drawer offers its edits again").toBeVisible();

    const restoredListing = await snapshotListing(page, "drawer-restored", `${APP_FILES_URL}?folder=${folderId}`);
    await expectViewMatchesBody(page, restoredListing.live, "drawer-restored");
    expect(
      restoredListing.live.results.map((entry) => entry.id),
      "the restored file is listed in its folder again"
    ).toContain(created.fileId);

    // --- trash again, then purge, through its confirmation --------------------
    const trashedAgain = await apiWrite(page, "delete", `${listUrl()}${created.fileId}/`);
    expect(trashedAgain.status(), "the file goes to the trash a second time").toBe(204);

    await openDrawerByDeepLink(page, created.fileId, "&view=trash");
    await page.getByTestId("files-drawer-purge").click();
    const purgeModal = page.getByTestId("files-drawer-purge-modal");
    await expect(purgeModal, "the purge asks first").toBeVisible();
    await expect(purgeModal).toContainText(`Permanently delete ${renamed}? This cannot be undone.`);

    // A question owns Escape while it is open: it closes the question, not the drawer.
    await page.keyboard.press("Escape");
    await expect(purgeModal, "Escape closes the question").toBeHidden();
    await expect(page.getByTestId(DRAWER), "and leaves the drawer it was asked from").toBeVisible();

    await page.getByTestId("files-drawer-purge").click();
    await expect(purgeModal).toBeVisible();
    await page.getByTestId("files-drawer-purge-confirm").click();
    await expect(page.getByTestId(DRAWER), "purging closes the drawer: there is nothing left to show").toBeHidden();
    expect(new URL(page.url()).searchParams.get("file"), "and drops the file from the URL").toBeNull();

    const goneResponse = await page.request.get(`${listUrl()}${created.fileId}/?trashed=true`);
    expect(goneResponse.status(), "the API no longer answers for the purged file").toBe(404);

    const afterPurge = await snapshotListing(page, "drawer-purged", APP_FILES_URL);
    await expectViewMatchesBody(page, afterPurge.live, "drawer-purged");
    expect(
      afterPurge.live.results.map((entry) => entry.id),
      "and the listing no longer carries it"
    ).not.toContain(created.fileId);
  });

  test("a_guest_gets_a_mutation_free_drawer", async ({ browser }) => {
    const stamp = Date.now();
    const marker = `drawer-guest-${stamp}`;
    const ownerContext = await browser.newContext({ baseURL: WEB_URL, ignoreHTTPSErrors: true });
    const guestContext = await browser.newContext({
      baseURL: WEB_URL,
      ignoreHTTPSErrors: true,
      viewport: { width: 1440, height: 900 },
    });

    try {
      const ownerPage = await ownerContext.newPage();
      await signIn(ownerPage, OWNER_EMAIL, OWNER_PASSWORD);
      const created = await createFile(ownerPage, {
        name: `${marker}.txt`,
        mimeType: "text/plain",
        content: textFixture(marker).content,
      });

      const page = await guestContext.newPage();
      await signIn(page, GUEST_EMAIL, GUEST_PASSWORD);

      const details = captureResponses(page, (url) => url.pathname === `${FILES_PATH}${created.fileId}/`);
      await openDrawerByDeepLink(page, created.fileId);
      const detail = await settledBody<TDetail>(page, details, 0, "the detail the guest's drawer consumed");
      expect(details[0]?.status, "a guest may read the file's detail").toBe(200);

      expect(detail.permissions.can_edit, "the API reports the guest's affordances").toBe(false);
      expect(detail.permissions.can_delete, "a guest may not mutate").toBe(false);
      await expect(page.getByTestId(DRAWER).locator("h2"), "the guest sees the file").toHaveText(
        detail.file.name_display
      );

      // The reads a guest is allowed: the preview, the versions and the audit history.
      await expect(page.getByTestId("files-drawer-versions")).toBeVisible();
      await expect(page.getByTestId("files-drawer-activity")).toBeVisible();

      // No mutation affordance exists in the guest's drawer — not a disabled control, no
      // control at all (EXP-001 F-01), each absence following the payload's own permissions.
      for (const testId of [
        "files-drawer-upload-version",
        "files-drawer-rename",
        "files-drawer-move",
        "files-drawer-restore",
        "files-drawer-purge",
      ]) {
        await expect(page.getByTestId(testId), `${testId} must not be rendered for a guest`).toHaveCount(0);
      }
      expect(
        await page.locator('[data-testid^="files-drawer-version-activate-"]').count(),
        "and no version is offered for activation"
      ).toBe(0);
      expect(
        await page.locator('[data-testid^="files-drawer-version-download-"]').count(),
        "per-version download follows the payload's can_download"
      ).toBe(detail.permissions.can_download ? detail.versions.length : 0);
      await expect(
        page.getByTestId("files-drawer-download"),
        "and so does the file's own Download, the one read a guest is allowed"
      ).toHaveCount(detail.permissions.can_download ? 1 : 0);

      // Nothing a guest could press in the drawer writes: the dialogs are absent with the
      // controls that open them.
      for (const testId of [
        "files-drawer-rename-modal",
        "files-drawer-move-modal",
        "files-drawer-purge-modal",
        "files-drawer-activation-modal",
      ]) {
        await expect(page.getByTestId(testId), `${testId} must not exist for a guest`).toHaveCount(0);
      }
    } finally {
      await ownerContext.close();
      await guestContext.close();
    }
  });
});
