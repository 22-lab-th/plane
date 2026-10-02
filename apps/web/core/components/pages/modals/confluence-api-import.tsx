import { useEffect, useRef, useState } from "react";
import { Combobox } from "@headlessui/react";
import {
  AlertCircle,
  Check,
  CheckCircle2,
  ChevronDown,
  FileText,
  Image,
  Paperclip,
  RefreshCw,
  Search,
  Video,
} from "lucide-react";
import useSWR from "swr";
import { ConfluenceService, confluenceErrorMessage, isConfluenceRunActive } from "@plane/services";
import type { ConfluenceRun, ConfluenceSpace, ImportMode } from "@plane/services";
import { Button } from "@plane/propel/button";
import useDebounce from "@/hooks/use-debounce";

const service = new ConfluenceService();
const actionClass = "min-h-9 px-3 focus-visible:ring-2 focus-visible:ring-accent-strong focus-visible:ring-offset-2";
const inputClass =
  "min-h-10 w-full min-w-0 rounded-md border border-strong bg-surface-1 px-3 py-2 text-13 text-secondary focus:outline-none focus:ring-2 focus:ring-accent-strong";
const runLabels: Record<ConfluenceRun["status"], string> = {
  queued: "Queued",
  discovering: "Finding pages and files",
  running: "Importing",
  completed: "Completed",
  partial: "Needs attention",
  failed: "Import failed",
};
const typeLabels: Record<string, string> = { page: "Pages", image: "Images", video: "Videos", file: "Files" };
type Props = {
  workspaceSlug: string;
  projectId: string;
  parentId: string | null;
  access: number;
  onImported: () => Promise<unknown>;
};

export function ConfluenceAPIImport({ workspaceSlug, projectId, parentId, access, onImported }: Props) {
  const [spaceId, setSpaceId] = useState("");
  const [chosenSpace, setChosenSpace] = useState<ConfluenceSpace | null>(null);
  const chosenSpaceRef = useRef<ConfluenceSpace | null>(null);
  const [spaceQuery, setSpaceQuery] = useState("");
  const search = useDebounce(spaceQuery.trim(), 300);
  const currentSearch = useRef(search);
  currentSearch.current = spaceQuery.trim();
  const loadingMoreSpaces = useRef(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [spacesPageError, setSpacesPageError] = useState("");
  const [spaces, setSpaces] = useState<ConfluenceSpace[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [runId, setRunId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [offset, setOffset] = useState(0);
  const [failuresOnly, setFailuresOnly] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [confirmOverwrite, setConfirmOverwrite] = useState(false);
  const cancelOverwrite = useRef<HTMLButtonElement>(null);
  const overwriteTrigger = useRef<HTMLButtonElement>(null);
  const wasConfirmingOverwrite = useRef(false);
  useEffect(() => {
    if (confirmOverwrite) {
      cancelOverwrite.current?.focus();
    } else if (wasConfirmingOverwrite.current) {
      overwriteTrigger.current?.focus();
    }
    wasConfirmingOverwrite.current = confirmOverwrite;
  }, [confirmOverwrite]);
  const refreshedRuns = useRef(new Set<string>());
  const completedCallback = useRef(onImported);
  completedCallback.current = onImported;
  const {
    data: initialSpaces,
    error: spacesError,
    isLoading: spacesLoading,
    mutate: refreshSpaces,
  } = useSWR(["CONFLUENCE_SPACES", workspaceSlug, projectId, search], () =>
    service.spaces(workspaceSlug, projectId, undefined, search || undefined)
  );
  const searching = spaceQuery.trim() !== search || spacesLoading;
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
      setSpacesPageError("");
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
  const run = detail?.run ?? runs?.find((job) => job.id === runId);
  const active = isConfluenceRunActive(run);
  const selectedSpaceActive = runs?.some((job) => job.space_id === spaceId && isConfluenceRunActive(job));
  const actionsDisabled = busy || active || !detail;
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
    setConfirmOverwrite(false);
  };
  const start = async (mode: ImportMode, newSpace = false) => {
    if (busy || (newSpace ? !spaceId || selectedSpaceActive : !run || active || !detail)) return;
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
  const moreSpaces = async (retry = false) => {
    if (!cursor || busy || searching || loadingMoreSpaces.current || (!retry && spacesPageError)) return;
    const requestSearch = search;
    loadingMoreSpaces.current = true;
    setLoadingMore(true);
    setSpacesPageError("");
    try {
      const page = await service.spaces(workspaceSlug, projectId, cursor, search || undefined);
      if (currentSearch.current !== requestSearch) return;
      setSpaces((current) => [...new Map([...current, ...page.results].map((space) => [space.id, space])).values()]);
      setCursor(page.next_cursor);
    } catch (err) {
      if (currentSearch.current === requestSearch) setSpacesPageError(confluenceErrorMessage(err));
    } finally {
      loadingMoreSpaces.current = false;
      setLoadingMore(false);
    }
  };
  const processed = run ? run.counts.completed + run.counts.failed + run.counts.skipped : 0;
  const percent = run?.counts.total ? Math.min(100, Math.round((processed / run.counts.total) * 100)) : 0;
  return (
    <div className="space-y-5">
      <section aria-labelledby="confluence-space-label" className="space-y-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <label id="confluence-space-label" htmlFor="confluence-space" className="text-14 font-medium text-primary">
            Choose a Confluence space
          </label>
        </div>
        <div className="flex flex-col gap-2 sm:flex-row sm:items-start">
          <Combobox
            value={chosenSpace}
            by="id"
            onClose={() => {
              setSpaceQuery("");
              setSpaceId(chosenSpaceRef.current?.id ?? "");
            }}
            onChange={(space: ConfluenceSpace | null) => {
              chosenSpaceRef.current = space;
              setChosenSpace(space);
              setSpaceId(space?.id ?? "");
            }}
            disabled={busy}
            as="div"
            className="relative min-w-0 flex-1"
          >
            <div className="relative">
              <Search aria-hidden="true" className="pointer-events-none absolute top-3 left-3 size-4 text-tertiary" />
              <Combobox.Input
                id="confluence-space"
                className={`${inputClass} pr-10 pl-9`}
                displayValue={(space: ConfluenceSpace | null) => (space ? `${space.name} (${space.key})` : "")}
                placeholder="Search by space name or key"
                autoComplete="off"
                maxLength={200}
                onChange={(event) => {
                  setSpaceQuery(event.target.value);
                  setSpaceId("");
                  setSpacesPageError("");
                }}
              />
              <Combobox.Button
                className="absolute inset-y-0 right-0 flex w-10 items-center justify-center rounded-r-md text-tertiary focus-visible:ring-2 focus-visible:ring-accent-strong"
                aria-label="Show Confluence spaces"
              >
                <ChevronDown aria-hidden="true" className="size-4" />
              </Combobox.Button>
            </div>
            <Combobox.Options
              className="vertical-scrollbar absolute z-10 mt-1 max-h-60 w-full overflow-y-auto rounded-md border border-subtle bg-surface-1 p-1 shadow-raised-200 focus:outline-none"
              onScroll={(event) => {
                const list = event.currentTarget;
                if (list.scrollHeight - list.scrollTop - list.clientHeight < 48) void moreSpaces();
              }}
            >
              {searching ? (
                <div role="status" className="p-3 text-13 text-tertiary">
                  {spaceQuery.trim() ? "Searching all accessible spaces…" : "Loading spaces…"}
                </div>
              ) : spacesError ? (
                <div role="status" className="p-3 text-13 text-danger-primary">
                  Unable to load spaces. Use Try again below.
                </div>
              ) : !spaces.length ? (
                <div role="status" className="p-3 text-13 text-tertiary">
                  {search ? "No matching spaces. Try another name or key." : "No accessible spaces found."}
                </div>
              ) : (
                spaces.map((space) => (
                  <Combobox.Option
                    key={space.id}
                    value={space}
                    className={({ active: optionActive }) =>
                      `flex cursor-pointer items-start gap-2 rounded-md px-3 py-2.5 text-13 ${optionActive ? "bg-accent-subtle text-accent-primary" : "text-secondary"}`
                    }
                  >
                    {({ selected: isSelected }) => (
                      <>
                        <span className="min-w-0 flex-1 [overflow-wrap:anywhere]">
                          {space.name}
                          <span className="mt-0.5 block text-12 text-tertiary">{space.key}</span>
                        </span>
                        {isSelected && <Check aria-hidden="true" className="mt-0.5 size-4 shrink-0" />}
                      </>
                    )}
                  </Combobox.Option>
                ))
              )}
              {loadingMore && (
                <div role="status" className="p-3 text-12 text-tertiary">
                  Loading more spaces…
                </div>
              )}
              {spacesPageError && (
                <div role="alert" className="space-y-2 p-3 text-12 text-danger-primary">
                  <p>{spacesPageError}</p>
                  <Button
                    variant="secondary"
                    size="xl"
                    className={actionClass}
                    disabled={loadingMore}
                    onClick={() => void moreSpaces(true)}
                  >
                    Try loading more again
                  </Button>
                </div>
              )}
            </Combobox.Options>
          </Combobox>
          <Button
            variant="primary"
            size="xl"
            className={`${actionClass} min-h-10 shrink-0`}
            disabled={!spaceId || busy || selectedSpaceActive}
            loading={busy}
            onClick={() => void start("changed", true)}
          >
            {busy ? "Please wait…" : "Import space"}
          </Button>
        </div>
        <p className="text-12 text-tertiary">
          Search all accessible spaces by name or key. More results load as you scroll.
        </p>
        {selectedSpaceActive && (
          <p role="status" className="text-12 text-tertiary">
            This space is already being imported. Follow its progress below.
          </p>
        )}
        {spacesError ? (
          <div role="alert" className="space-y-2 rounded-md bg-danger-subtle p-3 text-13 text-danger-primary">
            <p>{confluenceErrorMessage(spacesError)}</p>
            <p className="text-12">Connection settings are managed in God Mode → Confluence.</p>
            <Button variant="secondary" size="xl" className={actionClass} onClick={() => void refreshSpaces()}>
              Try again
            </Button>
          </div>
        ) : initialSpaces && !spaces.length && !search ? (
          <p role="status" className="text-13 text-tertiary">
            No accessible spaces found. Check the connected account’s Confluence permissions.
          </p>
        ) : (
          <p className="text-12 text-tertiary">
            Pages go into a space folder. Images, videos and attachments go into Project Files, visible to project
            members.
          </p>
        )}
      </section>
      {!!runs?.length && (
        <label className="flex flex-col gap-2 border-t border-subtle pt-4 text-13 text-secondary sm:flex-row sm:items-center sm:gap-4">
          <span className="shrink-0 font-medium">Import history</span>
          <select
            value={runId}
            onChange={(event) => chooseRun(event.target.value)}
            className={inputClass}
            disabled={busy}
          >
            {runs.map((job) => (
              <option value={job.id} key={job.id}>
                {job.space_name} — {runLabels[job.status]} — {new Date(job.created_at).toLocaleString()}
              </option>
            ))}
          </select>
        </label>
      )}
      {(error || detailError || runsError) && (
        <p
          role="alert"
          className="rounded-md bg-danger-subtle p-3 text-13 [overflow-wrap:anywhere] text-danger-primary"
        >
          {error || confluenceErrorMessage(detailError || runsError)}
        </p>
      )}
      {run && (
        <>
          <section aria-label="Import progress" className="space-y-4">
            <div role="status" aria-live="polite" className="space-y-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h4 className="min-w-0 text-16 font-semibold [overflow-wrap:anywhere] text-primary">
                  {run.space_name}
                </h4>
                <span
                  className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-12 font-medium ${active ? "bg-accent-subtle text-accent-primary" : run.status === "completed" ? "bg-success-subtle text-success-primary" : "bg-danger-subtle text-danger-primary"}`}
                >
                  {active ? (
                    <RefreshCw aria-hidden="true" className="size-3.5" />
                  ) : run.status === "completed" ? (
                    <CheckCircle2 aria-hidden="true" className="size-3.5" />
                  ) : (
                    <AlertCircle aria-hidden="true" className="size-3.5" />
                  )}
                  {runLabels[run.status]}
                </span>
              </div>
              <p className="text-13 text-tertiary">{run.phase}</p>
              <div className="grid grid-cols-2 gap-3 rounded-lg bg-surface-2 p-4 sm:grid-cols-5">
                {[
                  { label: "Total discovered", value: run.counts.total, color: "text-primary" },
                  { label: "Completed", value: run.counts.completed, color: "text-success-primary" },
                  {
                    label: "Failed",
                    value: run.counts.failed,
                    color: run.counts.failed ? "text-danger-primary" : "text-primary",
                  },
                  { label: "Unchanged / unselected", value: run.counts.skipped, color: "text-secondary" },
                  { label: "Remaining", value: run.counts.pending + run.counts.running, color: "text-primary" },
                ].map((stat) => (
                  <div key={stat.label}>
                    <p className={`text-24 font-semibold tabular-nums ${stat.color}`}>{stat.value}</p>
                    <p className="mt-1 text-12 text-tertiary">{stat.label}</p>
                  </div>
                ))}
              </div>
              {run.inventory_complete && run.counts.total > 0 && (
                <div className="space-y-1.5">
                  <div className="flex justify-between text-12 text-tertiary">
                    <span>
                      {processed} of {run.counts.total} processed
                    </span>
                    <span>{percent}%</span>
                  </div>
                  <div
                    role="progressbar"
                    aria-label="Items processed"
                    aria-valuemin={0}
                    aria-valuemax={run.counts.total}
                    aria-valuenow={processed}
                    className="flex h-1.5 overflow-hidden rounded-full bg-surface-2"
                  >
                    <div
                      className="bg-success-primary"
                      style={{ width: `${(run.counts.completed / run.counts.total) * 100}%` }}
                    />
                    <div
                      className="bg-danger-primary"
                      style={{ width: `${(run.counts.failed / run.counts.total) * 100}%` }}
                    />
                    <div
                      className="bg-accent-primary"
                      style={{ width: `${(run.counts.skipped / run.counts.total) * 100}%` }}
                    />
                  </div>
                </div>
              )}
              {!run.inventory_complete && (
                <p className="text-12 text-tertiary">
                  Still finding pages and files. These counts reflect items discovered so far.
                </p>
              )}
            </div>
            {run.error_message && (
              <p
                role="alert"
                className="rounded-md bg-danger-subtle p-3 text-13 [overflow-wrap:anywhere] text-danger-primary"
              >
                {run.error_message}
                <span className="mt-1 block text-12">{run.error_code}</span>
              </p>
            )}
            <details className="rounded-md border border-subtle">
              <summary className="cursor-pointer px-3 py-2.5 text-13 font-medium text-secondary focus-visible:outline-accent-strong">
                Pages and files by type
                <span className="font-normal ml-2 text-tertiary">
                  {Object.entries(run.types)
                    .map(([type, counts]) => `${counts.total} ${typeLabels[type]?.toLowerCase() ?? type}`)
                    .join(" · ")}
                </span>
              </summary>
              <div className="overflow-x-auto border-t border-subtle">
                <table className="w-full min-w-[560px] text-left text-12 text-secondary">
                  <caption className="sr-only">Progress by document and file type</caption>
                  <thead className="bg-surface-2">
                    <tr>
                      {["Type", "Total", "Completed", "Failed", "Unchanged / unselected", "Remaining"].map((label) => (
                        <th key={label} scope="col" className="px-3 py-2">
                          {label}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {Object.entries(run.types).map(([type, counts]) => (
                      <tr key={type} className="border-t border-subtle">
                        <th scope="row" className="px-3 py-2 font-medium">
                          {typeLabels[type] ?? type}
                        </th>
                        {(["total", "completed", "failed", "skipped", "remaining"] as const).map((key) => (
                          <td key={key} className="px-3 py-2 tabular-nums">
                            {key === "remaining" ? counts.pending + counts.running : counts[key]}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </details>
          </section>
          {active ? (
            <p className="rounded-md bg-accent-subtle p-3 text-13 text-secondary">
              Import is running in the background. You can close this window and return to check progress.
            </p>
          ) : (
            <div className="space-y-3">
              <div className="flex flex-col justify-between gap-3 sm:flex-row sm:items-center">
                <div className="text-13 text-secondary">
                  {run.counts.failed > 0 ? (
                    <>
                      <p className="font-medium">{run.counts.failed} items need attention</p>
                      <p className="mt-1 text-12 text-tertiary">
                        Review the reasons below, then retry the affected items.
                      </p>
                    </>
                  ) : (
                    <p>Ready to bring in new or updated content.</p>
                  )}
                </div>
                <div className="flex flex-wrap gap-2">
                  {run.counts.failed > 0 && (
                    <Button
                      variant="primary"
                      size="xl"
                      className={actionClass}
                      disabled={actionsDisabled}
                      onClick={() => void start("failed")}
                      prependIcon={<RefreshCw />}
                    >
                      Retry failed ({run.counts.failed})
                    </Button>
                  )}
                  <Button
                    variant={run.counts.failed > 0 ? "secondary" : "primary"}
                    size="xl"
                    className={actionClass}
                    disabled={actionsDisabled}
                    onClick={() => void start("changed")}
                  >
                    Sync updates
                  </Button>
                </div>
              </div>
              <details className="text-12 text-tertiary">
                <summary className="w-fit cursor-pointer py-2 font-medium text-secondary focus-visible:outline-accent-strong">
                  More import options
                </summary>
                <div className="space-y-3 rounded-md border border-subtle p-3">
                  <p>
                    Sync updates imports new or changed content using the same Plane pages and files. Retrying
                    attachments also rebuilds their pages.
                  </p>
                  <p>
                    Re-import all replaces every imported page and file, including edits made in Plane. Previous
                    versions are retained.
                  </p>
                  {confirmOverwrite ? (
                    <div role="alert" className="space-y-3 rounded-md bg-warning-subtle p-3">
                      <p className="font-medium text-secondary">Replace all imported content in {run.space_name}?</p>
                      <p>Edits made in Plane will be overwritten by content from Confluence.</p>
                      <div className="flex flex-wrap gap-2">
                        <Button
                          variant="error-fill"
                          size="xl"
                          className={actionClass}
                          disabled={actionsDisabled}
                          onClick={() => void start("all")}
                        >
                          Confirm overwrite
                        </Button>
                        <Button
                          variant="secondary"
                          size="xl"
                          className={actionClass}
                          ref={cancelOverwrite}
                          onClick={() => setConfirmOverwrite(false)}
                        >
                          Cancel
                        </Button>
                      </div>
                    </div>
                  ) : (
                    <Button
                      variant="error-outline"
                      size="xl"
                      className={actionClass}
                      disabled={actionsDisabled}
                      ref={overwriteTrigger}
                      onClick={() => setConfirmOverwrite(true)}
                    >
                      Re-import all…
                    </Button>
                  )}
                </div>
              </details>
            </div>
          )}
          <section aria-labelledby="confluence-results-heading" className="space-y-3 border-t border-subtle pt-5">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <h4 id="confluence-results-heading" className="text-14 font-medium text-primary">
                Import results
              </h4>
              <div
                role="group"
                aria-label="Filter import results"
                className="flex flex-wrap gap-1 rounded-md bg-surface-2 p-1"
              >
                <Button
                  variant={failuresOnly ? "ghost" : "secondary"}
                  size="xl"
                  className={actionClass}
                  aria-pressed={!failuresOnly}
                  onClick={() => {
                    setFailuresOnly(false);
                    setOffset(0);
                  }}
                >
                  All items
                </Button>
                <Button
                  variant={failuresOnly ? "secondary" : "ghost"}
                  size="xl"
                  className={actionClass}
                  aria-pressed={failuresOnly}
                  onClick={() => {
                    setFailuresOnly(true);
                    setOffset(0);
                  }}
                >
                  Failed ({run.counts.failed})
                </Button>
              </div>
            </div>
            {selected.size > 0 && (
              <div className="flex flex-wrap items-center justify-between gap-2 rounded-md bg-accent-subtle p-3">
                <span role="status" className="text-13 text-secondary">
                  {selected.size} selected
                </span>
                <div className="flex flex-wrap gap-2">
                  <Button variant="ghost" size="xl" className={actionClass} onClick={() => setSelected(new Set())}>
                    Clear selection
                  </Button>
                  <Button
                    variant="primary"
                    size="xl"
                    className={actionClass}
                    disabled={actionsDisabled}
                    onClick={() => void start("selected")}
                  >
                    Retry selected ({selected.size})
                  </Button>
                </div>
              </div>
            )}
            {selected.size >= 500 && (
              <p className="text-12 text-tertiary">You can retry up to 500 selected items at a time.</p>
            )}
            {!detail ? (
              <p role="status" className="py-6 text-13 text-tertiary">
                {detailError ? "Unable to load results. Use Refresh to try again." : "Loading results…"}
              </p>
            ) : !detail.results.length ? (
              <p role="status" className="rounded-md border border-subtle p-6 text-center text-13 text-tertiary">
                {failuresOnly
                  ? "No failed items in this import."
                  : active
                    ? "Pages and files will appear here as they are discovered."
                    : "No items in this import."}
              </p>
            ) : (
              <ul
                aria-label="Import item results and failure reasons"
                className="divide-y divide-subtle rounded-md border border-subtle"
              >
                {detail.results.map((item) => {
                  const ItemIcon =
                    item.kind === "page"
                      ? FileText
                      : item.category === "image"
                        ? Image
                        : item.category === "video"
                          ? Video
                          : Paperclip;
                  const mediaFailure = item.error_code === "media_dependency_failed";
                  return (
                    <li key={item.id} className="flex items-start gap-3 p-3 sm:p-4">
                      <input
                        type="checkbox"
                        aria-label={`Retry ${item.title}`}
                        className="accent-accent-primary mt-1 size-4 shrink-0 focus-visible:outline-accent-strong"
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
                      <ItemIcon aria-hidden="true" className="mt-0.5 hidden size-4 shrink-0 text-tertiary sm:block" />
                      <div className="min-w-0 flex-1 space-y-2">
                        <div className="flex flex-col items-start justify-between gap-2 sm:flex-row">
                          <div className="min-w-0">
                            <p className="text-13 font-medium [overflow-wrap:anywhere] text-secondary">{item.title}</p>
                            <p className="mt-1 text-12 text-tertiary">
                              {item.category} · Version {item.version}
                            </p>
                          </div>
                          <span
                            className={`shrink-0 rounded-full px-2 py-0.5 text-12 ${item.status === "failed" ? "bg-danger-subtle text-danger-primary" : item.status === "completed" ? "bg-success-subtle text-success-primary" : "bg-surface-2 text-tertiary"}`}
                          >
                            {
                              (
                                {
                                  pending: "Pending",
                                  running: "Importing",
                                  completed: "Completed",
                                  failed: "Failed",
                                  skipped: "Unchanged / unselected",
                                } as const
                              )[item.status]
                            }
                          </span>
                        </div>
                        {item.error_message && (
                          <p className="text-13 [overflow-wrap:anywhere] text-danger-primary">
                            {mediaFailure
                              ? "Some images or files used by this page could not be imported. Retry the failed files, then retry this page."
                              : item.error_message}
                          </p>
                        )}
                        {item.error_code && (
                          <details className="text-12 [overflow-wrap:anywhere] text-tertiary">
                            <summary className="w-fit cursor-pointer py-1 focus-visible:outline-accent-strong">
                              Error details
                            </summary>
                            <p className="mt-1">{item.error_code}</p>
                            {mediaFailure && <p className="mt-1">{item.error_message}</p>}
                          </details>
                        )}
                      </div>
                    </li>
                  );
                })}
              </ul>
            )}
            <div className="flex flex-wrap items-center justify-between gap-3 text-12 text-tertiary">
              <span>
                {detail?.results.length
                  ? `Showing ${offset + 1}–${offset + detail.results.length} of ${detail.count} items`
                  : `${detail?.count ?? 0} items`}
              </span>
              <div className="flex flex-wrap gap-2">
                <Button
                  variant="ghost"
                  size="xl"
                  className={actionClass}
                  onClick={() => {
                    void refreshDetail();
                    void refreshRuns();
                  }}
                  prependIcon={<RefreshCw />}
                >
                  Refresh
                </Button>
                <Button
                  variant="secondary"
                  size="xl"
                  className={actionClass}
                  disabled={!offset || !detail}
                  onClick={() => setOffset(Math.max(0, offset - 100))}
                >
                  Previous
                </Button>
                <Button
                  variant="secondary"
                  size="xl"
                  className={actionClass}
                  disabled={!detail || detail.next_offset == null}
                  onClick={() => setOffset(detail?.next_offset ?? offset)}
                >
                  Next
                </Button>
              </div>
            </div>
          </section>
        </>
      )}
    </div>
  );
}
