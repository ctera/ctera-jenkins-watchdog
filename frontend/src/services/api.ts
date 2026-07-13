import type { components } from "../types/api.generated";

type Schemas = components["schemas"];

export type Scan = Schemas["V2ScanResponse"];
export type ScanPage = Schemas["V2ScanPage"];
export type Incident = Schemas["V2IncidentResponse"];
export type IncidentPage = Schemas["V2IncidentPage"];
export type IncidentDetail = Schemas["V2IncidentDetailResponse"];
export type Observation = Schemas["V2ObservationResponse"];
export type Occurrence = Schemas["V2OccurrenceResponse"];
export type Investigation = Schemas["V2InvestigationResponse"];
export type Action = Schemas["V2ActionResponse"];
export type ActionPage = Schemas["V2ActionPage"];
export type ActionDetail = Schemas["V2ActionDetailResponse"];
export type DeliveryAttempt = Schemas["V2DeliveryAttemptResponse"];

export interface ScanEventEnvelope {
  sequence: number;
  type: string;
  occurred_at: string;
  payload_version: number;
  payload: Record<string, unknown>;
}

export interface IncidentFilters {
  status?: "open" | "resolved" | "suppressed";
  severity?: "low" | "warning" | "critical";
  source_type?: "merge_request" | "infrastructure" | "unknown";
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

export function reinvestigateIncident(incidentId: string): Promise<Investigation> {
  return request<Investigation>(`/incidents/${encodeURIComponent(incidentId)}/reinvestigate`, { method: "POST" });
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

export async function chat(message: string, incidentId?: string): Promise<string> {
  const response = await request<Schemas["V2ChatResponse"]>("/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, incident_id: incidentId ?? null }),
  });
  return response.content;
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
      if (["succeeded", "failed", "cancelled"].includes(scan.status)) return;
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
