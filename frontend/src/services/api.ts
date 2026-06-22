export interface Investigation {
  finding_fingerprint: string;
  root_cause: string;
  evidence: string[];
  impact: string;
  suggested_fix: string;
  fix_location: string | null;
  confidence: string;
  tools_used: string[];
  raw_reasoning?: string;
}

export interface JiraIssueRef {
  key: string;
  url: string;
}

export interface Finding {
  severity: string;
  category: string;
  resource: string;
  symptom: string;
  context: Record<string, unknown>;
  fingerprint: string;
  status: string;
  first_seen: string | null;
  last_seen: string | null;
  investigation: Investigation | null;
  jira_issue: JiraIssueRef | null;
}

export interface FindingsResponse {
  last_scan: string | null;
  total_findings: number;
  findings: Finding[];
}

const BASE = "/api";

export async function fetchFindings(): Promise<FindingsResponse> {
  const res = await fetch(`${BASE}/findings`);
  if (!res.ok) throw new Error(`Failed to fetch findings: ${res.status}`);
  return res.json();
}

export async function deleteFinding(fingerprint: string): Promise<void> {
  const res = await fetch(`${BASE}/findings/${fingerprint}`, { method: "DELETE" });
  if (!res.ok) throw new Error(`Failed to dismiss finding: ${res.status}`);
}

export interface ScanEvent {
  type: "scan_started" | "detection_complete" | "investigation_plan" | "investigation_start" | "tool_call" | "reasoning" | "investigation_complete" | "investigation_error" | "scan_complete" | "scan_stopped" | "error";
  scan_id?: string;
  total_findings?: number;
  count?: number;
  index?: number;
  total?: number;
  resource?: string;
  symptom?: string;
  tool?: string;
  args?: Record<string, unknown>;
  content?: string;
  message?: string;
  root_cause?: string;
  confidence?: string;
  tools_used?: string[];
  error?: string;
  new_findings?: number;
  critical_findings?: number;
  investigations_performed?: number;
  duration_s?: number;
}

export async function* streamScan(
  investigateAll = false,
  signal?: AbortSignal
): AsyncGenerator<ScanEvent> {
  const res = await fetch(`${BASE}/scan`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ investigate_all: investigateAll }),
    signal,
  });

  if (!res.ok) throw new Error(`Scan failed: ${res.status}`);

  const reader = res.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";

    for (const line of lines) {
      if (line.startsWith("data: ")) {
        try {
          yield JSON.parse(line.slice(6)) as ScanEvent;
        } catch {
          // skip malformed
        }
      }
    }
  }
}

export async function stopScan(): Promise<{ status: string }> {
  const res = await fetch(`${BASE}/scan/stop`, { method: "POST" });
  if (!res.ok) throw new Error(`Failed to stop scan: ${res.status}`);
  return res.json();
}

export interface ChatEvent {
  type: "token" | "tool_start" | "tool_result" | "done" | "error";
  content?: string;
  tool_name?: string;
  tool_args?: Record<string, unknown>;
  success?: boolean;
  session_id?: string;
}

export async function* streamChat(
  message: string,
  sessionId: string | null,
  signal?: AbortSignal
): AsyncGenerator<ChatEvent> {
  const res = await fetch(`${BASE}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, session_id: sessionId }),
    signal,
  });

  if (!res.ok) {
    throw new Error(`Chat failed: ${res.status}`);
  }

  const reader = res.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";

    for (const line of lines) {
      if (line.startsWith("data: ")) {
        try {
          const data = JSON.parse(line.slice(6));
          yield data as ChatEvent;
        } catch {
          // skip malformed lines
        }
      }
    }
  }
}
