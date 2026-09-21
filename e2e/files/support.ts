/**
 * The Files harness shared by `browse.spec.ts` (T-112) and `upload.spec.ts` (T-113):
 * the API base and the session, the live-response capture every assertion compares
 * against, and the `files-*` test ids the contract freezes.
 *
 * Evidence rule this module implements (ADV-001 §4.1/§4.2): the rendered DOM is
 * compared against the API response **this test consumed** — captured with
 * `page.waitForResponse` for the exact query the view issued and re-fetched with
 * `page.request.get` for that same URL. Nothing compares the DOM against a fixture
 * written down in the specs; the only literals there are the copies and the test ids
 * this ticket's contract fixes (DEC-003 and REQ-001).
 *
 * The harness (`e2e/files/run.sh`) seeds the workspace, the project, both members, the
 * folder tree and the files through the API; every piece of state the specs add is
 * created through the API with `page.request` after signing in, never by hand.
 *
 * The runs are single-worker (`workers: 1` in `playwright.files.config.ts`): the specs
 * share one project fixture and must not opt into parallel mode.
 */

import { expect, test, type APIResponse, type Page, type Response } from "@playwright/test";

// A browser is driven one step at a time: the loops below must await each step, and
// the retry helpers exist precisely to observe sequential state.
/* oxlint-disable no-await-in-loop */

export function requiredEnv(name: string): string {
  const value = process.env[name];
  if (!value) {
    throw new Error(`${name} is not set. Run the harness: pnpm test:e2e:files (e2e/files/run.sh).`);
  }
  return value;
}

export const API_URL = requiredEnv("E2E_API_URL").replace(/\/+$/, "");
export const WEB_URL = (process.env.E2E_WEB_URL ?? "http://127.0.0.1:3000").replace(/\/+$/, "");
export const WORKSPACE_SLUG = requiredEnv("E2E_WORKSPACE_SLUG");
export const PROJECT_ID = requiredEnv("E2E_PROJECT_ID");
/** The seeded work items, one per spec that needs its own (T-115). */
export const ISSUE_UNLINK_ID = requiredEnv("E2E_ISSUE_UNLINK_ID");
export const ISSUE_UPLOAD_ID = requiredEnv("E2E_ISSUE_UPLOAD_ID");
export const OWNER_EMAIL = requiredEnv("E2E_EMAIL");
export const OWNER_PASSWORD = requiredEnv("E2E_PASSWORD");
export const GUEST_EMAIL = requiredEnv("E2E_GUEST_EMAIL");
export const GUEST_PASSWORD = requiredEnv("E2E_GUEST_PASSWORD");

/** The list endpoint's path, as the web app's service builds it. */
const LIST_PATH = `/api/workspaces/${WORKSPACE_SLUG}/projects/${PROJECT_ID}/files/`;
/** The tab's own route (`/files`), the page the nav item points at. */
const APP_FILES_PATH = `/${WORKSPACE_SLUG}/projects/${PROJECT_ID}/files`;
export const APP_FILES_URL = `${WEB_URL}${APP_FILES_PATH}`;

type TUploader = { id: string; display_name: string };

export type TFileRow = {
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

export type TFolderRow = { id: string; name: string; parent_id: string | null; depth: number };

type TBreadcrumb = { id: string; name: string; depth: number };

type TStorageBlock = {
  project_used_bytes: number;
  workspace_used_bytes: number;
  limit_bytes: number;
  warn_threshold_pct: number;
  file_count: number;
  version_count: number;
};

export type TListBody = {
  results: TFileRow[];
  folders: TFolderRow[];
  breadcrumbs: TBreadcrumb[];
  page: { next_cursor: string | null; prev_cursor: string | null; total_results: number };
  storage: TStorageBlock;
};

export type TSnapshot = {
  /** The exact URL the view asked for, including the query it chose. */
  url: string;
  /** The body the view consumed. */
  live: TListBody;
  /** The body this test fetched for the same URL with `page.request.get`. */
  refetched: TListBody;
};

export type TRenderedRow = {
  fileId: string;
  name: string;
  size: string;
  kind: string;
  owner: string;
  updated: string;
  pinned: string;
};

export function listUrl(params: Record<string, string> = {}): string {
  const query = new URLSearchParams(params).toString();
  return `${API_URL}${LIST_PATH}${query ? `?${query}` : ""}`;
}

export function isListUrl(url: URL): boolean {
  return url.pathname === LIST_PATH;
}

export function isListResponse(response: Response): boolean {
  return isListUrl(new URL(response.url()));
}

export function isDetailResponse(response: Response): boolean {
  const path = new URL(response.url()).pathname;
  if (!path.startsWith(LIST_PATH)) return false;
  return /^[0-9a-fA-F-]{36}\/$/.test(path.slice(LIST_PATH.length));
}

// --- the DOM, read exactly as the contract freezes it -----------------------

/** The frozen test ids this file reads, so a selector cannot drift from its reader. */
export const ROW_SELECTOR = '[data-testid^="files-row-"]';
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

export async function readRows(page: Page): Promise<TRenderedRow[]> {
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

export async function focusedFileId(page: Page): Promise<string | null> {
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
export async function readFocusDecoration(
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
export async function tabUntilRowFocused(page: Page, maxPresses = 200): Promise<string> {
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
export const mutatedFileIds = new Set<string>();

export async function expectRowsMatchBody(page: Page, body: TListBody, label: string): Promise<void> {
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

export async function expectStorageMatchesBody(page: Page, body: TListBody, label: string): Promise<void> {
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

export async function expectViewMatchesBody(page: Page, body: TListBody, label: string): Promise<void> {
  await expectRowsMatchBody(page, body, label);
  await expectFoldersMatchBody(page, body, label);
  await expectBreadcrumbsMatchBody(page, body, label);
  await expectStorageMatchesBody(page, body, label);
}

// --- capturing a list request and its body ----------------------------------

/**
 * The `folder_id` the view asks for when the app URL lives at `url`, or `null` when the
 * view asks for no folder at all.
 *
 * The live browse scopes to one folder and names the root explicitly (`folder_id=root`,
 * DEFECT-005): an app URL with no `folder` - or with `folder=root` - is the project root,
 * which is a different question from an absent `folder_id` ("every file in the project, at
 * any depth"). The Trash, Pinned and Recent quick views are project-wide by purpose - they
 * exist to find a file again, and a file trashed through its folder keeps a `folder_id`
 * whose folder no longer resolves - so they are asked for with no `folder_id` at all
 * (T-118 F-1). One definition, so the mapping below and the drawer spec's capture check
 * cannot disagree about which listing an app URL names.
 */
export function folderQueryFromAppUrl(url: string): string | null {
  const params = new URL(url).searchParams;
  // Only the live browse is folder-scoped (see `buildListQuery`): the Trash, Pinned and
  // Recent quick views are project-wide, so they ask for no `folder_id` at all.
  if ((params.get("view") ?? "all") !== "all") return null;
  const folder = params.get("folder");
  return folder && folder !== "root" ? folder : "root";
}

function paramsFromAppUrl(page: Page): Record<string, string> {
  const app = new URL(page.url()).searchParams;
  const params: Record<string, string> = {};
  // The query the view actually issues for this app URL, root included where the view is
  // folder-scoped; an absent `folder_id` is a different question from the root.
  const folder = folderQueryFromAppUrl(page.url());
  if (folder) params.folder_id = folder;
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

export async function snapshotList(
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
    // Only a snapshot that insists on a live fetch may fail here; for every other one a
    // discarded body is just another reason to fall back to the re-fetch below.
    if (options.requireLive) {
      expect(response, `${label}: the view must ask for this URL at least once`).not.toBeNull();
    }
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
export async function apiWrite(
  page: Page,
  method: "post" | "patch" | "delete",
  url: string,
  data?: Record<string, unknown>
): Promise<APIResponse> {
  const tokenResponse = await page.request.get(`${API_URL}/auth/get-csrf-token/`);
  expect(tokenResponse.ok(), "the session needs a CSRF token for its writes").toBe(true);
  const { csrf_token: csrfToken } = (await tokenResponse.json()) as { csrf_token: string };

  const response = await page.request[method](url, {
    data,
    headers: { "X-CSRFToken": csrfToken, Origin: WEB_URL, Referer: `${WEB_URL}/` },
  });
  // A create answers 201, a patch 200, a delete 204: what matters is that the write succeeded.
  expect(response.status(), `${method.toUpperCase()} ${url}`).toBeGreaterThanOrEqual(200);
  expect(response.status(), `${method.toUpperCase()} ${url}`).toBeLessThan(300);
  return response;
}

export async function createFolder(page: Page, name: string): Promise<string> {
  const response = await apiWrite(page, "post", `${listUrl()}folders/`, { name });
  const body = (await response.json()) as { folder: TFolderRow };
  return body.folder.id;
}

export async function getListBody(page: Page, params: Record<string, string> = {}): Promise<TListBody> {
  const url = listUrl(params);
  const response = await page.request.get(url);
  expect(response.ok(), `GET ${url}`).toBe(true);
  return (await response.json()) as TListBody;
}

// --- session ----------------------------------------------------------------

export async function signIn(page: Page, email: string, password: string): Promise<void> {
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

  // A throttled sign-in is a redirect to the auth error page (error_code 5900,
  // RATE_LIMIT_EXCEEDED) instead of a session. Name the throttle here: otherwise the test
  // fails later on whatever it was actually asserting, which is how one throttled attempt
  // became three unrelated-looking failures (DEFECT-007). The stack raises the limit in
  // `run.sh`; this is not retried.
  const redirect = response.headers()["location"] ?? "";
  if (redirect.includes("RATE_LIMIT_EXCEEDED")) {
    throw new Error(
      `POST /auth/sign-in/ for ${email} was throttled by AUTHENTICATION_RATE_LIMIT ` +
        `(default 10/minute per IP; the e2e stack raises it in run.sh). Redirect: ${redirect}`
    );
  }

  const me = await page.request.get(`${API_URL}/api/users/me/`);
  expect(me.status(), `signing in ${email} must establish an API session`).toBe(200);
  expect(((await me.json()) as { email: string }).email, "the signed-in identity").toBe(email);
}

// --- session ----------------------------------------------------------------

/**
 * How long the first Files navigation of a run may take. A cold dev container compiles
 * the route on its first hit — and can restart the server while it optimises the
 * dependencies the route pulls in — which is far slower than any measured transition.
 * `global-setup.ts` spends this budget once per run, before any test's timeout starts;
 * the same tolerance here keeps a navigation issued later in the run from failing on
 * the same cold path.
 */
const FILES_BOOT_BUDGET_MS = 240_000;

/** How long one attempt waits for the rendered root once its document has answered. */
const FILES_BOOT_ATTEMPT_MS = 15_000;

export async function openFilesTab(page: Page, url = APP_FILES_URL): Promise<void> {
  const root = page.getByTestId("files-root");
  const deadline = Date.now() + FILES_BOOT_BUDGET_MS;

  for (;;) {
    try {
      // A route the dev server has not compiled yet answers slowly, and the server can
      // drop the document while it restarts: the navigation carries the boot tolerance
      // instead of the 30 s default, which would fail the test before the retry below.
      await page.goto(url, { timeout: 90_000 });
    } catch {
      if (Date.now() >= deadline) throw new Error(`${url} did not answer within ${FILES_BOOT_BUDGET_MS} ms`);
      continue;
    }

    try {
      await root.waitFor({ state: "visible", timeout: FILES_BOOT_ATTEMPT_MS });
      return;
    } catch {
      // The auth wrapper can still be hydrating right after sign-in; retrying the
      // navigation is cheaper than guessing at its redirect timing.
    }

    if (Date.now() >= deadline) break;
  }

  await expect(root, "the Files root must be on screen once the boot budget is spent").toBeVisible();
}

/**
 * Warm the Files route once per run. `global-setup.ts` calls this before the first
 * test, outside every test timeout, so the cold compile is paid once and the measured
 * transitions start on a server that has already served the route — the second
 * navigation is the proof that it is warm.
 */
export async function primeFilesRoute(page: Page): Promise<void> {
  await signIn(page, OWNER_EMAIL, OWNER_PASSWORD);
  await openFilesTab(page);
  await openFilesTab(page);
}
