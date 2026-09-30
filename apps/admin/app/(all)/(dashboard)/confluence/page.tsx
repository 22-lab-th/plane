import { useState } from "react";
import useSWR from "swr";
import { Button } from "@makeplane/propel/components/button";
import { ConfluenceService, confluenceErrorMessage } from "@plane/services";
import type { ConfluenceConfig } from "@plane/services";
import { PageWrapper } from "@/components/common/page-wrapper";
import type { Route } from "./+types/page";

const service = new ConfluenceService();

function ConfigurationForm({
  config,
  onSaved,
}: {
  config: ConfluenceConfig;
  onSaved: (value: ConfluenceConfig) => void;
}) {
  const [values, setValues] = useState({
    enabled: config.enabled,
    site_url: config.site_url,
    email: config.email,
    cloud_id: config.cloud_id,
    api_token: "",
  });
  const [clearToken, setClearToken] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const save = async () => {
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const { api_token, ...settings } = values;
      const saved = await service.saveConfig({ ...settings, ...(clearToken || api_token ? { api_token } : {}) });
      setValues({
        enabled: saved.enabled,
        site_url: saved.site_url,
        email: saved.email,
        cloud_id: saved.cloud_id,
        api_token: "",
      });
      setClearToken(false);
      onSaved(saved);
      setMessage("Confluence settings saved.");
    } catch (err) {
      setError(confluenceErrorMessage(err));
    } finally {
      setBusy(false);
    }
  };
  const test = async () => {
    setBusy(true);
    setError("");
    setMessage("");
    try {
      setMessage((await service.testConnection()).message);
    } catch (err) {
      setError(confluenceErrorMessage(err));
    } finally {
      setBusy(false);
    }
  };
  const inputClass = "w-full rounded-md border border-subtle bg-surface-1 px-3 py-2 text-13 text-primary";
  return (
    <form
      className="max-w-3xl space-y-6"
      onSubmit={(event) => {
        event.preventDefault();
        void save();
      }}
    >
      <label className="flex items-center gap-2 text-13 text-primary">
        <input
          type="checkbox"
          checked={values.enabled}
          disabled={busy}
          onChange={(e) => setValues({ ...values, enabled: e.target.checked })}
        />
        Enable direct Confluence imports
      </label>
      {(
        [
          ["site_url", "Confluence site URL", "https://your-site.atlassian.net", "url"],
          ["email", "Atlassian account email", "name@example.com", "email"],
          ["cloud_id", "Cloud ID (for scoped API tokens)", "Optional Atlassian Cloud ID UUID", "text"],
        ] as const
      ).map(([key, label, placeholder, type]) => (
        <label key={key} className="block space-y-2 text-13 text-primary">
          <span>{label}</span>
          <input
            className={inputClass}
            type={type}
            value={values[key]}
            placeholder={placeholder}
            disabled={busy}
            onChange={(e) => setValues({ ...values, [key]: e.target.value })}
          />
        </label>
      ))}
      <label className="block space-y-2 text-13 text-primary">
        <span>API token</span>
        <input
          className={inputClass}
          type="password"
          autoComplete="new-password"
          value={values.api_token}
          disabled={busy || clearToken}
          placeholder={config.token_configured ? "Token saved — leave blank to keep it" : "Atlassian API token"}
          onChange={(e) => setValues({ ...values, api_token: e.target.value })}
        />
      </label>
      <p className="text-12 text-tertiary">
        The token is encrypted and never returned to the browser. The account needs access to the spaces, pages and
        attachments you import. For a scoped token, enter its Cloud ID and grant Confluence read scopes.
      </p>
      {config.token_configured && (
        <label className="flex items-center gap-2 text-13 text-primary">
          <input
            type="checkbox"
            checked={clearToken}
            disabled={busy || values.enabled}
            onChange={(e) => {
              setClearToken(e.target.checked);
              setValues({ ...values, api_token: "" });
            }}
          />
          Remove saved token (disable imports first)
        </label>
      )}
      {error && (
        <p role="alert" className="text-13 text-danger-primary">
          {error}
        </p>
      )}
      {message && (
        <p role="status" className="text-13 text-primary">
          {message}
        </p>
      )}
      <div className="flex gap-3">
        <Button
          variant="primary"
          size="md"
          stretch="auto"
          label="Save settings"
          loading={busy}
          disabled={busy}
          type="submit"
        />
        <Button
          variant="secondary"
          size="md"
          stretch="auto"
          label="Test saved connection"
          type="button"
          disabled={busy || !config.enabled}
          onClick={() => void test()}
        />
      </div>
      <p className="text-12 text-tertiary">
        After saving, open Project → Pages → Import Confluence → Atlassian API. Jobs run in the background and retain
        progress and retry history.
      </p>
    </form>
  );
}

export default function ConfluencePage() {
  const { data, error, mutate } = useSWR("CONFLUENCE_CONFIGURATION", () => service.config());
  return (
    <PageWrapper
      header={{
        title: "Confluence",
        description: "Connect Confluence Cloud to import spaces, pages and attachments into Plane.",
      }}
    >
      {data ? (
        <ConfigurationForm
          config={data}
          onSaved={(value) => {
            void mutate(value, false);
          }}
        />
      ) : error ? (
        <p role="alert">{confluenceErrorMessage(error)}</p>
      ) : (
        <p role="status">Loading configuration…</p>
      )}
    </PageWrapper>
  );
}
export const meta: Route.MetaFunction = () => [{ title: "Confluence - God Mode" }];
