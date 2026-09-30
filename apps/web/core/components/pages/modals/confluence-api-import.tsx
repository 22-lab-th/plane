import { useEffect, useRef, useState } from "react";
import useSWR from "swr";
import { ConfluenceService, confluenceErrorMessage, isConfluenceRunActive } from "@plane/services";
import type { ConfluenceSpace, ImportMode } from "@plane/services";
import { Button } from "@plane/propel/button";

const service = new ConfluenceService();
type Props = {
  workspaceSlug: string;
  projectId: string;
  parentId: string | null;
  access: number;
  onImported: () => Promise<unknown>;
};

export function ConfluenceAPIImport({ workspaceSlug, projectId, parentId, access, onImported }: Props) {
  const [spaceId, setSpaceId] = useState("");
  const [spaces, setSpaces] = useState<ConfluenceSpace[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [runId, setRunId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [offset, setOffset] = useState(0);
  const [failuresOnly, setFailuresOnly] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const refreshedRuns = useRef(new Set<string>());
  const completedCallback = useRef(onImported);
  completedCallback.current = onImported;
  const { data: initialSpaces, error: spacesError } = useSWR(["CONFLUENCE_SPACES", workspaceSlug, projectId], () =>
    service.spaces(workspaceSlug, projectId)
  );
  const {
    data: runs,
    error: runsError,
    mutate: refreshRuns,
  } = useSWR(["CONFLUENCE_RUNS", workspaceSlug, projectId], () => service.runs(workspaceSlug, projectId), {
    refreshInterval: (values) => (values?.some(isConfluenceRunActive) ? 4000 : 0),
  });
  useEffect(() => {
    if (initialSpaces) {
      setSpaces(initialSpaces.results);
      setCursor(initialSpaces.next_cursor);
    }
  }, [initialSpaces]);
  useEffect(() => {
    if (!runId && runs?.length) setRunId(runs[0].id);
  }, [runs, runId]);
  const {
    data: detail,
    error: detailError,
    mutate: refreshDetail,
  } = useSWR(
    runId ? ["CONFLUENCE_RUN", workspaceSlug, projectId, runId, offset, failuresOnly] : null,
    () => service.detail(workspaceSlug, projectId, runId, offset, failuresOnly ? "failed" : undefined),
    { refreshInterval: (value) => (!value || isConfluenceRunActive(value.run) ? 3000 : 0) }
  );
  const run = detail?.run;
  const active = isConfluenceRunActive(run);
  useEffect(() => {
    if (run && !isConfluenceRunActive(run) && !refreshedRuns.current.has(run.id)) {
      refreshedRuns.current.add(run.id);
      void completedCallback.current().catch(() => undefined);
      void refreshRuns();
    }
  }, [run, refreshRuns]);
  const chooseRun = (id: string) => {
    setRunId(id);
    setOffset(0);
    setSelected(new Set());
    setFailuresOnly(false);
    setError("");
  };
  const start = async (mode: ImportMode, newSpace = false) => {
    if (busy || (!newSpace && (!run || active))) return;
    setBusy(true);
    setError("");
    try {
      const created = await service.start(workspaceSlug, projectId, {
        ...(newSpace ? { space_id: spaceId, parent_id: parentId, access } : { source_id: run?.source_id }),
        mode,
        ...(mode === "selected" ? { item_ids: [...selected] } : {}),
      });
      chooseRun(created.id);
      await refreshRuns();
    } catch (err) {
      setError(confluenceErrorMessage(err));
      await refreshRuns();
    } finally {
      setBusy(false);
    }
  };
  const moreSpaces = async () => {
    if (!cursor) return;
    setBusy(true);
    setError("");
    try {
      const page = await service.spaces(workspaceSlug, projectId, cursor);
      setSpaces((current) => [...new Map([...current, ...page.results].map((space) => [space.id, space])).values()]);
      setCursor(page.next_cursor);
    } catch (err) {
      setError(confluenceErrorMessage(err));
    } finally {
      setBusy(false);
    }
  };
  const inputClass = "rounded border border-subtle bg-surface-1 px-2 py-2 text-13 text-secondary";
  return (
    <div className="space-y-4">
      <p className="text-13 text-tertiary">
        Connect using credentials saved in God Mode → Confluence. Pages and attachments are imported in the background;
        you can close this window and return to check progress.
      </p>
      <p className="text-12 text-tertiary">
        Imported attachments follow Project Files access and are visible to project members.
      </p>
      {spacesError && (
        <p role="alert" className="text-13 text-danger-primary">
          {confluenceErrorMessage(spacesError)}
        </p>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <label className="sr-only" htmlFor="confluence-space">
          Confluence space
        </label>
        <select
          id="confluence-space"
          value={spaceId}
          onChange={(event) => setSpaceId(event.target.value)}
          className={`min-w-0 flex-1 ${inputClass}`}
          disabled={busy}
        >
          <option value="">Choose a Confluence space</option>
          {spaces.map((space) => (
            <option key={space.id} value={space.id}>
              {space.name} ({space.key})
            </option>
          ))}
        </select>
        {cursor && (
          <Button variant="secondary" size="sm" onClick={() => void moreSpaces()} disabled={busy}>
            More spaces
          </Button>
        )}
        <Button
          variant="primary"
          size="sm"
          disabled={!spaceId || busy}
          loading={busy}
          onClick={() => void start("changed", true)}
        >
          Import / sync space
        </Button>
      </div>
      {runsError && (
        <p role="alert" className="text-13 text-danger-primary">
          {confluenceErrorMessage(runsError)}
        </p>
      )}
      {!!runs?.length && (
        <label className="block space-y-1 text-13 text-secondary">
          <span>Import history</span>
          <select value={runId} onChange={(event) => chooseRun(event.target.value)} className={`w-full ${inputClass}`}>
            {runs.map((job) => (
              <option value={job.id} key={job.id}>
                {job.space_name} — {job.status} — {new Date(job.created_at).toLocaleString()}
              </option>
            ))}
          </select>
        </label>
      )}
      {(error || detailError) && (
        <p role="alert" className="text-13 text-danger-primary">
          {error || confluenceErrorMessage(detailError)}
        </p>
      )}
      {run && (
        <>
          <div aria-live="polite" role="status" className="space-y-1 text-13 text-secondary">
            <p className="font-medium">
              {run.space_name}: {run.status}
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
              <caption className="sr-only">Progress by document and file type</caption>
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
            Sync and overwrite use the same Plane page/file IDs. Overwrite replaces imported content, including local
            edits, and keeps previous page/file versions. Retrying attachments also rebuilds their owner pages.
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
                        {item.category} · v{item.version}
                      </span>
                    </td>
                    <td className="p-2">{item.status}</td>
                    <td className="max-w-72 p-2 break-words">
                      {item.error_message}
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
