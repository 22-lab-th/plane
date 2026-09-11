export type TSSOReadiness = {
  deploymentEnabled: boolean;
  configurationReady: boolean;
  recoveryReady: boolean;
};

export const getInteractiveTestStatus = (search: string) => {
  const outcome = new URLSearchParams(search).get("oidc_test");
  if (outcome === "success") return "Interactive login passed. This configuration is ready to enable.";
  if (outcome === "failed") return "Interactive login failed. Review the IdP client and redirect URI.";
  return "";
};

export const canEnableSSO = (readiness: TSSOReadiness) => readiness.deploymentEnabled && readiness.configurationReady;

export const canEnforceSSO = (readiness: TSSOReadiness) => canEnableSSO(readiness) && readiness.recoveryReady;
