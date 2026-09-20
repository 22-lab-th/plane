/**
 * T-113 — the Files tab's upload surface: the picker and a real drag-and-drop, per-file
 * progress, the client-side pre-check, the name-collision decision, cancel in flight and a
 * retry, and the guest's absence of any upload affordance.
 *
 * Evidence rule (ADV-001 §4.1/§4.2), the same one `browse.spec.ts` states in full and
 * `./support` implements: every rendered value is compared against the API response this
 * test consumed — the `initiate-upload`/`complete-upload` payloads for a queue row, the
 * listing response for the listing, and the object store itself for the bytes an upload
 * claims to have stored. Nothing here compares the DOM against a fixture written down in
 * this file: the only literals are the test ids and the copy the ticket's design fixes.
 *
 * The object store is read directly, with a signed request, because "the row says the file
 * is there" and "the bytes are there" are different claims (AC-03, R-UPL-3): the harness
 * exports the store's address and credentials for exactly this.
 */

// Node imports
import { createHash, createHmac } from "node:crypto";
import { setTimeout as delay } from "node:timers/promises";
// Playwright imports
import { expect, test, type Page, type Route } from "@playwright/test";
// harness
import {
  APP_FILES_URL,
  createFolder,
  expectViewMatchesBody,
  GUEST_EMAIL,
  GUEST_PASSWORD,
  listUrl,
  openFilesTab,
  OWNER_EMAIL,
  OWNER_PASSWORD,
  readRows,
  requiredEnv,
  signIn,
  snapshotList,
  WEB_URL,
  type TFileRow,
  type TSnapshot,
} from "./support";

// A browser is driven one step at a time: the loops below must await each step, and the
// queue's own transitions are observed over time rather than sampled once.
/* oxlint-disable no-await-in-loop */

const MINIO_URL = requiredEnv("E2E_MINIO_URL").replace(/\/+$/, "");
const MINIO_BUCKET = requiredEnv("E2E_MINIO_BUCKET");
const MINIO_ACCESS_KEY = requiredEnv("E2E_MINIO_ACCESS_KEY");
const MINIO_SECRET_KEY = requiredEnv("E2E_MINIO_SECRET_KEY");
const MINIO_REGION = requiredEnv("E2E_MINIO_REGION");

/** The element the view registers its drop handlers on. */
const FILES_ROOT = '[data-testid="files-root"]';
const UPLOAD_ROW_SELECTOR = '[data-testid^="files-upload-row-"]';

// --- the API's own upload payloads ------------------------------------------

type TUploadFile = {
  id: string;
  name_display: string;
  category: string;
  object_key: string;
  folder_id: string | null;
};

type TInitiation = {
  file: TUploadFile;
  version_no: number;
  upload: { url: string; method: string; headers: Record<string, string>; expires_at: string };
};

type TCompletion = {
  file: TUploadFile;
  version: { version_no: number; size_bytes: number; status: string };
  activation_required: boolean;
  storage_usage: { project_used_bytes: number; limit_bytes: number };
};

type TVersion = {
  id: string;
  version_no: number;
  status: string;
  is_active: boolean;
  size_bytes: number;
};

type TFileDetail = { file: TFileRow; version: TVersion | null; versions: TVersion[] };

/** One response the API answered for an upload path, with its body read as it arrived. */
type TCaptured = { status: number; body: Promise<unknown> };

/**
 * Capture every response the API answers for one upload path. The body is read as the
 * response arrives — a later navigation discards it — and the caller decides what the
 * entries have to be, so a missing one is reported as a missing attempt rather than as a
 * timeout somewhere else.
 */
function captureUploads(page: Page, pathSuffix: string): TCaptured[] {
  const captured: TCaptured[] = [];
  page.on("response", (response) => {
    if (!new URL(response.url()).pathname.endsWith(pathSuffix)) return;
    captured.push({ status: response.status(), body: response.json().catch(() => null) });
  });
  return captured;
}

/** Every request that would create or store a file: the presign, and the PUT that follows. */
function captureUploadRequests(page: Page): string[] {
  const requests: string[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname.endsWith("/initiate-upload/") || isObjectStoreUrl(url)) {
      requests.push(`${request.method()} ${request.url()}`);
    }
  });
  return requests;
}

function bodiesOf(captured: TCaptured[]): Promise<unknown[]> {
  return Promise.all(captured.map((entry) => entry.body));
}

/**
 * The one body a path answered with. A second attempt or a missing one is reported here,
 * where the expected count is known, rather than as a cast that silently reads null.
 */
async function singleBody<T>(captured: TCaptured[], label: string): Promise<T> {
  expect(captured.length, label).toBe(1);
  const [entry] = captured;
  return (entry ? await entry.body : null) as T;
}

/** The captured body for a file, or a failure naming the file that was never signed. */
function bodyFor<T>(bodies: unknown[], match: (body: T) => boolean, label: string): T {
  const found = (bodies as T[]).find(match);
  expect(found, label).toBeDefined();
  return found as T;
}

/** Wait until the API's own listing carries a name: the upload is then really stored. */
async function waitForListedName(page: Page, name: string): Promise<void> {
  await expect
    .poll(
      async () => {
        const response = await page.request.get(listUrl({ ordering: "-created" }));
        if (!response.ok()) return [];
        const body = (await response.json()) as { results: TFileRow[] };
        return body.results.map((row) => row.name_display);
      },
      { message: `${name} must reach the listing` }
    )
    .toContain(name);
}

async function getFileDetail(page: Page, fileId: string): Promise<TFileDetail> {
  const url = `${listUrl()}${fileId}/`;
  const response = await page.request.get(url);
  expect(response.ok(), `GET ${url}`).toBe(true);
  return (await response.json()) as TFileDetail;
}

/** A fresh, live listing: the reference the DOM is compared against. */
async function liveSnapshot(page: Page, label: string): Promise<TSnapshot> {
  return snapshotList(page, label, () => openFilesTab(page), { requireLive: true });
}

/** The row the API's own listing carries for a name, so nothing is asserted against a guess. */
async function listingRowFor(page: Page, name: string): Promise<TFileRow> {
  const url = listUrl({ ordering: "-created" });
  const response = await page.request.get(url);
  expect(response.ok(), `GET ${url}`).toBe(true);
  const body = (await response.json()) as { results: TFileRow[] };
  const row = body.results.find((candidate) => candidate.name_display === name);
  expect(row, `${name} must be in the listing the API returns`).toBeDefined();
  return row as TFileRow;
}

// --- the object store, read directly ----------------------------------------

const EMPTY_SHA256 = createHash("sha256").update("").digest("hex");
const sha256 = (data: Buffer | string): string => createHash("sha256").update(data).digest("hex");
const hmac = (key: Buffer | string, data: string) => createHmac("sha256", key).update(data).digest();

/** RFC 3986 percent-encoding, as the S3 canonical URI requires (`(`/`)` must be encoded). */
function encodeKeyPath(objectKey: string): string {
  return objectKey
    .split("/")
    .map((segment) =>
      encodeURIComponent(segment).replace(
        /[!'()*]/g,
        (character) => `%${character.charCodeAt(0).toString(16).toUpperCase()}`
      )
    )
    .join("/");
}

/**
 * The URL of one object and the headers that authorise a request for it (SigV4, the scheme
 * the API presigns with). Signed here rather than fetched through the app: the claim under
 * test is that the bytes are in the store, and asking the app to hand them back would prove
 * only that the app can find them.
 */
function signedObjectRequest(
  method: "GET" | "HEAD",
  objectKey: string
): { url: string; headers: Record<string, string> } {
  const endpoint = new URL(MINIO_URL);
  const path = `/${MINIO_BUCKET}/${encodeKeyPath(objectKey)}`;
  const amzDate = new Date().toISOString().replace(/[:-]|\.\d{3}/g, "");
  const dateStamp = amzDate.slice(0, 8);
  const scope = `${dateStamp}/${MINIO_REGION}/s3/aws4_request`;
  const canonicalRequest = [method, path, "", `host:${endpoint.host}`, "", "host", EMPTY_SHA256].join("\n");
  const stringToSign = ["AWS4-HMAC-SHA256", amzDate, scope, sha256(canonicalRequest)].join("\n");
  const signingKey = hmac(hmac(hmac(hmac(`AWS4${MINIO_SECRET_KEY}`, dateStamp), MINIO_REGION), "s3"), "aws4_request");
  const signature = createHmac("sha256", signingKey).update(stringToSign).digest("hex");

  return {
    url: `${MINIO_URL}${path}`,
    headers: {
      "x-amz-content-sha256": EMPTY_SHA256,
      "x-amz-date": amzDate,
      authorization: `AWS4-HMAC-SHA256 Credential=${MINIO_ACCESS_KEY}/${scope}, SignedHeaders=host, Signature=${signature}`,
    },
  };
}

/** What the store holds under this key: the status and, when it exists, its length. */
async function objectHead(page: Page, objectKey: string): Promise<{ status: number; length: number | null }> {
  const { url, headers } = signedObjectRequest("HEAD", objectKey);
  const response = await page.request.fetch(url, { method: "HEAD", headers, failOnStatusCode: false });
  const length = response.headers()["content-length"];
  return { status: response.status(), length: length === undefined ? null : Number(length) };
}

async function objectBytes(page: Page, objectKey: string): Promise<{ status: number; body: Buffer }> {
  const { url, headers } = signedObjectRequest("GET", objectKey);
  const response = await page.request.fetch(url, { headers, failOnStatusCode: false });
  return { status: response.status(), body: response.ok() ? await response.body() : Buffer.alloc(0) };
}

/** True for a request the browser sends straight to the store (the presigned PUT). */
function isObjectStoreUrl(url: URL): boolean {
  return url.origin === new URL(MINIO_URL).origin && url.pathname.startsWith(`/${MINIO_BUCKET}/`);
}

// --- the files this spec uploads --------------------------------------------

/**
 * One file the spec uploads: the same bytes go to the picker (or the drag), and the same
 * bytes are looked for in the store afterwards. `text/plain` is deliberately the simplest
 * type the server verifies — it has no signature to satisfy, only the UTF-8 rule — so a
 * failure here is about the upload path and not about the file's contents.
 *
 * The shape is also Playwright's `FilePayload`, which is why a fixture can be handed to
 * `setInputFiles` as it stands.
 */
type TFixture = { name: string; mimeType: string; text: string; buffer: Buffer; sizeBytes: number };

function textFixture(name: string, sizeBytes: number, marker: string): TFixture {
  const unit = `${marker}:`;
  const text = unit.repeat(Math.ceil(sizeBytes / unit.length)).slice(0, sizeBytes);
  const buffer = Buffer.from(text, "ascii");
  expect(buffer.length, `${name} must carry exactly ${sizeBytes} bytes`).toBe(sizeBytes);
  return { name, mimeType: "text/plain", text, buffer, sizeBytes: buffer.length };
}

// --- observing the queue ----------------------------------------------------

/** One row as the queue rendered it at one moment; `status` `gone` is a row leaving. */
type TQueueRecord = { id: string; name: string; status: string; progress: string; ariaNow: string };

/** A queue row as the DOM currently shows it. */
type TRenderedUploadRow = {
  id: string;
  name: string;
  status: string;
  message: string;
  hasProgress: boolean;
  hasRetry: boolean;
  hasDismiss: boolean;
};

/**
 * Record every state the queue renders, from real DOM mutations.
 *
 * The per-file progress this ticket claims is visible only while an attempt is in flight,
 * and a loopback upload can be over before a poll looks. A `MutationObserver` therefore
 * samples the rows on every change the browser makes, so "queued -> uploading (own
 * progress bar) -> 100% -> gone" is observed rather than raced; the assertions read what
 * the UI actually painted.
 */
async function recordUploadQueue(page: Page): Promise<void> {
  await page.evaluate(() => {
    const records: TQueueRecord[] = [];
    const stateByRow = new Map<string, string>();
    const nameByRow = new Map<string, string>();
    const departed = new Set<string>();

    const sample = (): void => {
      const rows = Array.from(document.querySelectorAll<HTMLElement>('[data-testid^="files-upload-row-"]'));
      const present = new Set<string>();

      for (const row of rows) {
        const id = (row.getAttribute("data-testid") ?? "").replace("files-upload-row-", "");
        const name = row.getAttribute("data-upload-name") ?? "";
        present.add(id);
        departed.delete(id);
        nameByRow.set(id, name);

        const bar = document.querySelector<HTMLElement>(`[data-testid="files-upload-progress-${id}"]`);
        const record: TQueueRecord = {
          id,
          name,
          status: row.getAttribute("data-upload-status") ?? "",
          progress: row.getAttribute("data-upload-progress") ?? "",
          ariaNow: bar?.getAttribute("aria-valuenow") ?? "",
        };
        const key = `${record.status}|${record.progress}|${record.ariaNow}`;
        if (stateByRow.get(id) === key) continue;
        stateByRow.set(id, key);
        records.push(record);
      }

      for (const id of stateByRow.keys()) {
        if (present.has(id) || departed.has(id)) continue;
        departed.add(id);
        records.push({ id, name: nameByRow.get(id) ?? "", status: "gone", progress: "", ariaNow: "" });
      }
    };

    const observer = new MutationObserver(sample);
    observer.observe(document.body, {
      subtree: true,
      childList: true,
      attributes: true,
      attributeFilter: ["data-upload-status", "data-upload-progress", "aria-valuenow"],
    });
    sample();

    (window as unknown as { __filesUploadRecords?: TQueueRecord[] }).__filesUploadRecords = records;
  });
}

async function uploadRecords(page: Page): Promise<TQueueRecord[]> {
  return page.evaluate(
    () => (window as unknown as { __filesUploadRecords?: TQueueRecord[] }).__filesUploadRecords ?? []
  );
}

async function renderedUploadRows(page: Page): Promise<TRenderedUploadRow[]> {
  return page.$$eval(UPLOAD_ROW_SELECTOR, (nodes) =>
    nodes.map((node) => {
      const id = (node.getAttribute("data-testid") ?? "").replace("files-upload-row-", "");
      return {
        id,
        name: node.getAttribute("data-upload-name") ?? "",
        status: node.getAttribute("data-upload-status") ?? "",
        message: (node.querySelector('[data-testid^="files-upload-message-"]')?.textContent ?? "").trim(),
        hasProgress: node.querySelector(`[data-testid="files-upload-progress-${id}"]`) !== null,
        hasRetry: node.querySelector(`[data-testid="files-upload-retry-${id}"]`) !== null,
        hasDismiss: node.querySelector(`[data-testid="files-upload-dismiss-${id}"]`) !== null,
      };
    })
  );
}

/** The queue is empty once every attempt has settled and left it. */
async function waitForQueueEmpty(page: Page, label: string): Promise<void> {
  await expect(page.getByTestId("files-upload-rows"), `${label}: the queue must be empty`).toHaveCount(0);
}

/** The one row the queue is showing for a file, or a failure naming what is on screen. */
async function uploadRowFor(page: Page, name: string): Promise<TRenderedUploadRow> {
  let rows: TRenderedUploadRow[] = [];
  await expect
    .poll(
      async () => {
        rows = await renderedUploadRows(page);
        return rows.some((candidate) => candidate.name === name);
      },
      { message: `the queue must show a row for ${name}` }
    )
    .toBe(true);

  return bodyFor<TRenderedUploadRow>(
    rows,
    (row) => row.name === name,
    `the queue's row for ${name} (it shows ${JSON.stringify(rows)})`
  );
}

/**
 * The lifecycle the queue painted for one file: it was uploading, it drew its own progress
 * bar, the bar reached 100%, and the attempt left the queue when it was stored. Nothing
 * here is derived from the file's own bytes — it is what the DOM showed, in order.
 */
function expectProgressThenCompletion(records: TQueueRecord[], name: string): void {
  const rows = records.filter((record) => record.name === name);
  expect(rows.length, `${name}: the queue rendered this file`).toBeGreaterThan(0);

  const statuses = rows.map((record) => record.status);
  expect(statuses, `${name}: the attempt was uploading`).toContain("uploading");
  expect(
    rows.some((record) => record.ariaNow !== ""),
    `${name}: the uploading row drew its own progress bar (aria-valuenow)`
  ).toBe(true);

  const steps = rows
    .filter((record) => record.status === "uploading" || record.status === "finalizing")
    .map((record) => Number(record.progress));
  expect(steps.every(Number.isFinite), `${name}: every progress value is a number (${steps.join(", ")})`).toBe(true);
  expect(steps, `${name}: progress never goes backwards (${steps.join(", ")})`).toEqual(
    steps.toSorted((a, b) => a - b)
  );
  expect(Math.max(...steps), `${name}: the progress reached 100 before the server was asked to finalize`).toBe(100);
  expect(statuses.at(-1), `${name}: a stored upload leaves the queue`).toBe("gone");
}

/** A real `DataTransfer` carrying a real `File`, built inside the page. */
async function fileDataTransfer(page: Page, fixture: TFixture) {
  return page.evaluateHandle(
    ({ name, mimeType, text }) => {
      const transfer = new DataTransfer();
      transfer.items.add(new File([text], name, { type: mimeType }));
      return transfer;
    },
    { name: fixture.name, mimeType: fixture.mimeType, text: fixture.text }
  );
}

// --- the specs --------------------------------------------------------------

test.describe("Project files upload (T-113)", () => {
  test("a_multi_file_picker_and_a_drop_store_into_the_listing_and_the_store", async ({ page }) => {
    const stamp = Date.now();
    const picked = [
      textFixture(`upload-spec-a-${stamp}.txt`, 8 * 1024 * 1024, "picked-a"),
      textFixture(`upload-spec-b-${stamp}.txt`, 20 * 1024 * 1024, "picked-b"),
    ];
    const dropped = textFixture(`upload-spec-c-${stamp}.txt`, 4_096, "dropped-c");
    const fixtures = [...picked, dropped];
    const totalBytes = fixtures.reduce((sum, fixture) => sum + fixture.sizeBytes, 0);

    await signIn(page, OWNER_EMAIL, OWNER_PASSWORD);
    const before = await liveSnapshot(page, "upload-before");
    await expectViewMatchesBody(page, before.live, "upload-before");

    const initiations = captureUploads(page, "/initiate-upload/");
    const completions = captureUploads(page, "/complete-upload/");
    await recordUploadQueue(page);

    // One picker selection carrying more than one file: each gets its own row, its own
    // presign, its own PUT and its own progress.
    await page.getByTestId("files-upload-input").setInputFiles(picked);

    // And one file dropped, as a real drag: the browser builds the `DataTransfer`, the
    // view's own handlers receive it, and the overlay it shows names the destination.
    await expect(page.getByTestId("files-upload-drop-overlay")).toBeHidden();
    const transfer = await fileDataTransfer(page, dropped);
    await page.dispatchEvent(FILES_ROOT, "dragenter", { dataTransfer: transfer });
    await expect(page.getByTestId("files-upload-drop-overlay"), "a drag shows where the files land").toBeVisible();
    await expect(page.getByTestId("files-upload-drop-overlay")).toContainText("Drop to upload to Project root");
    await page.dispatchEvent(FILES_ROOT, "dragover", { dataTransfer: transfer });
    await page.dispatchEvent(FILES_ROOT, "drop", { dataTransfer: transfer });
    await expect(page.getByTestId("files-upload-drop-overlay")).toBeHidden();

    await expect
      .poll(() => completions.length, { message: "every attempt must be finalized by the API" })
      .toBe(fixtures.length);
    await waitForQueueEmpty(page, "upload");

    // --- what the queue painted ---------------------------------------------
    const records = await uploadRecords(page);
    for (const fixture of fixtures) expectProgressThenCompletion(records, fixture.name);

    // --- what the API answered ----------------------------------------------
    expect(initiations.length, "one presign per file").toBe(fixtures.length);
    expect(completions.length, "one finalize per file").toBe(fixtures.length);
    expect(
      initiations.every((entry) => entry.status === 200),
      "every initiate answered 200"
    ).toBe(true);
    expect(
      completions.every((entry) => entry.status === 200),
      "every finalize answered 200"
    ).toBe(true);

    const initiationBodies = await bodiesOf(initiations);
    const completionBodies = await bodiesOf(completions);

    // --- the listing the view shows, against the listing the API answers -----
    const after = await liveSnapshot(page, "upload-after");
    await expectViewMatchesBody(page, after.live, "upload-after");

    for (const fixture of fixtures) {
      const signed = bodyFor<TInitiation>(
        initiationBodies,
        (body) => body.file.name_display === fixture.name,
        `the API signed an upload for ${fixture.name}`
      );
      expect(signed.version_no, `${fixture.name}: a first upload of a new file is version 1`).toBe(1);

      const stored = bodyFor<TCompletion>(
        completionBodies,
        (body) => body.file.id === signed.file.id,
        `the API finalized ${fixture.name}`
      );
      expect(stored.version.size_bytes, `${fixture.name}: the store's own byte count`).toBe(fixture.sizeBytes);
      expect(stored.version.status, `${fixture.name}: a first verified version is active`).toBe("active");
      expect(stored.activation_required, `${fixture.name}: nothing is waiting for confirmation`).toBe(false);

      const listed = bodyFor<TFileRow>(
        after.live.results,
        (row) => row.name_display === fixture.name,
        `${fixture.name} is in the listing the API answered`
      );
      expect(listed.id, `${fixture.name}: the listed row is the file initiate-upload created`).toBe(signed.file.id);
      expect(listed.size_bytes, `${fixture.name}: the listing's own size_bytes`).toBe(fixture.sizeBytes);
      expect(listed.folder_id, `${fixture.name}: it landed in the folder the view was browsing`).toBeNull();

      const head = await objectHead(page, signed.file.object_key);
      expect(head.status, `${fixture.name}: the object exists in the store`).toBe(200);
      expect(head.length, `${fixture.name}: the store holds exactly the bytes that were sent`).toBe(fixture.sizeBytes);
    }

    // The bytes, not just the length: two of the three are read back and hashed.
    for (const fixture of [picked[0], dropped]) {
      const signed = bodyFor<TInitiation>(
        initiationBodies,
        (body) => body.file.name_display === fixture.name,
        `the API signed an upload for ${fixture.name}`
      );
      const object = await objectBytes(page, signed.file.object_key);
      expect(object.status, `${fixture.name}: the object can be read back`).toBe(200);
      expect(sha256(object.body), `${fixture.name}: the stored bytes are the bytes this test sent`).toBe(
        sha256(fixture.buffer)
      );
    }

    // The storage figures are the listing's own, and they rose by exactly the bytes stored.
    expect(
      after.live.storage.project_used_bytes - before.live.storage.project_used_bytes,
      "the project's usage rose by exactly the bytes that were uploaded"
    ).toBe(totalBytes);

    // The quota notices are driven by the same block the chip shows, so what is on screen
    // follows from the response rather than from a fixture.
    const { storage } = after.live;
    const usedPct = storage.limit_bytes > 0 ? Math.round((storage.project_used_bytes / storage.limit_bytes) * 100) : 0;
    await expect(page.getByTestId("files-upload-quota-exceeded")).toHaveCount(
      storage.limit_bytes > 0 && storage.project_used_bytes >= storage.limit_bytes ? 1 : 0
    );
    await expect(page.getByTestId("files-upload-quota-warning")).toHaveCount(
      storage.warn_threshold_pct > 0 && usedPct >= storage.warn_threshold_pct ? 1 : 0
    );
  });

  test("pre_validation_refuses_an_over_size_and_a_disallowed_type_without_a_request", async ({ page }) => {
    const stamp = Date.now();
    // The over-size file is also a type the server refuses, so the order of the two rules is
    // observable: the size check runs first (R-UPL-7).
    const tooLarge = {
      name: `upload-spec-too-large-${stamp}.exe`,
      mimeType: "application/x-msdownload",
      buffer: Buffer.alloc(26_214_400 + 1, 0x41),
    };
    const disallowed = {
      name: `upload-spec-blocked-${stamp}.exe`,
      mimeType: "application/x-msdownload",
      buffer: Buffer.from("MZ this type is not on the allowlist\n", "ascii"),
    };

    await signIn(page, OWNER_EMAIL, OWNER_PASSWORD);
    const before = await liveSnapshot(page, "precheck-before");
    await expectViewMatchesBody(page, before.live, "precheck-before");

    const requests = captureUploadRequests(page);
    await page.getByTestId("files-upload-input").setInputFiles([tooLarge, disallowed]);

    // The refusal is the row itself: both files are in the queue and neither is running.
    await expect(page.locator(UPLOAD_ROW_SELECTOR)).toHaveCount(2);
    const rows = await renderedUploadRows(page);
    expect(
      rows.map((row) => row.status),
      "both files are refused before anything is signed"
    ).toEqual(["rejected", "rejected"]);
    expect(
      rows.every((row) => !row.hasProgress),
      "a refused file never shows a progress bar"
    ).toBe(true);
    expect(
      rows.every((row) => !row.hasRetry),
      "a refused file is not retried as it stands"
    ).toBe(true);
    expect(
      rows.every((row) => row.hasDismiss),
      "a refused row can be dismissed"
    ).toBe(true);

    const tooLargeRow = bodyFor<TRenderedUploadRow>(rows, (row) => row.name === tooLarge.name, "the over-size row");
    expect(tooLargeRow.message, "the over-size file is refused for its size, not its type").toContain("Too large");
    expect(tooLargeRow.message, "the message names the limit the server enforces").toContain("25 MB");
    const disallowedRow = bodyFor<TRenderedUploadRow>(
      rows,
      (row) => row.name === disallowed.name,
      "the disallowed-type row"
    );
    expect(disallowedRow.message, "the disallowed type is named in the refusal").toBe(
      "File type not allowed: application/x-msdownload."
    );

    // Nothing went out: no presign, no PUT to the store.
    expect(requests, "a pre-validated refusal issues no request at all").toEqual([]);

    for (const row of rows) await page.getByTestId(`files-upload-dismiss-${row.id}`).click();
    await waitForQueueEmpty(page, "precheck");

    // And the listing is untouched, file for file and byte for byte.
    const after = await liveSnapshot(page, "precheck-after");
    await expectViewMatchesBody(page, after.live, "precheck-after");
    expect(
      after.live.results.map((row) => row.id),
      "a refused file leaves the listing as it was"
    ).toEqual(before.live.results.map((row) => row.id));
    expect(after.live.storage, "and leaves the usage untouched").toEqual(before.live.storage);
    expect(requests, "and still nothing was signed").toEqual([]);
  });

  test("a_name_collision_is_resolved_by_keep_both_replace_and_cancel", async ({ page }) => {
    const stamp = Date.now();
    const base = textFixture(`upload-spec-collide-${stamp}.txt`, 512, "collision-1");
    const stem = base.name.replace(/\.txt$/, "");

    await signIn(page, OWNER_EMAIL, OWNER_PASSWORD);
    await openFilesTab(page);

    // The colliding name is this test's own: the file it uploads first through the surface.
    await page.getByTestId("files-upload-input").setInputFiles([base]);
    // The queue starts empty here, so "the queue is empty" is not a sync point: the
    // listing is the API's own word that the file was really stored.
    await waitForListedName(page, base.name);
    const existing = await listingRowFor(page, base.name);
    expect(existing.size_bytes, "the base file is listed with its own size").toBe(base.sizeBytes);
    await expect
      .poll(async () => (await readRows(page)).some((row) => row.fileId === existing.id), {
        message: "the view must list the file the collision is judged against",
      })
      .toBe(true);

    const initiations = captureUploads(page, "/initiate-upload/");
    const requests = captureUploadRequests(page);
    const modal = page.getByTestId("files-upload-collision");

    // --- keep both: the server suffixes the name, the existing file is untouched -----
    const variant = textFixture(base.name, 700, "collision-2");
    await page.getByTestId("files-upload-input").setInputFiles([variant]);
    await expect(modal, "a name already in the folder opens the collision decision").toBeVisible();
    await expect(modal).toContainText(`“${base.name}” already exists in Project root`);
    const keepBothLabel = (await page.getByTestId("files-upload-collision-keep-both").textContent()) ?? "";
    const predictedName = /Keep both \((.+?)\)Adds/.exec(keepBothLabel)?.[1] ?? "";
    expect(predictedName, "the keep-both choice names the file the server derives from the rule (R-FOLD-5)").toBe(
      `${stem} (2).txt`
    );
    // All three ways out are offered (DESIGN §8).
    await expect(page.getByTestId("files-upload-collision-replace")).toContainText("Replace as new version");
    await expect(page.getByTestId("files-upload-collision-cancel")).toContainText("Cancel");

    await page.getByTestId("files-upload-collision-keep-both").click();
    await expect(modal).toBeHidden();
    await waitForQueueEmpty(page, "collision-keep-both");

    const afterKeep = await liveSnapshot(page, "collision-keep-both");
    await expectViewMatchesBody(page, afterKeep.live, "collision-keep-both");
    const kept = afterKeep.live.results.filter((row) => row.id !== existing.id && row.name_display.startsWith(stem));
    expect(kept.length, "keep both adds a second file beside the existing one").toBe(1);
    expect(kept[0]?.name_display, "the name the server derived is the one the modal predicted").toBe(predictedName);
    expect(kept[0]?.size_bytes, "the added file carries its own bytes").toBe(variant.sizeBytes);
    const untouched = afterKeep.live.results.find((row) => row.id === existing.id);
    expect(untouched?.name_display, "the existing file keeps its name").toBe(base.name);
    expect(untouched?.size_bytes, "and its bytes").toBe(base.sizeBytes);
    expect((await getFileDetail(page, existing.id)).versions.length, "keep both adds no version to it").toBe(1);

    // --- replace as a new version: a version is added, nothing becomes active --------
    const revision = textFixture(base.name, 900, "collision-3");
    await page.getByTestId("files-upload-input").setInputFiles([revision]);
    await expect(modal).toBeVisible();
    await expect(modal).toContainText("Adds a new version to");
    await page.getByTestId("files-upload-collision-replace").click();
    await expect(modal).toBeHidden();

    // The revision is stored but never silently activated (AD-18), and the row says so.
    await expect
      .poll(async () => (await renderedUploadRows(page)).map((row) => row.status), {
        message: "the revision settles into a row that reports what was stored",
      })
      .toEqual(["saved"]);
    const savedRow = await uploadRowFor(page, base.name);
    expect(savedRow.message, "and says it is not the active one").toContain("not the active version");

    const revisionInitiation = bodyFor<TInitiation>(
      await bodiesOf(initiations),
      (body) => body.version_no === 2,
      "the replacement presigned version 2 of the existing file"
    );
    expect(revisionInitiation.file.id, "the replacement targets the existing file, not a new one").toBe(existing.id);
    expect(savedRow.message, "the row reports the version the API numbered").toContain(
      `Version ${revisionInitiation.version_no} saved`
    );
    const revisionObject = await objectHead(page, revisionInitiation.file.object_key);
    expect(revisionObject.status, "the revision's bytes reached the store").toBe(200);
    expect(revisionObject.length, "and hold exactly the revision's bytes").toBe(revision.sizeBytes);

    await page.getByTestId(`files-upload-dismiss-${savedRow.id}`).click();
    await waitForQueueEmpty(page, "collision-replace");

    const detail = await getFileDetail(page, existing.id);
    expect(
      detail.versions.map((version) => version.version_no).toSorted((a, b) => a - b),
      "the file now has both versions"
    ).toEqual([1, 2]);
    expect(
      detail.versions.filter((version) => version.is_active).map((version) => version.version_no),
      "the old version stays active"
    ).toEqual([1]);
    expect(
      detail.versions.filter((version) => version.version_no === 2).map((version) => version.status),
      "the new one is stored as superseded"
    ).toEqual(["superseded"]);

    const afterReplace = await liveSnapshot(page, "collision-replace");
    await expectViewMatchesBody(page, afterReplace.live, "collision-replace");
    expect(
      afterReplace.live.results.find((row) => row.id === existing.id)?.size_bytes,
      "the listing still reports the active version's bytes"
    ).toBe(base.sizeBytes);
    expect(
      afterReplace.live.results.filter((row) => row.name_display.startsWith(stem)).length,
      "a revision does not add a row"
    ).toBe(2);

    // --- cancel: nothing is signed, nothing is created ------------------------------
    const cancelled = textFixture(base.name, 1_200, "collision-4");
    const initiationsBefore = initiations.length;
    const requestsBefore = requests.length;
    await page.getByTestId("files-upload-input").setInputFiles([cancelled]);
    await expect(modal).toBeVisible();
    await page.getByTestId("files-upload-collision-cancel").click();

    await expect(modal).toHaveCount(0);
    await expect(page.locator(UPLOAD_ROW_SELECTOR), "cancel takes the row with it").toHaveCount(0);
    expect(initiations.length, "cancel resolves the collision before anything is signed").toBe(initiationsBefore);
    expect(requests.length, "and sends no request at all").toBe(requestsBefore);

    const afterCancel = await liveSnapshot(page, "collision-cancel");
    await expectViewMatchesBody(page, afterCancel.live, "collision-cancel");
    expect(
      afterCancel.live.results.map((row) => row.id),
      "cancel leaves the listing exactly as the replacement left it"
    ).toEqual(afterReplace.live.results.map((row) => row.id));
    expect((await getFileDetail(page, existing.id)).versions.length, "and adds no version").toBe(2);
  });

  test("cancelling_a_put_in_flight_ends_the_attempt_and_leaves_no_object", async ({ page }) => {
    const stamp = Date.now();
    const fixture = textFixture(`upload-spec-cancel-${stamp}.txt`, 2 * 1024 * 1024, "cancel");

    await signIn(page, OWNER_EMAIL, OWNER_PASSWORD);
    const before = await liveSnapshot(page, "cancel-before");
    await expectViewMatchesBody(page, before.live, "cancel-before");

    // The PUT is held at the store, so the attempt is genuinely in flight when Cancel is
    // pressed; after the abort the held request is simply dropped. The timer is unref'd so
    // an abandoned hold cannot keep the worker's event loop alive after the test.
    let held = 0;
    const holdPut = async (route: Route): Promise<void> => {
      held += 1;
      await delay(15_000, null, { ref: false });
      await route.continue().catch(() => undefined);
    };
    // The same matcher value for `route` and `unroute`: a function matcher is matched by
    // reference, so a fresh arrow would leave the route in place.
    await page.route(isObjectStoreUrl, holdPut);

    const initiations = captureUploads(page, "/initiate-upload/");
    const aborts = captureUploads(page, "/abort-upload/");
    const completions = captureUploads(page, "/complete-upload/");

    await page.getByTestId("files-upload-input").setInputFiles([fixture]);

    const row = await uploadRowFor(page, fixture.name);
    const rowSelector = `[data-testid="files-upload-row-${row.id}"]`;
    await expect(page.locator(rowSelector), "the row is in flight").toHaveAttribute("data-upload-status", "uploading");
    await expect(page.getByTestId(`files-upload-progress-${row.id}`), "the row shows its own progress").toBeVisible();
    await expect(page.getByTestId(`files-upload-progress-${row.id}`)).toHaveAttribute("aria-valuenow", /^\d+$/);
    await expect(page.getByTestId(`files-upload-message-${row.id}`)).toContainText("Uploading to Project root");
    // The row says "uploading" from the moment the attempt starts, which is before the
    // presign has answered: the PUT has to be waited for rather than assumed.
    await expect.poll(() => held, { message: "the attempt must reach the store" }).toBe(1);

    await page.getByTestId(`files-upload-cancel-${row.id}`).click();

    // The row reports the cancellation and offers a way back; the attempt is given up with
    // the API's own endpoint, which is what releases its reservation exactly once.
    await expect(page.locator(rowSelector)).toHaveAttribute("data-upload-status", "cancelled");
    await expect(page.getByTestId(`files-upload-message-${row.id}`)).toHaveText("Cancelled.");
    await expect(page.getByTestId(`files-upload-retry-${row.id}`)).toBeVisible();
    await expect.poll(() => aborts.length, { message: "cancelling must end the attempt" }).toBe(1);
    expect(aborts[0]?.status, "the abort is accepted and releases the reservation once").toBe(204);
    expect(completions.length, "a cancelled attempt is never finalized").toBe(0);

    const initiation = await singleBody<TInitiation>(initiations, "the cancelled attempt was signed exactly once");
    const orphan = await objectHead(page, initiation.file.object_key);
    expect(orphan.status, "the interrupted PUT left no object behind").toBe(404);

    // The bytes were never stored, so the usage is where it was — and the listing is
    // whatever the API answers, which is what the DOM is compared against. The live fetch
    // is triggered by a quick view rather than by a navigation: navigating would rebuild the
    // view and throw away the cancelled row this test is about to retry. A file row whose
    // only version failed is still listed by the API until T-118's sweep removes it
    // (DEFECT-001, another ticket's); the row it answers with is recorded here, not
    // asserted away.
    const afterCancel = await snapshotList(page, "cancel-after", () => page.getByTestId("files-quick-recent").click());
    await expectViewMatchesBody(page, afterCancel.live, "cancel-after");
    expect(afterCancel.live.storage.project_used_bytes, "a cancelled attempt stores no bytes and holds none").toBe(
      before.live.storage.project_used_bytes
    );
    console.log(
      `[upload-spec] listing rows for the cancelled name: ${JSON.stringify(
        afterCancel.live.results
          .filter((entry) => entry.name_display === fixture.name)
          .map((entry) => ({ name: entry.name_display, size_bytes: entry.size_bytes }))
      )}`
    );

    // Retrying the cancelled attempt is what proves the reservation was released: the
    // presign is refused while a live reservation exists for the same file (ARCH-001 §2.4),
    // and the retry asks for that same file.
    await page.unroute(isObjectStoreUrl);
    await page.getByTestId(`files-upload-retry-${row.id}`).click();
    await waitForQueueEmpty(page, "cancel-retry");

    const afterRetry = await liveSnapshot(page, "cancel-retry");
    await expectViewMatchesBody(page, afterRetry.live, "cancel-retry");
    const listed = afterRetry.live.results.filter((entry) => entry.name_display === fixture.name);
    expect(listed.length, "the retry stores the file the cancelled attempt had claimed, once").toBe(1);
    expect(listed[0]?.size_bytes, "with the bytes this test sent").toBe(fixture.sizeBytes);
    expect(listed[0]?.id, "under the same file id, not a second file").toBe(initiation.file.id);
    expect(
      afterRetry.live.storage.project_used_bytes - before.live.storage.project_used_bytes,
      "and the usage moved by exactly those bytes"
    ).toBe(fixture.sizeBytes);
  });

  test("a_failed_put_is_retried_into_the_same_file_with_no_object_left_behind", async ({ page }) => {
    const stamp = Date.now();
    const fixture = textFixture(`upload-spec-retry-${stamp}.txt`, 3 * 1024 * 1024, "retry");

    await signIn(page, OWNER_EMAIL, OWNER_PASSWORD);
    const before = await liveSnapshot(page, "retry-before");
    await expectViewMatchesBody(page, before.live, "retry-before");

    // The first attempt's PUT never reaches the store; the retry's goes through.
    let attempts = 0;
    await page.route(isObjectStoreUrl, async (route) => {
      attempts += 1;
      if (attempts === 1) await route.abort("failed");
      else await route.continue();
    });

    const initiations = captureUploads(page, "/initiate-upload/");
    const completions = captureUploads(page, "/complete-upload/");
    const aborts = captureUploads(page, "/abort-upload/");

    await page.getByTestId("files-upload-input").setInputFiles([fixture]);

    const failedRow = await uploadRowFor(page, fixture.name);
    const rowSelector = `[data-testid="files-upload-row-${failedRow.id}"]`;
    await expect(page.locator(rowSelector)).toHaveAttribute("data-upload-status", "failed");
    await expect(
      page.getByTestId(`files-upload-message-${failedRow.id}`),
      "the failure names what happened"
    ).toContainText("Upload failed — The upload could not reach the storage service.");
    await expect(page.getByTestId(`files-upload-retry-${failedRow.id}`), "the row offers the retry").toBeVisible();

    const firstAttempt = await singleBody<TInitiation>(initiations, "the failed attempt was signed exactly once");
    const orphan = await objectHead(page, firstAttempt.file.object_key);
    expect(orphan.status, "the failed PUT stored nothing").toBe(404);

    // A quick view refetches the listing without rebuilding the view, so the failed row is
    // still there to retry after this snapshot.
    const afterFailure = await snapshotList(page, "retry-failed", () => page.getByTestId("files-quick-recent").click());
    await expectViewMatchesBody(page, afterFailure.live, "retry-failed");
    expect(afterFailure.live.storage.project_used_bytes, "a failed attempt counts as unused storage").toBe(
      before.live.storage.project_used_bytes
    );

    await page.getByTestId(`files-upload-retry-${failedRow.id}`).click();
    await waitForQueueEmpty(page, "retry");

    // The retry ends as the same file — one row, this file's id — with the bytes stored,
    // which is what "a retry resumes the upload" has to mean (EXPERIENCE: no duplicate file
    // row). The API's own version history is the evidence for how it got there.
    const afterRetry = await liveSnapshot(page, "retry-after");
    await expectViewMatchesBody(page, afterRetry.live, "retry-after");
    const listed = afterRetry.live.results.filter((entry) => entry.name_display === fixture.name);
    expect(listed.length, "the retry leaves exactly one file for this name").toBe(1);
    expect(listed[0]?.id, "and it is the file the first attempt created").toBe(firstAttempt.file.id);
    expect(listed[0]?.size_bytes, "listed with the bytes that were stored").toBe(fixture.sizeBytes);

    const detail = await getFileDetail(page, firstAttempt.file.id);
    expect(
      detail.versions.map((version) => version.version_no).toSorted((a, b) => a - b),
      "the retry is the second version of that file"
    ).toEqual([1, 2]);
    expect(
      detail.versions.filter((version) => version.is_active).map((version) => version.version_no),
      "with the retried version active"
    ).toEqual([2]);
    expect(
      detail.versions.filter((version) => version.status === "failed").map((version) => version.version_no),
      "and the abandoned attempt recorded as failed"
    ).toEqual([1]);
    expect(detail.version?.version_no, "the file's active version is the retried one").toBe(2);

    expect(completions.length, "one finalize, for the retry that stored the bytes").toBe(1);
    expect(aborts.length, "the abandoned attempt was given up once").toBe(1);
    expect(aborts[0]?.status).toBe(204);
    expect(attempts, "two PUTs: the failed one and the retry").toBe(2);

    const stored = await singleBody<TCompletion>(completions, "the retry was the only attempt finalized");
    const storedObject = await objectHead(page, stored.file.object_key);
    expect(storedObject.status, "the retried bytes are in the store").toBe(200);
    expect(storedObject.length).toBe(fixture.sizeBytes);
    expect(
      afterRetry.live.storage.project_used_bytes - before.live.storage.project_used_bytes,
      "the usage rose by exactly those bytes"
    ).toBe(fixture.sizeBytes);
  });

  test("a_guest_is_offered_no_upload_affordance_and_a_drop_stores_nothing", async ({ browser }) => {
    const stamp = Date.now();
    const fixture = textFixture(`guest-drop-${stamp}.txt`, 1_024, "guest");
    const ownerContext = await browser.newContext({ baseURL: WEB_URL, ignoreHTTPSErrors: true });
    const guestContext = await browser.newContext({
      baseURL: WEB_URL,
      ignoreHTTPSErrors: true,
      viewport: { width: 1440, height: 900 },
    });

    try {
      // An empty folder for the guest to look at, created through the API as a member: the
      // empty state is where a member is offered an upload action, so a guest must not be
      // offered one there either.
      const ownerPage = await ownerContext.newPage();
      await signIn(ownerPage, OWNER_EMAIL, OWNER_PASSWORD);
      await openFilesTab(ownerPage);
      const emptyFolderId = await createFolder(ownerPage, `Guest Empty ${stamp}`);

      const page = await guestContext.newPage();
      await signIn(page, GUEST_EMAIL, GUEST_PASSWORD);

      const root = await liveSnapshot(page, "guest-upload-root");
      await expectViewMatchesBody(page, root.live, "guest-upload-root");

      // No part of the upload surface exists in the guest's view — not a disabled control,
      // no control at all (EXP-001 F-01).
      for (const testId of [
        "files-upload-action",
        "files-upload-input",
        "files-upload-dropzone",
        "files-upload-rows",
        "files-upload-quota-warning",
        "files-upload-quota-exceeded",
        "files-upload-collision",
      ]) {
        await expect(page.getByTestId(testId), `${testId} must not be rendered for a guest`).toHaveCount(0);
      }

      const requests = captureUploadRequests(page);

      // Even a drag a member can perform stores nothing here: the handlers ignore it.
      const transfer = await fileDataTransfer(page, fixture);
      await page.dispatchEvent(FILES_ROOT, "dragenter", { dataTransfer: transfer });
      await expect(page.getByTestId("files-upload-drop-overlay")).toHaveCount(0);
      await page.dispatchEvent(FILES_ROOT, "dragover", { dataTransfer: transfer });
      await page.dispatchEvent(FILES_ROOT, "drop", { dataTransfer: transfer });
      await expect(page.locator(UPLOAD_ROW_SELECTOR)).toHaveCount(0);
      expect(requests, "a guest's drop signs nothing and sends nothing").toEqual([]);

      // The empty folder is the other place a member is offered an upload action.
      await openFilesTab(page, `${APP_FILES_URL}?folder=${emptyFolderId}`);
      await expect(page.getByTestId("files-state-empty"), "the guest's folder view is the empty state").toBeVisible();
      await expect(page.getByTestId("files-state-empty")).toHaveAttribute("data-variant", "folder");
      await expect(page.getByTestId("files-upload-empty-action")).toHaveCount(0);
      expect(requests, "and still nothing was signed").toEqual([]);
    } finally {
      await ownerContext.close();
      await guestContext.close();
    }
  });
});
