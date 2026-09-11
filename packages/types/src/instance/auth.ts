/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

export type TCoreInstanceAuthenticationModeKeys =
  | "unique-codes"
  | "passwords-login"
  | "google"
  | "github"
  | "gitlab"
  | "gitea";

export type TInstanceAuthenticationModeKeys = TCoreInstanceAuthenticationModeKeys;

export type TInstanceAuthenticationModes = {
  key: TInstanceAuthenticationModeKeys;
  name: string;
  description: string;
  icon: React.ReactNode;
  config: React.ReactNode;
  enabledConfigKey: TInstanceAuthenticationMethodKeys;
  unavailable?: boolean;
};

export type TInstanceAuthenticationMethodKeys =
  | "ENABLE_SIGNUP"
  | "ENABLE_MAGIC_LINK_LOGIN"
  | "ENABLE_EMAIL_PASSWORD"
  | "IS_GOOGLE_ENABLED"
  | "IS_GITHUB_ENABLED"
  | "IS_GITLAB_ENABLED"
  | "IS_GITEA_ENABLED";

export type TInstanceGoogleAuthenticationConfigurationKeys =
  | "GOOGLE_CLIENT_ID"
  | "GOOGLE_CLIENT_SECRET"
  | "ENABLE_GOOGLE_SYNC";

export type TInstanceGithubAuthenticationConfigurationKeys =
  | "GITHUB_CLIENT_ID"
  | "GITHUB_CLIENT_SECRET"
  | "GITHUB_ORGANIZATION_ID"
  | "ENABLE_GITHUB_SYNC";

export type TInstanceGitlabAuthenticationConfigurationKeys =
  | "GITLAB_HOST"
  | "GITLAB_CLIENT_ID"
  | "GITLAB_CLIENT_SECRET"
  | "ENABLE_GITLAB_SYNC";

export type TInstanceGiteaAuthenticationConfigurationKeys =
  | "GITEA_HOST"
  | "GITEA_CLIENT_ID"
  | "GITEA_CLIENT_SECRET"
  | "ENABLE_GITEA_SYNC";

export type TInstanceAuthenticationConfigurationKeys =
  | TInstanceGoogleAuthenticationConfigurationKeys
  | TInstanceGithubAuthenticationConfigurationKeys
  | TInstanceGitlabAuthenticationConfigurationKeys
  | TInstanceGiteaAuthenticationConfigurationKeys;

export type TInstanceAuthenticationKeys = TInstanceAuthenticationMethodKeys | TInstanceAuthenticationConfigurationKeys;

export type TGetBaseAuthenticationModeProps = {
  disabled: boolean;
  updateConfig: (key: TInstanceAuthenticationMethodKeys, value: string) => void;
  resolvedTheme: string | undefined;
};

export type TOAuthOption = {
  id: string;
  text: string;
  icon: React.ReactNode;
  onClick: () => void;
  enabled?: boolean;
};

export type TOAuthConfigs = {
  isOAuthEnabled: boolean;
  oAuthOptions: TOAuthOption[];
};

export type TCoreLoginMediums = "email" | "magic-code" | "github" | "gitlab" | "google" | "gitea";

export type TSSOProvider = {
  id: string;
  name: string;
  slug: string;
  protocol: "oidc";
  issuer_url: string;
  client_id: string;
  client_secret_configured: boolean;
  configuration_ready: boolean;
  recovery_ready: boolean;
  deployment_enabled: boolean;
  mode: "disabled" | "optional" | "enforced";
  scopes: string[];
  claim_mappings: Record<string, string>;
  allowed_email_domains: string[];
  allowed_groups: string[];
  jit_provisioning_enabled: boolean;
  allow_verified_email_auto_link: boolean;
  is_enabled: boolean;
  is_enforced: boolean;
  metadata_tested_at: string | null;
  configuration_tested_at: string | null;
  recovery_tested_at: string | null;
  created_at: string;
  updated_at: string;
};

export type TSSOProviderPayload = Omit<
  TSSOProvider,
  | "id"
  | "client_secret_configured"
  | "configuration_ready"
  | "recovery_ready"
  | "deployment_enabled"
  | "mode"
  | "metadata_tested_at"
  | "configuration_tested_at"
  | "recovery_tested_at"
  | "created_at"
  | "updated_at"
> & {
  client_secret?: string;
};

export type TSSOTestResult = {
  status: "passed" | "failed";
  issuer?: string;
  metadata_tested_at?: string;
  code?: string;
  error?: string;
};

export type TSSORecoveryTestResult = {
  status: "passed" | "failed";
  recovery_tested_at?: string;
  error?: string;
};
