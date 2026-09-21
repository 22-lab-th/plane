/**
 * T-115 — the work item's attachment surface: a project file attached to a work item, the
 * section that renders it, and the unlink that leaves the file in the project (AC-16).
 *
 * Evidence rule, the same one `browse.spec.ts` states in full and `./support` implements:
 * every rendered value is compared against the API response this test consumed. The file
 * attached here is created through the **real pipeline** (`initiate-upload` with a link,
 * a PUT to the presigned URL, `complete-upload`), and every assertion reads back either the
 * entity-links response, the file detail, the project listing or the bytes the download URL
 * serves. No fixture holds the values being asserted.
 *
 * The state under test is deliberately the one DEFECT-009 hid: a work item whose **only**
 * attachment is a project file (no legacy file asset at all), so the section, its badge and
 * its row can only come from the linked file.
 */

// Playwright imports
import { expect, test, type APIResponse, type Page } from "@playwright/test";
// harness
import {
  apiWrite,
  getListBody,
  ISSUE_UPLOAD_ID,
  ISSUE_UNLINK_ID,
  listUrl,
  OWNER_EMAIL,
  OWNER_PASSWORD,
  PROJECT_ID,
  signIn,
  WEB_URL,
  WORKSPACE_SLUG,
} from "./support";

/** One row of `GET files/links/`: the link a surface unlinks by, and the file it shows. */
type TEntityLinkRow = {
  link: { id: string; entity_type: string; entity_id: string; entity_identifier: string | null };
  file: { id: string; name_display: string; size_bytes: number; mime_type: string; link_count: number };
};

type TEntityLinksBody = { results: TEntityLinkRow[] };

type TFileDetail = {
  file: { id: string; name_display: string; object_key: string };
  link_count: number;
  permissions: { can_download: boolean };
};

type TAccessUrl = { url: string; disposition: string; file_name: string };

/** The bytes this spec uploads, so a mismatch is a real one rather than a length check. */
const ATTACHMENT_BYTES = Buffer.from("T-115 attachment payload\nsecond line\n", "utf8");

function workItemUrl(issueId: string): string {
  return `/${WORKSPACE_SLUG}/projects/${PROJECT_ID}/issues/${issueId}`;
}

/**
 * Attach a file to a work item through the product's own three steps, from the host.
 *
 * The presigned URL points at the published MinIO port, which the test process can reach
 * (the API container cannot), so the PUT happens here with the URL's own headers - the same
 * path `e2e/files/seed_content.py` takes.
 */
async function attachFileToWorkItem(
  page: Page,
  issueId: string,
  fileName: string,
  bytes: Buffer
): Promise<{ fileId: string }> {
  const initiated = await apiWrite(page, "post", `${listUrl()}initiate-upload/`, {
    file_name: fileName,
    size_bytes: bytes.length,
    mime_type: "text/plain",
    link: { entity_type: "issue", entity_id: issueId },
  });
  const body = (await initiated.json()) as {
    file: { id: string };
    version_no: number;
    upload: { url: string; headers: Record<string, string> };
  };

  const stored = await fetch(body.upload.url, { method: "PUT", headers: body.upload.headers, body: bytes });
  expect(stored.status, "the presigned PUT to the object store").toBe(200);

  await apiWrite(page, "post", `${listUrl()}${body.file.id}/complete-upload/`, {
    version_no: body.version_no,
    size_bytes: bytes.length,
  });

  return { fileId: body.file.id };
}

async function readEntityLinks(page: Page, issueId: string): Promise<TEntityLinksBody> {
  const response = await page.request.get(`${listUrl()}links/?entity_type=issue&entity_id=${issueId}`);
  expect(response.ok(), `GET files/links/ for issue ${issueId} answered ${response.status()}`).toBe(true);
  return (await response.json()) as TEntityLinksBody;
}

async function readFileDetail(page: Page, fileId: string): Promise<TFileDetail> {
  const response = await page.request.get(`${listUrl()}${fileId}/`);
  expect(response.ok(), `GET files/${fileId}/ answered ${response.status()}`).toBe(true);
  return (await response.json()) as TFileDetail;
}

/** The bytes the download endpoint's signed URL actually serves. */
async function fetchSignedBytes(page: Page, url: string): Promise<APIResponse> {
  return page.request.get(url);
}

test.describe("work item attachments are project files", () => {
  test.beforeEach(async ({ page }) => {
    await signIn(page, OWNER_EMAIL, OWNER_PASSWORD);
  });

  test("a work item whose only attachment is a project file shows the section, and unlinking leaves the file intact", async ({
    page,
  }) => {
    const issueId = ISSUE_UNLINK_ID;
    const { fileId } = await attachFileToWorkItem(page, issueId, "design-notes.txt", ATTACHMENT_BYTES);

    // --- the API's own answer, which every DOM value below is compared against ------
    const links = await readEntityLinks(page, issueId);
    expect(links.results.length, "the upload's link is one row for this issue").toBe(1);
    const [row] = links.results;
    const file = row.file;
    expect(file.id).toBe(fileId);
    expect(file.name_display).toBe("design-notes.txt");
    expect(file.size_bytes).toBe(ATTACHMENT_BYTES.length);
    expect(file.link_count).toBe(1);
    expect(row.link.entity_type).toBe("issue");
    expect(row.link.entity_id).toBe(issueId);

    // --- the work item's surface renders it (the DEFECT-009 state) -------------------
    await page.goto(`${WEB_URL}${workItemUrl(issueId)}`);
    const attachmentRow = page.locator(`[data-testid="issue-attachment-${file.id}"]`);
    await expect(attachmentRow, "the section and its row render for a project-file attachment").toBeVisible();
    await expect(attachmentRow).toContainText(file.name_display);

    const section = page.getByRole("button", { name: /Attachments/ });
    await expect(section, "the Attachments widget is on the work item").toBeVisible();
    // The badge itself, not the section's whole text: the count is the number the store
    // computes from the legacy assets plus the linked project files, so reading the
    // element is what pins DEFECT-009's fix.
    await expect(page.getByTestId("issue-attachments-count"), "the badge counts the linked file").toHaveText("1");

    // --- the project's Files view lists the same id, once ---------------------------
    const listing = await getListBody(page, { folder_id: "root" });
    const listed = listing.results.filter((result) => result.id === file.id);
    expect(listed.length, "the file is listed exactly once in the project").toBe(1);
    expect(listed[0].name_display).toBe(file.name_display);
    expect(listed[0].size_bytes).toBe(file.size_bytes);

    // --- unlink from the work item --------------------------------------------------
    await attachmentRow.getByRole("button", { name: `Actions for ${file.name_display}` }).click();
    await page.getByRole("menuitem", { name: "Delete" }).click();
    await expect(page.getByText("Remove attachment"), "the unlink modal names the action").toBeVisible();
    // The copy has to say what actually happens: the file stays in the repository. The
    // legacy path's "permanently removed, cannot be undone" would be false here.
    await expect(
      page.getByText(/stays in the project's Files view/),
      "the modal says the file survives the unlink"
    ).toBeVisible();
    await expect(page.getByText(/permanently removed/), "and never claims a deletion").toHaveCount(0);
    await page.getByRole("button", { name: "Remove" }).click();

    await expect(attachmentRow, "the work item stops rendering the attachment").toHaveCount(0);

    // --- the file is intact: listed, downloadable, link_count 0 ---------------------
    const after = await readEntityLinks(page, issueId);
    expect(after.results, "the entity listing is empty after the unlink").toEqual([]);

    const detail = await readFileDetail(page, fileId);
    expect(detail.link_count, "the file's link count drops (AC-21)").toBe(0);
    expect(detail.file.id).toBe(fileId);
    expect(detail.permissions.can_download, "the file is still downloadable").toBe(true);

    const afterListing = await getListBody(page, { folder_id: "root" });
    const stillListed = afterListing.results.filter((result) => result.id === fileId);
    expect(stillListed.length, "the file stays in the project's Files view").toBe(1);

    const downloadResponse = await page.request.get(`${listUrl()}${fileId}/download/`);
    expect(downloadResponse.ok(), "the download endpoint still signs a URL").toBe(true);
    const access = (await downloadResponse.json()) as TAccessUrl;
    expect(access.disposition, "and forces attachment").toBe("attachment");
    const served = await fetchSignedBytes(page, access.url);
    expect(served.status()).toBe(200);
    expect(Buffer.from(await served.body()).equals(ATTACHMENT_BYTES), "the stored bytes are unchanged").toBe(true);
  });

  test("uploading through the work item's own surface stores one project file with that id", async ({ page }) => {
    const issueId = ISSUE_UPLOAD_ID;
    await page.goto(`${WEB_URL}${workItemUrl(issueId)}`);

    // The section starts empty, so the quick action in its header is the upload door the
    // product offers here; its input is the one the widget owns.
    const input = page.getByTestId("issue-attachment-quick-input");
    await expect(input).toBeAttached();
    await input.setInputFiles({
      name: "from-the-surface.txt",
      mimeType: "text/plain",
      buffer: ATTACHMENT_BYTES,
    });

    // The API is the source of truth: the upload's own link and file id.
    await expect
      .poll(async () => (await readEntityLinks(page, issueId)).results.length, {
        message: "the upload created one linked project file",
        timeout: 30_000,
      })
      .toBe(1);

    const links = await readEntityLinks(page, issueId);
    const file = links.results[0].file;
    expect(file.name_display).toBe("from-the-surface.txt");
    expect(file.size_bytes).toBe(ATTACHMENT_BYTES.length);

    // The row the surface renders carries that file's id and name.
    const attachmentRow = page.locator(`[data-testid="issue-attachment-${file.id}"]`);
    await expect(attachmentRow).toBeVisible();
    await expect(attachmentRow).toContainText(file.name_display);

    // And the project's Files view lists the same id, once.
    const listing = await getListBody(page, { folder_id: "root" });
    expect(listing.results.filter((result) => result.id === file.id).length).toBe(1);
  });
});
