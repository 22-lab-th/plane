import { useState } from "react";
import useSWR from "swr";
import { Button } from "@makeplane/propel/components/button";
import { JiraService, jiraErrorMessage } from "@plane/services";
import type { JiraConfig } from "@plane/services";
import { PageWrapper } from "@/components/common/page-wrapper";
import type { Route } from "./+types/page";

const service = new JiraService();

function ConfigurationForm({ config, onSaved }: { config: JiraConfig; onSaved: (value: JiraConfig) => void }) {
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
      setMessage("Jira settings saved.");
    } catch (err) {
      setError(jiraErrorMessage(err));
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
      setError(jiraErrorMessage(err));
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
        Enable direct Jira imports
      </label>
      {(
        [
          ["site_url", "Jira site URL", "https://your-site.atlassian.net", "url"],
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
        The saved token is encrypted and omitted from responses. The account needs access to the projects, issues,
        comments, boards, sprints and attachments you import. For a scoped token, enter its Cloud ID and grant Jira
        project, issue, comment, attachment and field read scopes, plus read:board-scope:jira-software and
        read:sprint:jira-software for cycles.
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
        After saving, open Project settings → Jira Import. Jobs run in the background and retain progress and retry
        history.
      </p>
    </form>
  );
}

export default function JiraPage() {
  const { data, error, mutate } = useSWR("JIRA_CONFIGURATION", () => service.config());
  return (
    <PageWrapper
      header={{
        title: "Jira",
        description: "Connect Jira Cloud to import projects, work items, sprints and attachments into Plane.",
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
        <p role="alert">{jiraErrorMessage(error)}</p>
      ) : (
        <p role="status">Loading configuration…</p>
      )}
    </PageWrapper>
  );
}
export const meta: Route.MetaFunction = () => [{ title: "Jira - God Mode" }];
