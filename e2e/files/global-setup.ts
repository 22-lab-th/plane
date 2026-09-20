/**
 * Cold-start priming for the Files harness (T-113).
 *
 * A dev container compiles the Files route on its first hit, and the compile can take
 * minutes: a run that started with a test paid that cost inside the test's own timeout,
 * and the run then failed with "`files-root` never became visible" rather than on
 * anything the suite asserts. The route is therefore warmed once per run, here, where no
 * test timeout applies — the tests themselves keep their own waits and assertions.
 *
 * Nothing about the app is stubbed or cached by hand: this signs in through the API and
 * navigates exactly as the specs do, so the server is warmed by a real visit.
 */

import { chromium } from "@playwright/test";
// harness
import { primeFilesRoute, WEB_URL } from "./support";

export default async function globalSetup(): Promise<void> {
  const browser = await chromium.launch();
  try {
    const context = await browser.newContext({ baseURL: WEB_URL, ignoreHTTPSErrors: true });
    const page = await context.newPage();
    await primeFilesRoute(page);
  } finally {
    await browser.close();
  }
}
