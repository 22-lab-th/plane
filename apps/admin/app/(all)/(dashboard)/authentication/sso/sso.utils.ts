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

export const getNormalAuthMethodLabels = (config: {
  isEmailPasswordEnabled: boolean;
  isMagicLoginEnabled: boolean;
  isSmtpConfigured: boolean;
  isGoogleEnabled: boolean;
  isGithubEnabled: boolean;
  isGitlabEnabled: boolean;
  isGiteaEnabled: boolean;
}) => {
  const methods: string[] = [];
  if (config.isEmailPasswordEnabled) methods.push("email/password");
  if (config.isMagicLoginEnabled && config.isSmtpConfigured) methods.push("magic code");
  if (config.isGoogleEnabled) methods.push("Google");
  if (config.isGithubEnabled) methods.push("GitHub");
  if (config.isGitlabEnabled) methods.push("GitLab");
  if (config.isGiteaEnabled) methods.push("Gitea");
  return methods;
};
