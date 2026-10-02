import { expect, test, type Locator } from "@playwright/test";

import { OWNER_EMAIL, OWNER_PASSWORD, PROJECT_ID, signIn, WEB_URL, WORKSPACE_SLUG } from "./support";

type PanelGeometry = {
  horizontalOverlap: number;
  verticalGap: number;
};

async function readPanelGeometry(trigger: Locator, panel: Locator): Promise<PanelGeometry> {
  const triggerBox = await trigger.boundingBox();
  const panelBox = await panel.boundingBox();

  expect(triggerBox, "dropdown trigger must have a layout box").not.toBeNull();
  expect(panelBox, "open dropdown panel must have a layout box").not.toBeNull();
  if (!triggerBox || !panelBox) throw new Error("Dropdown geometry was not available");

  const horizontalOverlap =
    Math.min(triggerBox.x + triggerBox.width, panelBox.x + panelBox.width) - Math.max(triggerBox.x, panelBox.x);
  const triggerBottom = triggerBox.y + triggerBox.height;
  const panelBottom = panelBox.y + panelBox.height;
  const verticalGap = panelBox.y >= triggerBottom ? panelBox.y - triggerBottom : triggerBox.y - panelBottom;

  return { horizontalOverlap, verticalGap };
}

async function expectPanelAnchoredToTrigger(trigger: Locator, panel: Locator): Promise<void> {
  await expect(panel).toBeVisible();

  await expect
    .poll(async () => {
      const { horizontalOverlap, verticalGap } = await readPanelGeometry(trigger, panel);
      return horizontalOverlap > 0 && verticalGap >= -1 && verticalGap <= 24;
    })
    .toBe(true);

  const { horizontalOverlap, verticalGap } = await readPanelGeometry(trigger, panel);
  expect(horizontalOverlap, "dropdown must overlap its trigger horizontally").toBeGreaterThan(0);
  expect(verticalGap, "dropdown must open adjacent to its trigger").toBeGreaterThanOrEqual(-1);
  expect(verticalGap, "dropdown must open adjacent to its trigger").toBeLessThanOrEqual(24);
}

test("project settings comboboxes stay anchored to their triggers", async ({ page }) => {
  await signIn(page, OWNER_EMAIL, OWNER_PASSWORD);
  await page.goto(`${WEB_URL}/${WORKSPACE_SLUG}/settings/projects/${PROJECT_ID}/`);

  const timezoneHeading = page.getByRole("heading", { name: /project timezone/i });
  await expect(timezoneHeading).toBeVisible({ timeout: 90_000 });
  const timezoneTrigger = timezoneHeading.locator("..").getByRole("button");
  await timezoneTrigger.click();
  await expectPanelAnchoredToTrigger(timezoneTrigger, page.getByRole("listbox"));

  await timezoneTrigger.click();
  await expect(page.getByRole("listbox")).toBeHidden();

  const networkHeading = page.getByRole("heading", { name: /^network$/i });
  const networkTrigger = networkHeading.locator("..").getByRole("button");
  await networkTrigger.click();
  await expectPanelAnchoredToTrigger(networkTrigger, page.getByRole("listbox"));
});
