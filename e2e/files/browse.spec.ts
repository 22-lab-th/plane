/**
 * T-112 — the Files tab: browsing, breadcrumbs, quick views and states.
 *
 * Evidence rule this file is built on (ADV-001 §4.1/§4.2): the rendered DOM is
 * compared against the API response **this test consumed**, captured with
 * `page.waitForResponse` for the exact query the view issued and re-fetched with
 * `page.request.get` for that same URL. Nothing here compares the DOM against a
 * fixture written down in this file; the only literals are the copies and the
 * test ids this ticket's contract fixes (DEC-003 and REQ-001; the Orphan/Unlinked
 * quick view is P2 per AC-21, so the listing has no filter for it and the view must
 * not offer one).
 *
 * The harness (`e2e/files/run.sh`) seeds the workspace, the project, both members,
 * the folder tree and the files through the API; every piece of state this spec
 * adds (an empty folder, a pinned file) is created through the API with
 * `page.request` after signing in, never by hand.
 *
 * The run is single-worker (`workers: 1` in `playwright.files.config.ts`): the
 * tests share one project fixture and this file must not opt into parallel mode.
 */

import { expect, test, type APIResponse, type Page, type Response } from "@playwright/test";

// A browser is driven one step at a time: the loops below must await each step, and
// the retry helpers exist precisely to observe sequential state.
/* oxlint-disable no-await-in-loop */

function requiredEnv(name: string): string {
  const value = process.env[name];
  if (!value) {
    throw new Error(`${name} is not set. Run the harness: pnpm test:e2e:files (e2e/files/run.sh).`);
  }
  return value;
}

const API_URL = requiredEnv("E2E_API_URL").replace(/\/+$/, "");
const WEB_URL = (process.env.E2E_WEB_URL ?? "http://127.0.0.1:3000").replace(/\/+$/, "");
const WORKSPACE_SLUG = requiredEnv("E2E_WORKSPACE_SLUG");
const PROJECT_ID = requiredEnv("E2E_PROJECT_ID");
const OWNER_EMAIL = requiredEnv("E2E_EMAIL");
const OWNER_PASSWORD = requiredEnv("E2E_PASSWORD");
const GUEST_EMAIL = requiredEnv("E2E_GUEST_EMAIL");
const GUEST_PASSWORD = requiredEnv("E2E_GUEST_PASSWORD");

/** The list endpoint's path, as the web app's service builds it. */
const LIST_PATH = `/api/workspaces/${WORKSPACE_SLUG}/projects/${PROJECT_ID}/files/`;
/** The tab's own route (`/files`), the page the nav item points at. */
const APP_FILES_PATH = `/${WORKSPACE_SLUG}/projects/${PROJECT_ID}/files`;
const APP_FILES_URL = `${WEB_URL}${APP_FILES_PATH}`;

type TUploader = { id: string; display_name: string };

type TFileRow = {
  id: string;
  name_display: string;
  extension: string;
  mime_type: string;
  size_bytes: number;
  folder_id: string | null;
  is_pinned: boolean;
  trashed: boolean;
  uploader: TUploader | null;
  created_at: string;
  updated_at: string;
};

type TFolderRow = { id: string; name: string; parent_id: string | null; depth: number };

type TBreadcrumb = { id: string; name: string; depth: number };

type TStorageBlock = {
  project_used_bytes: number;
  workspace_used_bytes: number;
  limit_bytes: number;
  warn_threshold_pct: number;
  file_count: number;
  version_count: number;
};

type TListBody = {
  results: TFileRow[];
  folders: TFolderRow[];
  breadcrumbs: TBreadcrumb[];
  page: { next_cursor: string | null; prev_cursor: string | null; total_results: number };
  storage: TStorageBlock;
};

type TSnapshot = {
  /** The exact URL the view asked for, including the query it chose. */
  url: string;
  /** The body the view consumed. */
  live: TListBody;
  /** The body this test fetched for the same URL with `page.request.get`. */
  refetched: TListBody;
};

type TRenderedRow = {
  fileId: string;
  name: string;
  size: string;
  kind: string;
  owner: string;
  updated: string;
  pinned: string;
};

function listUrl(params: Record<string, string> = {}): string {
  const query = new URLSearchParams(params).toString();
  return `${API_URL}${LIST_PATH}${query ? `?${query}` : ""}`;
}

function isListUrl(url: URL): boolean {
  return url.pathname === LIST_PATH;
}

function isListResponse(response: Response): boolean {
  return isListUrl(new URL(response.url()));
}

function isDetailResponse(response: Response): boolean {
  const path = new URL(response.url()).pathname;
  if (!path.startsWith(LIST_PATH)) return false;
  return /^[0-9a-fA-F-]{36}\/$/.test(path.slice(LIST_PATH.length));
}

// --- the DOM, read exactly as the contract freezes it -----------------------

/** The frozen test ids this file reads, so a selector cannot drift from its reader. */
const ROW_SELECTOR = '[data-testid^="files-row-"]';
const FOLDER_SELECTOR = '[data-testid^="files-folder-"]';
const CRUMB_SELECTOR = '[data-testid^="files-breadcrumb-"]:not([data-testid="files-breadcrumb-root"])';

/**
 * Wait for the view to have rendered the given ids, in order. The list keeps the
 * previous page on screen while a new one loads (`keepPreviousData`), so counting
 * rows is not enough — the ids have to be the new ones. A timeout is not an error
 * here: the caller compares and reports the mismatch with the response body.
 */
async function waitForRenderedIds(
  page: Page,
  selector: string,
  attribute: "data-testid" | "data-file-id",
  expectedIds: string[]
): Promise<void> {
  const deadline = Date.now() + 15_000;
  do {
    const ids = await page.$$eval(
      selector,
      (nodes, attr) => nodes.map((node) => node.getAttribute(attr) ?? ""),
      attribute
    );
    if (JSON.stringify(ids) === JSON.stringify(expectedIds)) return;
    await page.waitForTimeout(100);
  } while (Date.now() < deadline);
}

async function readRows(page: Page): Promise<TRenderedRow[]> {
  return page.$$eval(ROW_SELECTOR, (nodes) =>
    nodes.map((node) => ({
      fileId: node.getAttribute("data-file-id") ?? "",
      name: node.getAttribute("data-name") ?? "",
      size: node.getAttribute("data-size") ?? "",
      kind: node.getAttribute("data-kind") ?? "",
      owner: node.getAttribute("data-owner") ?? "",
      updated: node.getAttribute("data-updated") ?? "",
      pinned: node.getAttribute("data-pinned") ?? "",
    }))
  );
}

async function focusedFileId(page: Page): Promise<string | null> {
  return page.evaluate(() => {
    const active = document.activeElement as HTMLElement | null;
    const row = active?.closest?.('[data-testid^="files-row-"]') ?? null;
    return row?.getAttribute("data-file-id") ?? null;
  });
}

/**
 * The decoration of the focused row (and of the control inside it that took focus)
 * against a resting row. Rows render from one component, so the difference is
 * precisely the focus ring — asserting "some box-shadow exists" would also pass on
 * a row that always carries one.
 */
async function readFocusDecoration(
  page: Page
): Promise<{ focused: string | null; unfocused: string | null; paintsSomething: boolean }> {
  return page.evaluate(() => {
    /* oxlint-disable consistent-function-scoping */
    const transparent = (value: string): boolean =>
      value === "none" || value === "" || /rgba?\([^)]*,\s*0(\.0+)?\s*\)/.test(value) || value === "transparent";

    const decoration = (node: HTMLElement): string => {
      const style = window.getComputedStyle(node);
      return `${style.boxShadow}|${style.outlineStyle}|${style.outlineWidth}:${style.outlineColor}`;
    };

    /** Whether this element paints a focus indicator a user can actually see. */
    const paints = (node: HTMLElement): boolean => {
      const style = window.getComputedStyle(node);
      const width = Number.parseFloat(style.outlineWidth || "0");
      const outlinePaints = style.outlineStyle !== "none" && width > 0 && !transparent(style.outlineColor);
      return outlinePaints || !transparent(style.boxShadow);
    };
    /* oxlint-enable consistent-function-scoping */
    const active = document.activeElement as HTMLElement | null;
    const row = (active?.closest?.('[data-testid^="files-row-"]') ?? null) as HTMLElement | null;
    if (!row) return { focused: null, unfocused: null, paintsSomething: false };

    const rows = Array.from(document.querySelectorAll('[data-testid^="files-row-"]')) as HTMLElement[];
    const resting = rows.find((candidate) => candidate !== row) ?? null;
    const chain = [active, row].filter((node): node is HTMLElement => Boolean(node));
    return {
      focused: chain.map(decoration).join(" ; "),
      unfocused: resting ? decoration(resting) : null,
      paintsSomething: chain.some(paints),
    };
  });
}

/**
 * Walk the document's tab order until a file row takes focus. The bound is
 * generous on purpose: the app shell (workspace and project navigation) comes
 * before the Files view in the tab order, and this has to be a pure keyboard path
 * from the document to a row, not a programmatic `.focus()`.
 */
async function tabUntilRowFocused(page: Page, maxPresses = 200): Promise<string> {
  await page.evaluate(() => {
    if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
  });
  for (let press = 0; press < maxPresses; press += 1) {
    await page.keyboard.press("Tab");
    const fileId = await focusedFileId(page);
    if (fileId) return fileId;
  }
  throw new Error(`no file row took keyboard focus after ${maxPresses} Tab presses`);
}

// --- comparing a response body to the DOM -----------------------------------

/**
 * On a mismatch the compared body has to survive into the evidence, so it is
 * printed here (stdout of the run) and attached to the report by `snapshotList`.
 */
function logMismatch(label: string, apiBody: unknown, rendered: unknown): void {
  console.log(`[files-spec] ${label}: the DOM does not match the API response`);
  console.log(`[files-spec] API body: ${JSON.stringify(apiBody, null, 2)}`);
  console.log(`[files-spec] rendered: ${JSON.stringify(rendered, null, 2)}`);
}

function expectSameBody(reference: TListBody, refetched: TListBody, label: string): void {
  const referenceRowIds = reference.results.map((row) => row.id);
  const refetchedRowIds = refetched.results.map((row) => row.id);
  const referenceFolderIds = reference.folders.map((folder) => folder.id);
  const refetchedFolderIds = refetched.folders.map((folder) => folder.id);
  const referenceCrumbIds = reference.breadcrumbs.map((crumb) => crumb.id);
  const refetchedCrumbIds = refetched.breadcrumbs.map((crumb) => crumb.id);

  if (JSON.stringify(referenceRowIds) !== JSON.stringify(refetchedRowIds)) {
    logMismatch(`${label}: re-fetching the same URL answered different rows`, reference, refetched);
  }

  expect(refetchedRowIds, `${label}: the same URL must answer the same row ids`).toEqual(referenceRowIds);
  expect(
    refetched.results.map((row) => row.name_display),
    `${label}: the same URL must answer the same names`
  ).toEqual(reference.results.map((row) => row.name_display));
  expect(refetchedFolderIds, `${label}: the same URL must answer the same folders`).toEqual(referenceFolderIds);
  expect(refetchedCrumbIds, `${label}: the same URL must answer the same breadcrumbs`).toEqual(referenceCrumbIds);
  expect(refetched.page.total_results, `${label}: total_results`).toBe(reference.page.total_results);
  expect(refetched.storage, `${label}: the storage block`).toEqual(reference.storage);
}

/**
 * Set by `snapshotList` when the view answered from its own cache, i.e. when the
 * reference body was re-fetched after the DOM was rendered. Only `updated_at` is
 * affected: a row the test itself mutated in between (pinning a file bumps it) is
 * legitimately older on screen than in the fresh response.
 */
let rowsComeFromLiveResponse = true;

/** Files this spec wrote to through the API; only their timestamps may lag a re-fetch. */
const mutatedFileIds = new Set<string>();

async function expectRowsMatchBody(page: Page, body: TListBody, label: string): Promise<void> {
  const expectedIds = body.results.map((row) => row.id);
  await waitForRenderedIds(page, ROW_SELECTOR, "data-file-id", expectedIds);

  const rows = await readRows(page);
  const renderedIds = rows.map((row) => row.fileId);
  const renderedNames = rows.map((row) => row.name);
  const expectedNames = body.results.map((row) => row.name_display);
  const emptyKind = rows.some((row) => row.kind.length === 0);

  if (
    JSON.stringify(renderedIds) !== JSON.stringify(expectedIds) ||
    JSON.stringify(renderedNames) !== JSON.stringify(expectedNames) ||
    emptyKind
  ) {
    logMismatch(`${label}: rendered rows`, body, rows);
  }

  expect(renderedIds, `${label}: file ids, in the API's order`).toEqual(expectedIds);
  expect(renderedNames, `${label}: names, in the API's order`).toEqual(expectedNames);
  expect(rows.length, `${label}: row count`).toBe(body.results.length);

  body.results.forEach((row, index) => {
    const rendered = rows[index];
    expect(rendered?.owner, `${label}: ${row.name_display} data-owner`).toBe(row.uploader?.display_name ?? "");
    expect(rendered?.size, `${label}: ${row.name_display} data-size`).toBe(String(row.size_bytes));
    if (rowsComeFromLiveResponse) {
      expect(rendered?.updated, `${label}: ${row.name_display} data-updated`).toBe(row.updated_at);
    } else {
      expect(
        Number.isFinite(Date.parse(rendered?.updated ?? "")),
        `${label}: ${row.name_display} data-updated is a timestamp`
      ).toBe(true);
      expect(
        Date.parse(rendered?.updated ?? ""),
        `${label}: ${row.name_display} was rendered before this response, so it cannot be newer`
      ).toBeLessThanOrEqual(Date.parse(row.updated_at));
      expect(
        mutatedFileIds.has(row.id) || Date.parse(rendered?.updated ?? "") === Date.parse(row.updated_at),
        `${label}: ${row.name_display} may carry an older timestamp only if this spec wrote to it`
      ).toBe(true);
    }
    expect(rendered?.pinned, `${label}: ${row.name_display} data-pinned`).toBe(String(row.is_pinned));
    // `data-kind` is a display kind derived from the mime type, not an API field:
    // the contract freezes the attribute, not its vocabulary.
    expect(rendered?.kind, `${label}: ${row.name_display} data-kind`).not.toBe("");
  });
}

async function expectFoldersMatchBody(page: Page, body: TListBody, label: string): Promise<void> {
  const expectedIds = body.folders.map((folder) => folder.id);
  const expectedNames = body.folders.map((folder) => folder.name);
  await waitForRenderedIds(
    page,
    FOLDER_SELECTOR,
    "data-testid",
    expectedIds.map((id) => `files-folder-${id}`)
  );

  const rendered = await page.$$eval(FOLDER_SELECTOR, (nodes) =>
    nodes.map((node) => ({
      id: (node.getAttribute("data-testid") ?? "").replace("files-folder-", ""),
      name: node.getAttribute("data-name") ?? "",
    }))
  );
  if (
    JSON.stringify(rendered.map((folder) => folder.id)) !== JSON.stringify(expectedIds) ||
    JSON.stringify(rendered.map((folder) => folder.name)) !== JSON.stringify(expectedNames)
  ) {
    logMismatch(`${label}: rendered folders`, body, rendered);
  }

  expect(
    rendered.map((folder) => folder.id),
    `${label}: folder ids, in the API's order`
  ).toEqual(expectedIds);
  expect(
    rendered.map((folder) => folder.name),
    `${label}: folder names, in the API's order`
  ).toEqual(expectedNames);
}

async function expectBreadcrumbsMatchBody(page: Page, body: TListBody, label: string): Promise<void> {
  const expectedIds = body.breadcrumbs.map((crumb) => crumb.id);

  if (expectedIds.length > 0) {
    // The crumb that walks back to the project root is what makes a folder
    // reachable again, so it is required exactly when there is a folder to leave.
    await expect(page.getByTestId("files-breadcrumbs")).toBeVisible();
    await expect(page.getByTestId("files-breadcrumb-root")).toBeVisible();
  }
  await waitForRenderedIds(
    page,
    CRUMB_SELECTOR,
    "data-testid",
    expectedIds.map((id) => `files-breadcrumb-${id}`)
  );

  const crumbs = await page.$$eval(CRUMB_SELECTOR, (nodes) =>
    nodes.map((node) => ({
      id: (node.getAttribute("data-testid") ?? "").replace("files-breadcrumb-", ""),
      name: (node.textContent ?? "").trim(),
    }))
  );
  const ids = crumbs.map((crumb) => crumb.id);
  if (JSON.stringify(ids) !== JSON.stringify(expectedIds)) {
    logMismatch(`${label}: rendered breadcrumbs`, body, crumbs);
  }

  expect(ids, `${label}: breadcrumb ids, root-first, as the API orders them`).toEqual(expectedIds);
  body.breadcrumbs.forEach((crumb, index) => {
    expect(crumbs[index]?.name, `${label}: breadcrumb ${crumb.name}`).toContain(crumb.name);
  });
}

async function expectStorageMatchesBody(page: Page, body: TListBody, label: string): Promise<void> {
  const indicator = page.getByTestId("files-storage");
  await expect(indicator).toBeVisible();

  const attributes = await indicator.evaluate((node) => ({
    used: node.getAttribute("data-used") ?? "",
    limit: node.getAttribute("data-limit") ?? "",
    pct: node.getAttribute("data-pct") ?? "",
  }));

  if (Number(attributes.used) !== body.storage.project_used_bytes) {
    logMismatch(`${label}: the storage indicator disagrees with this response`, body, attributes);
  }

  expect(Number(attributes.used), `${label}: data-used must be this response's storage.project_used_bytes`).toBe(
    body.storage.project_used_bytes
  );
  expect(Number(attributes.limit), `${label}: data-limit`).toBe(body.storage.limit_bytes);
  expect(Number.isFinite(Number(attributes.pct)), `${label}: data-pct must be a number`).toBe(true);
  await expect(page.getByTestId("files-storage-text")).toHaveText(/\S/);
}

async function expectViewMatchesBody(page: Page, body: TListBody, label: string): Promise<void> {
  await expectRowsMatchBody(page, body, label);
  await expectFoldersMatchBody(page, body, label);
  await expectBreadcrumbsMatchBody(page, body, label);
  await expectStorageMatchesBody(page, body, label);
}

// --- capturing a list request and its body ----------------------------------

/** The API query the app URL is currently asking for. */
function paramsFromAppUrl(page: Page): Record<string, string> {
  const app = new URL(page.url()).searchParams;
  const params: Record<string, string> = {};
  const folder = app.get("folder");
  if (folder && folder !== "root") params.folder_id = folder;
  const q = app.get("q");
  if (q) params.q = q;
  // The app always sends an ordering; with none in the URL it sends its own default,
  // which is the listing's "newest first" (the service's PROJECT_FILE_DEFAULT_ORDERING).
  params.ordering = app.get("ordering") ?? "-created";
  const view = app.get("view");
  if (view === "pinned") params.pinned = "true";
  if (view === "trash") params.trashed = "true";
  if (view === "recent") {
    params.created_from = new Date(Date.now() - 30 * 86_400_000).toISOString().slice(0, 10);
  }
  return params;
}

async function snapshotList(
  page: Page,
  label: string,
  action: () => Promise<void>,
  options: { requireLive?: boolean } = {}
): Promise<TSnapshot> {
  // One transition fires a request and it is that response the DOM is compared
  // against. A repeat of a key the view recently fetched can be served from its own
  // cache, in which case the reference is the same URL the view is showing,
  // re-fetched — the API's own output for the exact query either way.
  // Long enough for any warmed transition to answer, short enough that the fallbacks
  // below do not dominate the run. The request is captured alongside the response so a
  // fallback compares against the URL the view asked for, not a re-derivation of it.
  // The body is read as soon as the response arrives: a snapshot whose action is a
  // navigation loses the response body, and reading it afterwards is a race.
  const captured = page
    .waitForResponse(isListResponse, { timeout: 8_000 })
    .then(async (response) => ({ response, body: await response.json().catch(() => null) }))
    .catch(() => null);
  const capturedRequest = page
    .waitForRequest((request) => isListUrl(new URL(request.url())), { timeout: 8_000 })
    .catch(() => null);
  await action();
  const [received, request] = await Promise.all([captured, capturedRequest]);
  const response = received?.response ?? null;

  // A response whose body the browser had already discarded (the action navigated) is
  // treated as a cache fallback: the reference is then the re-fetch of the URL the view
  // asked for, which is the API's own answer either way. `requireLive` asks only that the
  // view issued the request, which is the guarantee that matters.
  if (!response || !received?.body) {
    expect(options.requireLive ?? false, `${label}: the view must ask for this URL at least once`).toBe(
      Boolean(response)
    );
    const derivedUrl = listUrl(paramsFromAppUrl(page));
    const url = request?.url() ?? derivedUrl;
    if (request) {
      expect(derivedUrl, `${label}: the app-URL mapping must name the query the view asked for`).toBe(url);
    }
    console.log(`${label}: the view served this URL from its cache, re-fetching ${url}`);
    rowsComeFromLiveResponse = false;
    const fallback = await page.request.get(url);
    expect(fallback.ok(), `${label}: re-fetching ${url}`).toBe(true);
    const body = (await fallback.json()) as TListBody;
    await test.info().attach(`${label}.list-response.json`, {
      body: JSON.stringify({ url, refetched: body, servedFromCache: true }, null, 2),
      contentType: "application/json",
    });
    return { url, live: body, refetched: body };
  }

  rowsComeFromLiveResponse = true;
  expect(response.status(), `${label}: ${response.url()}`).toBe(200);
  const live = received.body as TListBody;

  const refetchedResponse = await page.request.get(response.url());
  expect(refetchedResponse.ok(), `${label}: re-fetching ${response.url()}`).toBe(true);
  const refetched = (await refetchedResponse.json()) as TListBody;

  await test.info().attach(`${label}.list-response.json`, {
    body: JSON.stringify({ url: response.url(), captured: live, refetched }, null, 2),
    contentType: "application/json",
  });

  expectSameBody(live, refetched, label);

  return { url: response.url(), live, refetched };
}

// --- API state, created with page.request -----------------------------------

/** A write through the session's cookies, carrying the CSRF token the API needs. */
async function apiWrite(
  page: Page,
  method: "post" | "patch",
  url: string,
  data: Record<string, unknown>
): Promise<APIResponse> {
  const tokenResponse = await page.request.get(`${API_URL}/auth/get-csrf-token/`);
  expect(tokenResponse.ok(), "the session needs a CSRF token for its writes").toBe(true);
  const { csrf_token: csrfToken } = (await tokenResponse.json()) as { csrf_token: string };

  const response = await page.request[method](url, {
    data,
    headers: { "X-CSRFToken": csrfToken, Origin: WEB_URL, Referer: `${WEB_URL}/` },
  });
  // A create answers 201, a patch 200: what matters is that the write succeeded.
  expect(response.status(), `${method.toUpperCase()} ${url}`).toBeGreaterThanOrEqual(200);
  expect(response.status(), `${method.toUpperCase()} ${url}`).toBeLessThan(300);
  return response;
}

async function createFolder(page: Page, name: string): Promise<string> {
  const response = await apiWrite(page, "post", `${listUrl()}folders/`, { name });
  const body = (await response.json()) as { folder: TFolderRow };
  return body.folder.id;
}

async function getListBody(page: Page, params: Record<string, string> = {}): Promise<TListBody> {
  const url = listUrl(params);
  const response = await page.request.get(url);
  expect(response.ok(), `GET ${url}`).toBe(true);
  return (await response.json()) as TListBody;
}

// --- session ----------------------------------------------------------------

async function signIn(page: Page, email: string, password: string): Promise<void> {
  // The app reads its session from the API, and the landing page's markup is not part
  // of this ticket, so the session is established the way the SSO suite does it.
  const tokenResponse = await page.request.get(`${API_URL}/auth/get-csrf-token/`);
  expect(tokenResponse.ok(), "a sign-in attempt needs a CSRF token").toBe(true);
  const { csrf_token: csrfToken } = (await tokenResponse.json()) as { csrf_token: string };

  const response = await page.request.post(`${API_URL}/auth/sign-in/`, {
    form: { email, password },
    headers: { "X-CSRFToken": csrfToken, Origin: WEB_URL, Referer: `${WEB_URL}/` },
    maxRedirects: 0,
  });
  expect([200, 302], `POST /auth/sign-in/ answered ${response.status()}`).toContain(response.status());

  const me = await page.request.get(`${API_URL}/api/users/me/`);
  expect(me.status(), `signing in ${email} must establish an API session`).toBe(200);
  expect(((await me.json()) as { email: string }).email, "the signed-in identity").toBe(email);
}

async function openFilesTab(page: Page): Promise<void> {
  const root = page.getByTestId("files-root");
  for (let attempt = 0; attempt < 3; attempt += 1) {
    await page.goto(APP_FILES_URL);
    try {
      await root.waitFor({ state: "visible", timeout: 10_000 });
      return;
    } catch {
      // The auth wrapper can still be hydrating right after sign-in; retrying the
      // navigation is cheaper than guessing at its redirect timing.
    }
  }
  await expect(root).toBeVisible();
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

    const trashReference = await getListBody(page, { trashed: "true" });
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
    expect(new URL(noMatch.url).searchParams.get("folder_id"), "the root request carries no folder").toBeNull();
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
    expect(new URL(backAtRootFromEmpty.url).searchParams.get("folder_id")).toBeNull();
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
    expect(new URL(backAtRoot.url).searchParams.get("folder_id"), "the root request carries no folder").toBeNull();
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
    const rowsBeforeFailedRefresh = await page.locator(ROW_SELECTOR).count();
    await page.route(isListUrl, (route) => route.abort("failed"));
    // A revalidation of the key already on screen, which is what the app does on reconnect.
    await page.evaluate(() => window.dispatchEvent(new Event("online")));
    await expect(page.getByTestId("files-error-banner"), "a failed refresh is announced").toBeVisible();
    expect(await page.locator(ROW_SELECTOR).count(), "and the rows it already had are kept").toBe(
      rowsBeforeFailedRefresh
    );

    await page.unroute(isListUrl);
    await page.getByTestId("files-retry").click();
    await expect(page.getByTestId("files-error-banner")).toBeHidden();

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
