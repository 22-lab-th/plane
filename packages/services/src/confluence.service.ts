import { isAxiosError } from "axios";
import { API_BASE_URL } from "@plane/constants";
import { APIService } from "./api.service";

export type ConfluenceConfig = {
  enabled: boolean;
  site_url: string;
  email: string;
  cloud_id: string;
  token_configured: boolean;
};
export type ImportMode = "all" | "changed" | "failed" | "selected";
export type ImportCounts = Record<"total" | "completed" | "failed" | "skipped" | "pending" | "running", number>;
export type ConfluenceRun = {
  id: string;
  source_id: string;
  space_id: string;
  space_name: string;
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
export type ConfluenceResult = {
  id: string;
  remote_id: string;
  kind: "page" | "attachment";
  category: string;
  title: string;
  version: number;
  status: "pending" | "running" | "completed" | "failed" | "skipped";
  error_code: string;
  error_message: string;
  page_id: string | null;
  file_id: string | null;
};
export type ConfluenceRunDetail = {
  run: ConfluenceRun;
  results: ConfluenceResult[];
  count: number;
  next_offset: number | null;
};
export type ConfluenceSpace = { id: string; name: string; key: string };
export const isConfluenceRunActive = (run: ConfluenceRun | null | undefined) =>
  !!run && ["queued", "discovering", "running"].includes(run.status);

export function confluenceErrorMessage(error: unknown): string {
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

export class ConfluenceService extends APIService {
  constructor() {
    super(API_BASE_URL);
  }
  async config(): Promise<ConfluenceConfig> {
    return (await this.get("/api/instances/confluence/")).data;
  }
  async saveConfig(
    data: Partial<Omit<ConfluenceConfig, "token_configured">> & { api_token?: string }
  ): Promise<ConfluenceConfig> {
    return (await this.patch("/api/instances/confluence/", data)).data;
  }
  async testConnection(): Promise<{ message: string }> {
    return (await this.post("/api/instances/confluence/test/")).data;
  }
  private path(workspace: string, project: string) {
    return `/api/workspaces/${encodeURIComponent(workspace)}/projects/${encodeURIComponent(project)}/confluence`;
  }
  async spaces(
    workspace: string,
    project: string,
    cursor?: string,
    search?: string
  ): Promise<{ results: ConfluenceSpace[]; next_cursor: string | null; site_url: string; count?: number }> {
    return (await this.get(`${this.path(workspace, project)}/spaces/`, { params: { cursor, search } })).data;
  }
  async runs(workspace: string, project: string): Promise<ConfluenceRun[]> {
    return (await this.get(`${this.path(workspace, project)}/runs/`)).data;
  }
  async detail(
    workspace: string,
    project: string,
    id: string,
    offset = 0,
    status?: string
  ): Promise<ConfluenceRunDetail> {
    return (
      await this.get(`${this.path(workspace, project)}/runs/${encodeURIComponent(id)}/`, { params: { offset, status } })
    ).data;
  }
  async start(
    workspace: string,
    project: string,
    data: {
      space_id?: string;
      source_id?: string;
      mode: ImportMode;
      item_ids?: string[];
      parent_id?: string | null;
      access?: number;
    }
  ): Promise<ConfluenceRun> {
    return (await this.post(`${this.path(workspace, project)}/runs/`, data)).data;
  }
}
