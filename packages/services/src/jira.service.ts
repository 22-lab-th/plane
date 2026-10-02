import type { ImportMode, ImportCounts } from "./confluence.service";
import { isAxiosError } from "axios";
import { API_BASE_URL } from "@plane/constants";
import { APIService } from "./api.service";

export type JiraConfig = {
  enabled: boolean;
  site_url: string;
  email: string;
  cloud_id: string;
  token_configured: boolean;
};
export type JiraRun = {
  id: string;
  source_id: string;
  remote_project_id: string;
  project_name: string;
  project_key: string;
  mode: ImportMode;
  status: "queued" | "discovering" | "running" | "completed" | "partial" | "failed";
  phase: string;
  inventory_complete: boolean;
  counts: ImportCounts;
  types: Record<string, ImportCounts>;
  error_code: string;
  error_message: string;
  created_at: string;
  updated_at: string;
  finished_at: string | null;
};
export type JiraResult = {
  id: string;
  remote_id: string;
  kind: "issue" | "comment" | "attachment" | "sprint" | "inventory";
  category: string;
  title: string;
  revision: string;
  status: "pending" | "running" | "completed" | "failed" | "skipped";
  error_code: string;
  error_message: string;
  warning: string;
  issue_id: string | null;
  cycle_id: string | null;
  file_id: string | null;
};
export type JiraRunDetail = {
  run: JiraRun;
  results: JiraResult[];
  count: number;
  next_offset: number | null;
  user_mapping: Record<string, string>;
};
export type JiraProject = { id: string; name: string; key: string };
export const isJiraRunActive = (run: JiraRun | null | undefined) =>
  !!run && ["queued", "discovering", "running"].includes(run.status);

export function jiraErrorMessage(error: unknown): string {
  if (isAxiosError(error)) {
    const data: unknown = error.response?.data;
    if (data && typeof data === "object" && "error" in data && typeof data.error === "string") return data.error;
    if (data && typeof data === "object") {
      const messages = Object.entries(data).flatMap(([key, value]) => {
        const values: unknown[] = Array.isArray(value) ? value : [value];
        return values.filter((entry): entry is string => typeof entry === "string").map((entry) => `${key}: ${entry}`);
      });
      if (messages.length) return messages.join("; ").slice(0, 2000);
    }
  }
  return "Unable to complete the request. Check the connection and try again.";
}

export class JiraService extends APIService {
  constructor() {
    super(API_BASE_URL);
  }
  async config(): Promise<JiraConfig> {
    return (await this.get("/api/instances/jira/")).data;
  }
  async saveConfig(data: Partial<Omit<JiraConfig, "token_configured">> & { api_token?: string }): Promise<JiraConfig> {
    return (await this.patch("/api/instances/jira/", data)).data;
  }
  async testConnection(): Promise<{ message: string }> {
    return (await this.post("/api/instances/jira/test/")).data;
  }
  private path(workspace: string, project: string) {
    return `/api/workspaces/${encodeURIComponent(workspace)}/projects/${encodeURIComponent(project)}/jira`;
  }
  async projects(
    workspace: string,
    project: string,
    offset?: number
  ): Promise<{ results: JiraProject[]; next_offset: number | null; site_url: string }> {
    return (await this.get(`${this.path(workspace, project)}/projects/`, { params: { offset } })).data;
  }
  async members(workspace: string, project: string): Promise<{ id: string; name: string }[]> {
    return (await this.get(`${this.path(workspace, project)}/members/`)).data;
  }
  async runs(workspace: string, project: string): Promise<JiraRun[]> {
    return (await this.get(`${this.path(workspace, project)}/runs/`)).data;
  }
  async detail(workspace: string, project: string, id: string, offset = 0, status?: string): Promise<JiraRunDetail> {
    return (
      await this.get(`${this.path(workspace, project)}/runs/${encodeURIComponent(id)}/`, { params: { offset, status } })
    ).data;
  }
  async start(
    workspace: string,
    project: string,
    data: {
      remote_project_id?: string;
      source_id?: string;
      mode: ImportMode;
      item_ids?: string[];
      user_mapping?: Record<string, string>;
    }
  ): Promise<JiraRun> {
    return (await this.post(`${this.path(workspace, project)}/runs/`, data)).data;
  }
}
