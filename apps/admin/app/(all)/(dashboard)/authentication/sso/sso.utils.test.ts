import { describe, expect, it } from "vitest";

import { canEnableSSO, canEnforceSSO, getInteractiveTestStatus, getNormalAuthMethodLabels } from "./sso.utils";

describe("OIDC SSO readiness", () => {
  it("keeps actions inactive when the deployment gate is off", () => {
    const readiness = { deploymentEnabled: false, configurationReady: true, recoveryReady: true };
    expect(canEnableSSO(readiness)).toBe(false);
    expect(canEnforceSSO(readiness)).toBe(false);
  });

  it("requires interactive and recovery readiness for enforcement", () => {
    expect(canEnforceSSO({ deploymentEnabled: true, configurationReady: true, recoveryReady: false })).toBe(false);
    expect(canEnforceSSO({ deploymentEnabled: true, configurationReady: true, recoveryReady: true })).toBe(true);
  });

  it("maps interactive callback outcomes to safe status messages", () => {
    expect(getInteractiveTestStatus("?oidc_test=success")).toContain("passed");
    expect(getInteractiveTestStatus("?oidc_test=failed&code=invalid_id_token")).toContain("failed");
    expect(getInteractiveTestStatus("?code=unrelated")).toBe("");
  });

  it("lists only usable normal authentication methods", () => {
    expect(
      getNormalAuthMethodLabels({
        isEmailPasswordEnabled: true,
        isMagicLoginEnabled: true,
        isSmtpConfigured: false,
        isGoogleEnabled: false,
        isGithubEnabled: true,
        isGitlabEnabled: false,
        isGiteaEnabled: false,
      })
    ).toEqual(["email/password", "GitHub"]);
  });
});
