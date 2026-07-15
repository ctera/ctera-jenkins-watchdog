import {
  Box,
  Chip,
  CircularProgress,
  IconButton,
  Link,
  Stack,
  TextField,
  Tooltip,
  Typography,
} from "@mui/material";
import CheckCircleOutlineIcon from "@mui/icons-material/CheckCircleOutline";
import ErrorOutlineIcon from "@mui/icons-material/ErrorOutline";
import SendIcon from "@mui/icons-material/Send";
import StopCircleOutlinedIcon from "@mui/icons-material/StopCircleOutlined";
import TerminalIcon from "@mui/icons-material/Terminal";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { streamChat } from "../services/api";
import { titleCase } from "../utils/format";
import MarkdownContent from "./MarkdownContent";

interface Message {
  id: number;
  role: "user" | "assistant" | "activity" | "error";
  content: string;
  references?: Array<Record<string, string>>;
  asOf?: string | null;
  coverage?: string;
  tool?: string;
  status?: "running" | "succeeded" | "failed";
}

export default function ChatPanel({ incidentId }: { incidentId?: string }) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [message, setMessage] = useState("");
  const [sending, setSending] = useState(false);
  const [phase, setPhase] = useState("");
  const controller = useRef<AbortController | null>(null);
  const sequence = useRef(0);

  useEffect(() => () => controller.current?.abort(), []);

  async function send() {
    const content = message.trim();
    if (!content || sending) return;
    const id = nextId();
    const history = messages
      .filter((item): item is Message & { role: "user" | "assistant" } => ["user", "assistant"].includes(item.role))
      .slice(-12)
      .map((item) => ({ role: item.role, content: item.content }));
    setMessages((current) => [...current, { id, role: "user", content }]);
    setMessage("");
    setSending(true);
    setPhase("Selecting live evidence");
    controller.current = new AbortController();
    try {
      for await (const event of streamChat(content, incidentId, history, controller.current.signal)) {
        if (event.event === "tool_call") {
          const tool = stringValue(event.data.tool);
          const detail = activityDetail(event.data.arguments);
          setMessages((current) => [
            ...current,
            { id: nextId(), role: "activity", tool, status: "running", content: detail },
          ]);
          setPhase("Gathering live evidence");
        } else if (event.event === "tool_result") {
          const tool = stringValue(event.data.tool);
          const ok = Boolean(event.data.ok);
          setMessages((current) => completeActivity(current, tool, ok));
        } else if (event.event === "reasoning") {
          setPhase("Synthesizing evidence");
        } else if (event.event === "message") {
          setMessages((current) => [
            ...current,
            {
              id: nextId(),
              role: "assistant",
              content: stringValue(event.data.content),
              references: arrayRecords(event.data.references) as Array<Record<string, string>>,
              asOf: nullableString(event.data.as_of),
              coverage: stringValue(event.data.coverage_status),
            },
          ]);
        } else if (event.event === "error") {
          throw new Error(titleCase(stringValue(event.data.code) || "Reasoning unavailable"));
        }
      }
    } catch (error) {
      if (!(error instanceof DOMException && error.name === "AbortError")) {
        setMessages((current) => [
          ...current,
          { id: nextId(), role: "error", content: error instanceof Error ? error.message : "Reasoning failed" },
        ]);
      }
    } finally {
      controller.current = null;
      setSending(false);
      setPhase("");
    }
  }

  function nextId(): number {
    sequence.current += 1;
    return sequence.current;
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    void send();
  }

  return (
    <Stack sx={{ minHeight: 420 }}>
      <Box sx={{ flex: 1, py: 1, display: "flex", flexDirection: "column", gap: 1.25 }} aria-live="polite">
        {messages.length === 0 && (
          <Box sx={{ py: 8, textAlign: "center" }}>
            <Typography color="text.secondary">No conversation yet</Typography>
          </Box>
        )}
        {messages.map((item) => item.role === "activity" ? (
          <Stack key={item.id} direction="row" gap={1} alignItems="center" sx={{ py: 0.5, color: "text.secondary" }}>
            {item.status === "running" ? <CircularProgress size={15} /> : item.status === "succeeded" ? <CheckCircleOutlineIcon color="success" fontSize="small" /> : <ErrorOutlineIcon color="error" fontSize="small" />}
            <TerminalIcon fontSize="small" />
            <Typography variant="body2" fontWeight={650}>{toolLabel(item.tool)}</Typography>
            {item.content && <Typography variant="caption" sx={{ overflowWrap: "anywhere" }}>{item.content}</Typography>}
          </Stack>
        ) : (
          <Box
            key={item.id}
            sx={{
              alignSelf: item.role === "user" ? "flex-end" : "flex-start",
              maxWidth: item.role === "user" ? { xs: "92%", md: "75%" } : { xs: "100%", md: "90%" },
              px: 1.75,
              py: 1.25,
              borderRadius: 1.5,
              bgcolor: item.role === "user" ? "#f0f4ff" : item.role === "error" ? "#fee4e2" : "#f1f3f4",
              border: "1px solid",
              borderColor: item.role === "error" ? "#fecdca" : "divider",
            }}
          >
            {item.role === "assistant" ? (
              <MarkdownContent content={item.content} />
            ) : (
              <Typography variant="body2" sx={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
                {item.content}
              </Typography>
            )}
            {item.role === "assistant" && item.references && item.references.length > 0 && (
              <Stack direction="row" gap={0.75} flexWrap="wrap" sx={{ mt: 1.25 }}>
                {item.references.slice(0, 6).map((reference) => (
                  <Chip
                    component={Link}
                    clickable
                    href={reference.kind === "scan" ? `/scans/${reference.id}` : `/incidents/${reference.id}`}
                    key={`${reference.kind}-${reference.id}`}
                    label={reference.label}
                    size="small"
                    variant="outlined"
                  />
                ))}
              </Stack>
            )}
            {item.role === "assistant" && item.asOf && (
              <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 1 }}>
                As of {new Date(item.asOf).toLocaleString()} · {item.coverage ?? "unknown"} coverage
              </Typography>
            )}
          </Box>
        ))}
        {sending && <Typography variant="body2" color="text.secondary">{phase}</Typography>}
      </Box>
      <Box component="form" onSubmit={submit} sx={{ borderTop: "1px solid", borderColor: "divider", pt: 2 }}>
        <Stack direction="row" gap={1} alignItems="flex-end">
          <TextField
            fullWidth
            multiline
            maxRows={5}
            label={incidentId ? "Message about this incident" : "Operational question"}
            value={message}
            onChange={(event) => setMessage(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                void send();
              }
            }}
          />
          {sending ? (
            <Tooltip title="Stop agent">
              <IconButton color="error" onClick={() => controller.current?.abort()} aria-label="Stop agent">
                <StopCircleOutlinedIcon />
              </IconButton>
            </Tooltip>
          ) : (
            <Tooltip title="Send message">
              <span>
                <IconButton type="submit" color="primary" disabled={!message.trim()} aria-label="Send message">
                  <SendIcon />
                </IconButton>
              </span>
            </Tooltip>
          )}
        </Stack>
      </Box>
    </Stack>
  );
}

function completeActivity(messages: Message[], tool: string, ok: boolean): Message[] {
  let index = -1;
  for (let current = messages.length - 1; current >= 0; current -= 1) {
    const item = messages[current];
    if (item.role === "activity" && item.tool === tool && item.status === "running") {
      index = current;
      break;
    }
  }
  if (index < 0) return messages;
  return messages.map((item, current) => current === index ? { ...item, status: ok ? "succeeded" : "failed" } : item);
}

function toolLabel(tool?: string): string {
  const labels: Record<string, string> = {
    jenkins_get_build_log: "Read Jenkins build log",
    jenkins_analyze_build_failure: "Analyze Jenkins failure",
    jenkins_get_job_build_history: "Compare Jenkins build history",
    jenkins_get_build_stages: "Inspect pipeline stages",
    jenkins_get_test_report: "Inspect test report",
    scm_get_change_diff: "Inspect change diff",
    k8s_get_pod_logs: "Read Kubernetes pod logs",
    k8s_get_events: "Inspect Kubernetes events",
    prometheus_query: "Query Prometheus metrics",
  };
  return labels[tool ?? ""] ?? titleCase((tool ?? "Read operational data").replaceAll("_", " "));
}

function activityDetail(value: unknown): string {
  if (!value || typeof value !== "object" || Array.isArray(value)) return "";
  const args = value as Record<string, unknown>;
  if (args.job_name) return `${String(args.job_name)}${args.build_number ? ` #${String(args.build_number)}` : ""}`;
  if (args.pod_name) return `${String(args.namespace ?? "")}/${String(args.pod_name)}`.replace(/^\//, "");
  if (args.repository) return `${String(args.repository)} #${String(args.change_number ?? "")}`;
  return "";
}

function stringValue(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function nullableString(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function arrayRecords(value: unknown): Array<Record<string, unknown>> {
  return Array.isArray(value)
    ? value.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === "object" && !Array.isArray(item))
    : [];
}
