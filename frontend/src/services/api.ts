import type { components } from "../types/api.generated";

type Schemas = components["schemas"];

export type Scan = Schemas["V2ScanResponse"];
export type CheckExecution = Schemas["V2CheckExecutionResponse"];
export type Overview = Schemas["V2OverviewResponse"];
export type ScanPage = Schemas["V2ScanPage"];
export type Incident = Schemas["V2IncidentResponse"];
export type IncidentPage = Schemas["V2IncidentPage"];
export type IncidentDetail = Schemas["V2IncidentDetailResponse"];
export type Observation = Schemas["V2ObservationResponse"];
export type Occurrence = Schemas["V2OccurrenceResponse"];
export type Investigation = Schemas["V2InvestigationResponse"];
export type InvestigationRequest = Schemas["V2InvestigationRequestResponse"];
export type Action = Schemas["V2ActionResponse"];
export type ActionPage = Schemas["V2ActionPage"];
export type ActionDetail = Schemas["V2ActionDetailResponse"];
export type DeliveryAttempt = Schemas["V2DeliveryAttemptResponse"];
export type ChatResponse = Schemas["V2ChatResponse"];
export type JenkinsWorkspace = Schemas["V2JenkinsWorkspaceResponse"];
export type JenkinsBuild = Schemas["V2JenkinsBuildResponse"];
export type JenkinsBuildDetail = Schemas["V2JenkinsBuildDetailResponse"];
export type JenkinsFailurePage = Schemas["V2JenkinsFailurePage"];
export type JenkinsExecution = Schemas["V2LogicalExecutionResponse"];
export type JenkinsFailurePattern = Schemas["V2FailurePatternResponse"];
export type JenkinsJobFamily = Schemas["V2JobFamilyResponse"];
export type JenkinsMultibranchFamily = Schemas["V2MultibranchFamilyResponse"];

export interface JenkinsFailureReportBuild {
  id: string; build_id: string; job_name: string; build_number: number; result: string; url: string;
  started_at: string; duration_ms: number; status: string; source: Record<string, unknown>;
  investigation_request_id?: string | null; investigation_status?: string | null;
  assessment?: Record<string, unknown> | null; error_summary?: string | null;
}

export interface JenkinsFailureReport {
  id: string; scan_id?: string | null; mode: string; status: string; window_started_at: string; window_ended_at: string;
  collected_at?: string | null; jobs_discovered: number; failures_found: number;
  coverage_exceptions: Array<Record<string, unknown>>; budget_reset_at?: string | null;
  created_at: string; updated_at: string; completed_at?: string | null; error_summary?: string | null;
  total_builds: number; offset: number; limit: number; counts: Record<string, number>; builds: JenkinsFailureReportBuild[];
}

export interface ScanEventEnvelope {
  sequence: number;
  type: string;
  occurred_at: string;
  payload_version: number;
  payload: Record<string, unknown>;
}

export interface ChatStreamEvent {
  event: "tool_call" | "tool_result" | "reasoning" | "message" | "error" | string;
  data: Record<string, unknown>;
}

export interface IncidentFilters {
  status?: "open" | "resolved" | "suppressed";
  severity?: "low" | "warning" | "critical";
  source_type?: "merge_request" | "repository" | "pipeline" | "multiple" | "infrastructure" | "unknown";
}

export interface ActionFilters {
  status?: "pending" | "running" | "retry_scheduled" | "succeeded" | "permanently_failed";
  action_type?: "email" | "jira_create" | "jira_update" | "github_comment" | "gitlab_comment";
  incident_id?: string;
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly detail: unknown,
  ) {
    super(message);
  }
}

const BASE = "/api/v2";

export function getOverview(signal?: AbortSignal): Promise<Overview> {
  return request<Overview>("/overview", { signal });
}

export function getJenkinsWorkspace(
  windowHours = 168,
  limit = 50,
  signal?: AbortSignal,
): Promise<JenkinsWorkspace> {
  return request<JenkinsWorkspace>(`/jenkins${query({ window_hours: windowHours, limit })}`, { signal });
}

export function listJenkinsFailures(
  windowHours = 168,
  filters: { view?: "all" | "new"; result?: "FAILURE" | "UNSTABLE" | "ABORTED"; job?: string } = {},
  limit = 100,
  cursor?: string,
  signal?: AbortSignal,
): Promise<JenkinsFailurePage> {
  return request<JenkinsFailurePage>(
    `/jenkins/failures${query({ window_hours: windowHours, limit, cursor, ...filters })}`,
    { signal },
  );
}

export function getJenkinsBuild(buildId: string, scanId?: string): Promise<JenkinsBuildDetail> {
  return request<JenkinsBuildDetail>(`/jenkins/builds/${encodeURIComponent(buildId)}${query({ scan_id: scanId })}`);
}

export function analyzeJenkinsBuild(
  buildId: string,
  mode: "regular" | "deep",
): Promise<InvestigationRequest> {
  return request<InvestigationRequest>(`/jenkins/builds/${encodeURIComponent(buildId)}/analyze`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mode }),
  });
}

export function getScanJenkinsFailures(scanId: string, offset = 0, limit = 50, filters: { status?: string; job?: string } = {}): Promise<JenkinsFailureReport> {
  return request<JenkinsFailureReport>(`/scans/${encodeURIComponent(scanId)}/jenkins-failures${query({ offset, limit, ...filters })}`);
}

export async function createScan(mode: "regular" | "deep", categories: string[]): Promise<Scan> {
  return request<Scan>("/scans", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mode, categories: categories.length ? categories : null }),
  });
}

export function listScans(cursor?: string, limit = 25): Promise<ScanPage> {
  return request<ScanPage>(`/scans${query({ cursor, limit })}`);
}

export function getScan(scanId: string): Promise<Scan> {
  return request<Scan>(`/scans/${encodeURIComponent(scanId)}`);
}

export function cancelScan(scanId: string): Promise<Schemas["V2CancelResponse"]> {
  return request(`/scans/${encodeURIComponent(scanId)}/cancel`, { method: "POST" });
}

export function listIncidents(filters: IncidentFilters = {}, cursor?: string, limit = 25): Promise<IncidentPage> {
  return request<IncidentPage>(`/incidents${query({ ...filters, cursor, limit })}`);
}

export function getIncident(incidentId: string): Promise<IncidentDetail> {
  return request<IncidentDetail>(`/incidents/${encodeURIComponent(incidentId)}`);
}

export function suppressIncident(incidentId: string, reason: string): Promise<Incident> {
  return request<Incident>(`/incidents/${encodeURIComponent(incidentId)}/suppress`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ reason }),
  });
}

export function unsuppressIncident(incidentId: string): Promise<Incident> {
  return request<Incident>(`/incidents/${encodeURIComponent(incidentId)}/unsuppress`, { method: "POST" });
}

export function reinvestigateIncident(incidentId: string): Promise<InvestigationRequest> {
  return request<InvestigationRequest>(`/incidents/${encodeURIComponent(incidentId)}/reinvestigate`, { method: "POST" });
}

export function listActions(filters: ActionFilters = {}, cursor?: string, limit = 25): Promise<ActionPage> {
  return request<ActionPage>(`/actions${query({ ...filters, cursor, limit })}`);
}

export function getAction(actionId: string): Promise<ActionDetail> {
  return request<ActionDetail>(`/actions/${encodeURIComponent(actionId)}`);
}

export function retryAction(actionId: string): Promise<Action> {
  return request<Action>(`/actions/${encodeURIComponent(actionId)}/retry`, { method: "POST" });
}

export function chat(message: string, incidentId?: string): Promise<ChatResponse> {
  return request<ChatResponse>("/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, incident_id: incidentId ?? null }),
  });
}

export async function* streamChat(
  message: string,
  incidentId: string | undefined,
  history: Array<{ role: "user" | "assistant"; content: string }>,
  signal: AbortSignal,
): AsyncGenerator<ChatStreamEvent> {
  const response = await fetch(`${BASE}/chat/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify({ message, incident_id: incidentId ?? null, history }),
    signal,
  });
  if (!response.ok || !response.body) {
    throw await apiError(response, "Failed to start live agent chat");
  }
  for await (const event of parseNamedSse(response.body, signal)) yield event;
}

export async function* streamScanEvents(
  scanId: string,
  signal: AbortSignal,
  resume = true,
): AsyncGenerator<ScanEventEnvelope> {
  let lastSequence = resume ? readLastSequence(scanId) : 0;
  while (!signal.aborted) {
    try {
      const headers: HeadersInit = { Accept: "text/event-stream" };
      if (lastSequence > 0) headers["Last-Event-ID"] = String(lastSequence);
      const response = await fetch(`${BASE}/scans/${encodeURIComponent(scanId)}/events`, { headers, signal });
      if (!response.ok || !response.body) {
        throw await apiError(response, "Failed to connect to scan events");
      }
      for await (const event of parseSse(response.body, signal)) {
        if (event.sequence > lastSequence) {
          lastSequence = event.sequence;
          writeLastSequence(scanId, lastSequence);
        }
        yield event;
      }
      if (signal.aborted) return;
      const scan = await getScan(scanId);
      if (["succeeded", "failed", "cancelled"].includes(scan.status) && !scan.analysis?.active_count) return;
    } catch (error) {
      if (signal.aborted) return;
      if (error instanceof ApiError && [404, 422].includes(error.status)) throw error;
    }
    await pause(750, signal);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, init);
  if (!response.ok) throw await apiError(response, `Request failed with status ${response.status}`);
  return response.json() as Promise<T>;
}

async function apiError(response: Response, fallback: string): Promise<ApiError> {
  let detail: unknown = null;
  try {
    detail = await response.json();
  } catch {
    detail = null;
  }
  const message = extractErrorMessage(detail) ?? fallback;
  return new ApiError(message, response.status, detail);
}

function extractErrorMessage(detail: unknown): string | null {
  if (!detail || typeof detail !== "object") return null;
  const value = "detail" in detail ? detail.detail : detail;
  if (typeof value === "string") return value;
  if (value && typeof value === "object" && "code" in value && typeof value.code === "string") {
    return value.code.replaceAll("_", " ");
  }
  return null;
}

function query(values: Record<string, string | number | undefined>): string {
  const params = new URLSearchParams();
  Object.entries(values).forEach(([name, value]) => {
    if (value !== undefined && value !== "") params.set(name, String(value));
  });
  const encoded = params.toString();
  return encoded ? `?${encoded}` : "";
}

async function* parseSse(stream: ReadableStream<Uint8Array>, signal: AbortSignal): AsyncGenerator<ScanEventEnvelope> {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (!signal.aborted) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true }).replaceAll("\r\n", "\n");
      let boundary = buffer.indexOf("\n\n");
      while (boundary >= 0) {
        const block = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const data = block
          .split("\n")
          .filter((line) => line.startsWith("data:"))
          .map((line) => line.slice(5).trimStart())
          .join("\n");
        if (data) yield JSON.parse(data) as ScanEventEnvelope;
        boundary = buffer.indexOf("\n\n");
      }
    }
  } finally {
    reader.releaseLock();
  }
}

async function* parseNamedSse(
  stream: ReadableStream<Uint8Array>,
  signal: AbortSignal,
): AsyncGenerator<ChatStreamEvent> {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (!signal.aborted) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true }).replaceAll("\r\n", "\n");
      let boundary = buffer.indexOf("\n\n");
      while (boundary >= 0) {
        const block = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        let event = "message";
        const data: string[] = [];
        block.split("\n").forEach((line) => {
          if (line.startsWith("event:")) event = line.slice(6).trim();
          if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
        });
        if (data.length) yield { event, data: JSON.parse(data.join("\n")) as Record<string, unknown> };
        boundary = buffer.indexOf("\n\n");
      }
    }
  } finally {
    reader.releaseLock();
  }
}

function readLastSequence(scanId: string): number {
  try {
    return Number(sessionStorage.getItem(`watchdog.scan.${scanId}.sequence`) ?? 0) || 0;
  } catch {
    return 0;
  }
}

function writeLastSequence(scanId: string, sequence: number): void {
  try {
    sessionStorage.setItem(`watchdog.scan.${scanId}.sequence`, String(sequence));
  } catch {
    // Session storage can be unavailable in hardened browser contexts.
  }
}

function pause(milliseconds: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(resolve, milliseconds);
    signal.addEventListener(
      "abort",
      () => {
        window.clearTimeout(timer);
        reject(new DOMException("Aborted", "AbortError"));
      },
      { once: true },
    );
  });
}
