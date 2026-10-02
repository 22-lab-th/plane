import { useEffect, useRef, useState } from "react";
import useSWR from "swr";
import { JiraService, jiraErrorMessage, isJiraRunActive } from "@plane/services";
import type { JiraProject, ImportMode } from "@plane/services";
import { Button } from "@plane/propel/button";

const service = new JiraService();
type Props = {
  workspaceSlug: string;
  projectId: string;
  onImported: () => Promise<unknown>;
};

export function JiraAPIImport({ workspaceSlug, projectId, onImported }: Props) {
  const [remoteProjectId, setRemoteProjectId] = useState("");
  const [projects, setProjects] = useState<JiraProject[]>([]);
  const [cursor, setCursor] = useState<number | null>(null);
  const [runId, setRunId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [offset, setOffset] = useState(0);
  const [failuresOnly, setFailuresOnly] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [mapping, setMapping] = useState<Record<string, string>>({});
  const [accountId, setAccountId] = useState("");
  const [memberId, setMemberId] = useState("");
  const [mappingDirty, setMappingDirty] = useState(false);
  const { data: members } = useSWR(["JIRA_MEMBERS", workspaceSlug, projectId], () =>
    service.members(workspaceSlug, projectId)
  );
  const refreshedRuns = useRef(new Set<string>());
  const completedCallback = useRef(onImported);
  completedCallback.current = onImported;
  const { data: initialProjects, error: projectsError } = useSWR(["JIRA_PROJECTS", workspaceSlug, projectId], () =>
    service.projects(workspaceSlug, projectId)
  );
  const {
    data: runs,
    error: runsError,
    mutate: refreshRuns,
  } = useSWR(["JIRA_RUNS", workspaceSlug, projectId], () => service.runs(workspaceSlug, projectId), {
    refreshInterval: (values) => (values?.some(isJiraRunActive) ? 4000 : 0),
  });
  useEffect(() => {
    if (initialProjects) {
      setProjects(initialProjects.results);
      setCursor(initialProjects.next_offset);
    }
  }, [initialProjects]);
  useEffect(() => {
    if (!runId && runs?.length) setRunId(runs[0].id);
  }, [runs, runId]);
  const {
    data: detail,
    error: detailError,
    mutate: refreshDetail,
  } = useSWR(
    runId ? ["JIRA_RUN", workspaceSlug, projectId, runId, offset, failuresOnly] : null,
    () => service.detail(workspaceSlug, projectId, runId, offset, failuresOnly ? "failed" : undefined),
    { refreshInterval: (value) => (!value || isJiraRunActive(value.run) ? 3000 : 0) }
  );
  const run = detail?.run;
  const active = isJiraRunActive(run);
  useEffect(() => {
    if (detail && !mappingDirty) setMapping(detail.user_mapping);
  }, [detail, mappingDirty]);
  useEffect(() => {
    if (run && !isJiraRunActive(run) && !refreshedRuns.current.has(run.id)) {
      refreshedRuns.current.add(run.id);
      void completedCallback.current().catch(() => undefined);
      void refreshRuns();
    }
  }, [run, refreshRuns]);
  const chooseRun = (id: string) => {
    setMappingDirty(false);
    setRunId(id);
    setOffset(0);
    setSelected(new Set());
    setFailuresOnly(false);
    setError("");
  };
  const start = async (mode: ImportMode, newProject = false) => {
    if (busy || (!newProject && (!run || active))) return;
    setBusy(true);
    setError("");
    try {
      const created = await service.start(workspaceSlug, projectId, {
        ...(newProject ? { remote_project_id: remoteProjectId } : { source_id: run?.source_id }),
        mode,
        ...(mappingDirty ? { user_mapping: mapping } : {}),
        ...(mode === "selected" ? { item_ids: [...selected] } : {}),
      });
      chooseRun(created.id);
      await refreshRuns();
    } catch (err) {
      setError(jiraErrorMessage(err));
      await refreshRuns();
    } finally {
      setBusy(false);
    }
  };
  const moreProjects = async () => {
    if (cursor == null) return;
    setBusy(true);
    setError("");
    try {
      const page = await service.projects(workspaceSlug, projectId, cursor);
      setProjects((current) => [...new Map([...current, ...page.results].map((space) => [space.id, space])).values()]);
      setCursor(page.next_offset);
    } catch (err) {
      setError(jiraErrorMessage(err));
    } finally {
      setBusy(false);
    }
  };
  const inputClass = "rounded border border-subtle bg-surface-1 px-2 py-2 text-13 text-secondary";
  return (
    <div className="space-y-4">
      <p className="text-13 text-tertiary">
        Connect using credentials saved in God Mode → Jira. Work items, comments, attachments and sprints are imported
        in the background; you can close this window and return to check progress.
      </p>
      <p className="text-12 text-tertiary">
        Imported work items, comments, files and cycles are visible according to the destination project permissions.
      </p>
      {projectsError && (
        <p role="alert" className="text-13 text-danger-primary">
          {jiraErrorMessage(projectsError)}
        </p>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <label className="sr-only" htmlFor="jira-project">
          Jira project
        </label>
        <select
          id="jira-project"
          value={remoteProjectId}
          onChange={(event) => setRemoteProjectId(event.target.value)}
          className={`min-w-0 flex-1 ${inputClass}`}
          disabled={busy}
        >
          <option value="">Choose a Jira project</option>
          {projects.map((space) => (
            <option key={space.id} value={space.id}>
              {space.name} ({space.key})
            </option>
          ))}
        </select>
        {cursor != null && (
          <Button variant="secondary" size="sm" onClick={() => void moreProjects()} disabled={busy}>
            More projects
          </Button>
        )}
        <Button
          variant="primary"
          size="sm"
          disabled={!remoteProjectId || busy}
          loading={busy}
          onClick={() => void start("changed", true)}
        >
          Import / sync project
        </Button>
      </div>
      <details className="rounded border border-subtle p-3">
        <summary className="cursor-pointer text-13 font-medium text-secondary">User mappings</summary>
        <p className="my-2 text-12 text-tertiary">
          Users are matched by visible email to active project members. When Jira hides an email, use the account ID
          shown in item warnings. Unmatched authors retain their names in comments; no invitations are sent.
        </p>
        <div className="flex flex-wrap gap-2">
          <input
            aria-label="Jira account ID"
            value={accountId}
            onChange={(event) => setAccountId(event.target.value)}
            placeholder="Jira account ID"
            className={inputClass}
            disabled={active || busy}
          />
          <select
            aria-label="Plane project member"
            value={memberId}
            onChange={(event) => setMemberId(event.target.value)}
            className={inputClass}
            disabled={active || busy}
          >
            <option value="">Choose project member</option>
            {members?.map((member) => (
              <option key={member.id} value={member.id}>
                {member.name}
              </option>
            ))}
          </select>
          <Button
            variant="secondary"
            size="sm"
            disabled={active || busy || !accountId.trim() || !memberId}
            onClick={() => {
              setMapping((current) => ({ ...current, [accountId.trim()]: memberId }));
              setMappingDirty(true);
              setAccountId("");
            }}
          >
            Add mapping
          </Button>
        </div>
        <ul className="mt-2 space-y-1 text-12 text-secondary">
          {Object.entries(mapping).map(([account, member]) => (
            <li key={account} className="flex items-center gap-2">
              <span>
                {account} → {members?.find((value) => value.id === member)?.name ?? member}
              </span>
              <Button
                variant="secondary"
                size="sm"
                disabled={active || busy}
                onClick={() => {
                  setMapping((current) =>
                    Object.fromEntries(Object.entries(current).filter(([key]) => key !== account))
                  );
                  setMappingDirty(true);
                }}
              >
                Remove
              </Button>
            </li>
          ))}
        </ul>
        <p className="mt-2 text-12 text-tertiary">
          Mappings are saved when you start an import or sync. Sync updated items reapplies changed mappings.
        </p>
      </details>
      <p className="text-12 text-tertiary">
        Sprints become cycles. Each work item is assigned to its active sprint, otherwise its future sprint or latest
        closed sprint. All discovered sprints remain available as cycles.
      </p>
      {runsError && (
        <p role="alert" className="text-13 text-danger-primary">
          {jiraErrorMessage(runsError)}
        </p>
      )}
      {!!runs?.length && (
        <label className="block space-y-1 text-13 text-secondary">
          <span>Import history</span>
          <select value={runId} onChange={(event) => chooseRun(event.target.value)} className={`w-full ${inputClass}`}>
            {runs.map((job) => (
              <option value={job.id} key={job.id}>
                {job.project_name} — {job.status} — {new Date(job.created_at).toLocaleString()}
              </option>
            ))}
          </select>
        </label>
      )}
      {(error || detailError) && (
        <p role="alert" className="text-13 text-danger-primary">
          {error || jiraErrorMessage(detailError)}
        </p>
      )}
      {run && (
        <>
          <div aria-live="polite" role="status" className="space-y-1 text-13 text-secondary">
            <p className="font-medium">
              {run.project_name}: {run.status}
            </p>
            <p>{run.phase}</p>
            <p>
              {run.counts.total} discovered · {run.counts.completed} completed · {run.counts.failed} failed ·{" "}
              {run.counts.skipped} unchanged / unselected · {run.counts.pending + run.counts.running} remaining
            </p>
            {!run.inventory_complete && (
              <p className="text-tertiary">Inventory is incomplete; counts reflect items discovered so far.</p>
            )}
          </div>
          {run.error_message && (
            <p role="alert" className="text-13 text-danger-primary">
              {run.error_message} ({run.error_code})
            </p>
          )}
          <div className="max-h-40 overflow-auto rounded border border-subtle">
            <table className="w-full text-left text-12 text-secondary">
              <caption className="sr-only">Progress by work item, sprint, comment and file type</caption>
              <thead>
                <tr>
                  {["Type", "Total", "Completed", "Failed", "Unchanged / unselected", "Remaining"].map((label) => (
                    <th key={label} className="p-2">
                      {label}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {Object.entries(run.types).map(([type, counts]) => (
                  <tr key={type} className="border-t border-subtle">
                    <td className="p-2">{type}</td>
                    {(["total", "completed", "failed", "skipped", "remaining"] as const).map((key) => (
                      <td key={key} className="p-2">
                        {key === "remaining" ? counts.pending + counts.running : counts[key]}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="flex flex-wrap gap-2">
            <Button variant="secondary" size="sm" disabled={busy || active} onClick={() => void start("changed")}>
              Sync new / updated items
            </Button>
            <Button variant="secondary" size="sm" disabled={busy || active} onClick={() => void start("failed")}>
              Retry all failed items
            </Button>
            <Button
              variant="secondary"
              size="sm"
              disabled={busy || active || !selected.size}
              onClick={() => void start("selected")}
            >
              Retry selected ({selected.size})
            </Button>
            <Button variant="secondary" size="sm" disabled={busy || active} onClick={() => void start("all")}>
              Re-import all / overwrite
            </Button>
          </div>
          <p className="text-12 text-tertiary">
            Sync and overwrite use the same Plane work item/comment/file/cycle IDs. Overwrite replaces imported content,
            including local edits, and keeps previous work item/file versions. Retrying attachments also rebuilds their
            owner work items.
          </p>
          <label className="flex items-center gap-2 text-13 text-secondary">
            <input
              type="checkbox"
              checked={failuresOnly}
              onChange={(event) => {
                setFailuresOnly(event.target.checked);
                setOffset(0);
              }}
            />{" "}
            Show failures only
          </label>
          <div className="max-h-64 overflow-auto rounded border border-subtle">
            <table className="w-full text-left text-12 text-secondary">
              <caption className="sr-only">Import item results and failure reasons</caption>
              <thead>
                <tr>
                  <th className="p-2">Select</th>
                  <th className="p-2">Item / type</th>
                  <th className="p-2">Status</th>
                  <th className="p-2">Failure reason</th>
                </tr>
              </thead>
              <tbody>
                {detail?.results.map((item) => (
                  <tr key={item.id} className="border-t border-subtle align-top">
                    <td className="p-2">
                      <input
                        type="checkbox"
                        aria-label={`Retry ${item.title}`}
                        disabled={active || busy || (!selected.has(item.id) && selected.size >= 500)}
                        checked={selected.has(item.id)}
                        onChange={(event) =>
                          setSelected((current) => {
                            const next = new Set(current);
                            if (event.target.checked) next.add(item.id);
                            else next.delete(item.id);
                            return next;
                          })
                        }
                      />
                    </td>
                    <td className="max-w-48 p-2 break-words">
                      {item.title}
                      <br />
                      <span className="text-tertiary">
                        {item.category} · {item.remote_id}
                      </span>
                    </td>
                    <td className="p-2">{item.status}</td>
                    <td className="max-w-72 p-2 break-words">
                      {item.error_message}
                      {item.warning && <p className="text-tertiary">{item.warning}</p>}
                      {item.error_code && <span className="block text-tertiary">{item.error_code}</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="flex items-center justify-between gap-2 text-12 text-tertiary">
            <span>
              {detail?.count ?? 0} items · Run {run.id}
            </span>
            <div className="flex gap-2">
              <Button
                variant="secondary"
                size="sm"
                onClick={() => {
                  void refreshDetail();
                  void refreshRuns();
                }}
              >
                Refresh
              </Button>
              <Button
                variant="secondary"
                size="sm"
                disabled={!offset}
                onClick={() => setOffset(Math.max(0, offset - 100))}
              >
                Previous
              </Button>
              <Button
                variant="secondary"
                size="sm"
                disabled={detail?.next_offset == null}
                onClick={() => setOffset(detail?.next_offset ?? offset)}
              >
                Next
              </Button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
