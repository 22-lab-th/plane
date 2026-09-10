/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useEffect, useState } from "react";
import useSWR from "swr";
// plane imports
import { API_BASE_URL } from "@plane/constants";
import { Button } from "@plane/propel/button";
import { TOAST_TYPE, setToast } from "@plane/propel/toast";
import { InstanceService } from "@plane/services";
import type { TSSOProvider, TSSOProviderPayload } from "@plane/types";
import { Input, ToggleSwitch } from "@plane/ui";
// components
import { PageWrapper } from "@/components/common/page-wrapper";
// types
import type { Route } from "./+types/page";

const instanceService = new InstanceService();

const EMPTY_PROVIDER: TSSOProviderPayload = {
  name: "",
  slug: "",
  protocol: "oidc",
  issuer_url: "",
  client_id: "",
  scopes: ["openid", "email", "profile"],
  claim_mappings: {},
  allowed_email_domains: [],
  allowed_groups: [],
  jit_provisioning_enabled: false,
  allow_verified_email_auto_link: false,
  is_enabled: false,
  is_enforced: false,
};

const listValue = (values: string[]) => values.join(", ");
const parseList = (value: string) => [
  ...new Set(
    value
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean)
  ),
];

const errorMessage = (error: unknown) => {
  if (typeof error === "object" && error !== null) {
    const details = error as { error?: string; detail?: string };
    return details.error ?? details.detail ?? "The SSO configuration could not be saved.";
  }
  return "The SSO configuration could not be saved.";
};

function Field(props: {
  id: string;
  label: string;
  value: string;
  onChange: (value: string) => void;
  type?: "text" | "password";
  placeholder?: string;
  required?: boolean;
  description?: string;
}) {
  return (
    <label className="flex flex-col gap-1 text-13 text-secondary" htmlFor={props.id}>
      <span className="font-medium text-primary">{props.label}</span>
      <Input
        id={props.id}
        name={props.id}
        type={props.type ?? "text"}
        value={props.value}
        onChange={(event) => props.onChange(event.target.value)}
        placeholder={props.placeholder}
        required={props.required}
        className="w-full"
      />
      {props.description && <span className="text-11 text-tertiary">{props.description}</span>}
    </label>
  );
}

export default function InstanceSSOAuthenticationPage(_props: Route.ComponentProps) {
  const [provider, setProvider] = useState<TSSOProvider>();
  const [form, setForm] = useState<TSSOProviderPayload>(EMPTY_PROVIDER);
  const [secret, setSecret] = useState("");
  const [isSaving, setIsSaving] = useState(false);
  const [isTesting, setIsTesting] = useState(false);
  const [testStatus, setTestStatus] = useState("");

  const { data, isLoading, mutate } = useSWR("INSTANCE_SSO_PROVIDERS", () => instanceService.ssoProviders());

  useEffect(() => {
    const currentProvider = data?.[0];
    if (!currentProvider) return;
    setProvider(currentProvider);
    setForm({
      name: currentProvider.name,
      slug: currentProvider.slug,
      protocol: currentProvider.protocol,
      issuer_url: currentProvider.issuer_url,
      client_id: currentProvider.client_id,
      scopes: currentProvider.scopes,
      claim_mappings: currentProvider.claim_mappings,
      allowed_email_domains: currentProvider.allowed_email_domains,
      allowed_groups: currentProvider.allowed_groups,
      jit_provisioning_enabled: currentProvider.jit_provisioning_enabled,
      allow_verified_email_auto_link: currentProvider.allow_verified_email_auto_link,
      is_enabled: currentProvider.is_enabled,
      is_enforced: currentProvider.is_enforced,
    });
  }, [data]);

  const updateForm = <Key extends keyof TSSOProviderPayload>(key: Key, value: TSSOProviderPayload[Key]) => {
    setForm((current) => ({ ...current, [key]: value }));
  };

  const save = async (overrides: Partial<TSSOProviderPayload> = {}) => {
    setIsSaving(true);
    try {
      const payload = { ...form, ...overrides };
      if (secret) payload.client_secret = secret;
      const saved = provider
        ? await instanceService.updateSSOProvider(provider.id, payload)
        : await instanceService.createSSOProvider(payload);
      setProvider(saved);
      setSecret("");
      await mutate();
      setToast({ type: TOAST_TYPE.SUCCESS, title: "SSO configuration saved", message: "Secrets remain masked." });
      return saved;
    } catch (error) {
      setToast({ type: TOAST_TYPE.ERROR, title: "Could not save SSO", message: errorMessage(error) });
      return undefined;
    } finally {
      setIsSaving(false);
    }
  };

  const testConnection = async () => {
    const saved = await save({ is_enabled: false, is_enforced: false });
    if (!saved) return;
    setIsTesting(true);
    setTestStatus("Testing discovery and signing keys…");
    try {
      const result = await instanceService.testSSOProvider(saved.id);
      setTestStatus(`Connection passed for ${result.issuer}. You can now enable SSO.`);
      await mutate();
    } catch (error) {
      setTestStatus(errorMessage(error));
    } finally {
      setIsTesting(false);
    }
  };

  const toggleEnabled = async () => {
    const enabled = !form.is_enabled;
    const saved = await save({ is_enabled: enabled, is_enforced: enabled ? form.is_enforced : false });
    if (saved) updateForm("is_enabled", saved.is_enabled);
  };

  const toggleEnforced = async () => {
    const enforced = !form.is_enforced;
    const saved = await save({ is_enforced: enforced });
    if (saved) updateForm("is_enforced", saved.is_enforced);
  };

  const browserOrigin = typeof window === "undefined" ? "" : window.location.origin;
  const callbackURL = `${API_BASE_URL || browserOrigin}/auth/sso/callback/`;

  return (
    <PageWrapper
      header={{
        title: "OpenID Connect SSO",
        description: "Configure and test one active identity provider. Plane stores the client secret encrypted.",
      }}
    >
      <form
        className="grid max-w-4xl grid-cols-1 gap-5 md:grid-cols-2"
        onSubmit={(event) => {
          event.preventDefault();
          void save();
        }}
      >
        <Field
          id="sso-name"
          label="Provider name"
          value={form.name}
          onChange={(value) => updateForm("name", value)}
          required
        />
        <Field
          id="sso-slug"
          label="Provider slug"
          value={form.slug}
          onChange={(value) => updateForm("slug", value)}
          required
        />
        <Field
          id="sso-issuer"
          label="Issuer URL"
          value={form.issuer_url}
          onChange={(value) => updateForm("issuer_url", value)}
          placeholder="https://id.example.com"
          required
        />
        <Field
          id="sso-client-id"
          label="Client ID"
          value={form.client_id}
          onChange={(value) => updateForm("client_id", value)}
          required
        />
        <Field
          id="sso-client-secret"
          label={provider?.client_secret_configured ? "Replace client secret (optional)" : "Client secret"}
          value={secret}
          onChange={setSecret}
          type="password"
          required={!provider?.client_secret_configured}
          description={
            provider?.client_secret_configured ? "A secret is configured. Its value is never returned." : undefined
          }
        />
        <Field
          id="sso-scopes"
          label="Scopes"
          value={listValue(form.scopes)}
          onChange={(value) => updateForm("scopes", parseList(value))}
          description="Comma-separated; openid and email are required."
          required
        />
        <Field
          id="sso-domains"
          label="Allowed email domains"
          value={listValue(form.allowed_email_domains)}
          onChange={(value) => updateForm("allowed_email_domains", parseList(value))}
          description="Leave empty to allow every verified domain."
        />
        <Field
          id="sso-groups"
          label="Allowed groups"
          value={listValue(form.allowed_groups)}
          onChange={(value) => updateForm("allowed_groups", parseList(value))}
          description="Leave empty to skip group filtering."
        />
        <Field
          id="sso-email-claim"
          label="Email claim"
          value={form.claim_mappings.email ?? "email"}
          onChange={(value) => updateForm("claim_mappings", { ...form.claim_mappings, email: value })}
          required
        />
        <Field
          id="sso-groups-claim"
          label="Groups claim"
          value={form.claim_mappings.groups ?? "groups"}
          onChange={(value) => updateForm("claim_mappings", { ...form.claim_mappings, groups: value })}
          required
        />
        <div className="rounded-lg border border-subtle bg-layer-2 p-4 text-13 md:col-span-2">
          <div className="font-medium text-primary">Redirect URI</div>
          <code className="mt-1 block break-all text-secondary">{callbackURL}</code>
        </div>
        <div className="flex items-center justify-between gap-4 rounded-lg border border-subtle p-4">
          <span>
            <span className="block font-medium text-primary">Just-in-time provisioning</span>
            <span className="text-11 text-tertiary">Create a user only after domain and group policy passes.</span>
          </span>
          <ToggleSwitch
            label="Just-in-time provisioning"
            value={form.jit_provisioning_enabled}
            onChange={() => updateForm("jit_provisioning_enabled", !form.jit_provisioning_enabled)}
            size="sm"
          />
        </div>
        <div className="flex items-center justify-between gap-4 rounded-lg border border-subtle p-4">
          <span>
            <span className="block font-medium text-primary">Verified email auto-link</span>
            <span className="text-11 text-tertiary">
              Link an existing account only when the IdP verifies its email.
            </span>
          </span>
          <ToggleSwitch
            label="Verified email auto-link"
            value={form.allow_verified_email_auto_link}
            onChange={() => updateForm("allow_verified_email_auto_link", !form.allow_verified_email_auto_link)}
            size="sm"
          />
        </div>
        <div className="flex flex-wrap items-center gap-3 md:col-span-2">
          <Button type="submit" variant="primary" loading={isSaving}>
            Save configuration
          </Button>
          <Button
            type="button"
            variant="secondary"
            loading={isTesting}
            disabled={isSaving}
            onClick={() => void testConnection()}
          >
            Test connection
          </Button>
          <Button
            type="button"
            variant="secondary"
            disabled={!provider?.configuration_tested_at || isSaving}
            onClick={() => void toggleEnabled()}
          >
            {form.is_enabled ? "Disable SSO" : "Enable SSO"}
          </Button>
          <Button
            type="button"
            variant="secondary"
            disabled={!form.is_enabled || !provider?.configuration_tested_at || isSaving}
            onClick={() => void toggleEnforced()}
          >
            {form.is_enforced ? "Stop enforcing SSO" : "Enforce SSO"}
          </Button>
        </div>
        <p className="text-11 text-tertiary md:col-span-2">
          Enforcement requires an active instance administrator listed in SSO_BREAK_GLASS_ADMIN_EMAILS. Recovery
          sessions are rate-limited and expire after the configured short lifetime.
        </p>
        <div className="min-h-5 text-13 text-secondary md:col-span-2" role="status" aria-live="polite">
          {isLoading ? "Loading SSO configuration…" : testStatus}
        </div>
      </form>
    </PageWrapper>
  );
}

export const meta: Route.MetaFunction = () => [{ title: "OpenID Connect SSO - God Mode" }];
