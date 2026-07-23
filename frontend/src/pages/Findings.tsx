import { useEffect, useRef, useState } from "react";
import {
  Accordion,
  AccordionDetails,
  AccordionSummary,
  Alert,
  Box,
  Button,
  Chip,
  CircularProgress,
  Collapse,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  FormControl,
  IconButton,
  InputLabel,
  MenuItem,
  Select,
  Stack,
  TextField,
  Tooltip,
  Typography,
} from "@mui/material";
import BugReportIcon from "@mui/icons-material/BugReport";
import ChatBubbleOutlineIcon from "@mui/icons-material/ChatBubbleOutline";
import DeleteOutlineIcon from "@mui/icons-material/DeleteOutline";
import ExpandMoreIcon from "@mui/icons-material/ExpandMore";
import OpenInNewIcon from "@mui/icons-material/OpenInNew";
import RefreshIcon from "@mui/icons-material/Refresh";
import SendIcon from "@mui/icons-material/Send";
import {
  deleteFinding,
  fetchFindingChatHistory,
  fetchFindings,
  reinvestigateFinding,
  streamFindingChat,
  type Finding,
  type FindingsResponse,
  type Investigation,
} from "../services/api";

interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  toolCalls?: { name: string; success?: boolean }[];
}

function FindingChatPanel({ fingerprint }: { fingerprint: string }) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [loadingHistory, setLoadingHistory] = useState(true);
  const [currentTools, setCurrentTools] = useState<{ name: string; success?: boolean }[]>([]);
  const abortRef = useRef<AbortController | null>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  };

  useEffect(() => {
    setLoadingHistory(true);
    fetchFindingChatHistory(fingerprint)
      .then((data) => {
        setMessages(
          data.messages.map((m) => ({
            role: m.role as "user" | "assistant",
            content: m.content,
          }))
        );
      })
      .catch(() => {})
      .finally(() => {
        setLoadingHistory(false);
        scrollToBottom();
      });
  }, [fingerprint]);

  const handleSend = async () => {
    const text = input.trim();
    if (!text || streaming) return;

    setInput("");
    setMessages((prev) => [...prev, { role: "user", content: text }]);
    setStreaming(true);
    setCurrentTools([]);

    const controller = new AbortController();
    abortRef.current = controller;

    let assistantContent = "";
    const tools: { name: string; success?: boolean }[] = [];

    setMessages((prev) => [...prev, { role: "assistant", content: "" }]);

    try {
      for await (const event of streamFindingChat(fingerprint, text, controller.signal)) {
        switch (event.type) {
          case "token":
            assistantContent += event.content || "";
            setMessages((prev) => {
              const updated = [...prev];
              updated[updated.length - 1] = { role: "assistant", content: assistantContent, toolCalls: [...tools] };
              return updated;
            });
            scrollToBottom();
            break;
          case "tool_start":
            tools.push({ name: event.tool_name || "unknown" });
            setCurrentTools([...tools]);
            break;
          case "tool_result":
            if (tools.length > 0) {
              tools[tools.length - 1].success = event.success;
            }
            setCurrentTools([...tools]);
            break;
          case "error":
            assistantContent += `\n\nError: ${event.content}`;
            setMessages((prev) => {
              const updated = [...prev];
              updated[updated.length - 1] = { role: "assistant", content: assistantContent, toolCalls: [...tools] };
              return updated;
            });
            break;
        }
      }
    } catch (e) {
      if ((e as Error).name !== "AbortError") {
        assistantContent += `\n\nConnection error: ${(e as Error).message}`;
      }
    } finally {
      setMessages((prev) => {
        const updated = [...prev];
        updated[updated.length - 1] = { role: "assistant", content: assistantContent, toolCalls: [...tools] };
        return updated;
      });
      setStreaming(false);
      setCurrentTools([]);
      abortRef.current = null;
      scrollToBottom();
    }
  };

  return (
    <Box
      sx={{
        mt: 2,
        border: 1,
        borderColor: "divider",
        borderRadius: 1,
        bgcolor: "grey.900",
        overflow: "hidden",
      }}
    >
      <Box sx={{ maxHeight: 400, overflow: "auto", p: 1.5 }}>
        {loadingHistory ? (
          <Box sx={{ display: "flex", justifyContent: "center", py: 2 }}>
            <CircularProgress size={20} />
          </Box>
        ) : messages.length === 0 ? (
          <Typography variant="caption" color="text.secondary" sx={{ display: "block", textAlign: "center", py: 2 }}>
            Ask questions about this finding
          </Typography>
        ) : (
          <Stack spacing={1}>
            {messages.map((msg, i) => (
              <Box key={i} sx={{ display: "flex", justifyContent: msg.role === "user" ? "flex-end" : "flex-start" }}>
                <Box
                  sx={{
                    maxWidth: "85%",
                    px: 1.5,
                    py: 1,
                    borderRadius: 1.5,
                    bgcolor: msg.role === "user" ? "rgba(33, 150, 243, 0.25)" : "grey.800",
                  }}
                >
                  {msg.toolCalls && msg.toolCalls.length > 0 && (
                    <Box sx={{ display: "flex", gap: 0.5, flexWrap: "wrap", mb: 0.5 }}>
                      {msg.toolCalls.map((tc, j) => (
                        <Chip
                          key={j}
                          label={tc.name}
                          size="small"
                          color={tc.success === false ? "error" : tc.success ? "success" : "default"}
                          variant="outlined"
                          sx={{ fontSize: "0.65rem", height: 18 }}
                        />
                      ))}
                    </Box>
                  )}
                  <Typography variant="body2" sx={{ whiteSpace: "pre-wrap" }}>
                    {msg.content || (streaming && i === messages.length - 1 ? "Investigating..." : "")}
                  </Typography>
                </Box>
              </Box>
            ))}
            {streaming && currentTools.length > 0 && (
              <Box sx={{ display: "flex", gap: 0.5, flexWrap: "wrap", alignItems: "center" }}>
                <CircularProgress size={12} />
                {currentTools.map((tc, i) => (
                  <Chip
                    key={i}
                    label={tc.name}
                    size="small"
                    color={tc.success === false ? "error" : tc.success ? "success" : "info"}
                    variant="outlined"
                    sx={{ fontSize: "0.65rem", height: 18 }}
                  />
                ))}
              </Box>
            )}
            <div ref={messagesEndRef} />
          </Stack>
        )}
      </Box>
      <Box sx={{ display: "flex", gap: 1, p: 1, borderTop: 1, borderColor: "divider" }}>
        <TextField
          fullWidth
          placeholder="Ask about this finding..."
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && !e.shiftKey && handleSend()}
          size="small"
          disabled={streaming}
          multiline
          maxRows={3}
          sx={{ "& .MuiInputBase-root": { bgcolor: "grey.800" } }}
        />
        <IconButton onClick={handleSend} color="primary" disabled={!input.trim() || streaming}>
          <SendIcon />
        </IconButton>
      </Box>
    </Box>
  );
}

function CreateBugDialog({ open, onClose, finding, onCreated }: { open: boolean; onClose: () => void; finding: Finding; onCreated: (info: { key: string; url: string; assignee: string }) => void }) {
  const inv = finding.investigation;
  const [project, setProject] = useState("CI");
  const [summary, setSummary] = useState(
    `[${finding.severity}] ${finding.symptom} - ${finding.resource}`.slice(0, 200)
  );
  const [description, setDescription] = useState(() => {
    if (!inv) return `Resource: ${finding.resource}\nSymptom: ${finding.symptom}`;
    return [
      `Resource: ${finding.resource}`,
      `Severity: ${finding.severity}`,
      "",
      `ROOT CAUSE`,
      inv.root_cause,
      "",
      `EVIDENCE`,
      ...inv.evidence.map((e) => `- ${e}`),
      "",
      `IMPACT`,
      inv.impact,
      "",
      `SUGGESTED FIX`,
      inv.suggested_fix,
      inv.fix_location ? `\nLocation: ${inv.fix_location}` : "",
    ].join("\n");
  });
  const [assignee, setAssignee] = useState("");
  const [customAssignee, setCustomAssignee] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState<{ key: string; url: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [jiraConfigured, setJiraConfigured] = useState<boolean | null>(null);

  useEffect(() => {
    if (!open) return;
    fetch("/api/jira/status")
      .then((r) => r.json())
      .then((data) => setJiraConfigured(data.configured ?? false))
      .catch(() => setJiraConfigured(false));
  }, [open]);

  const handleSubmit = async () => {
    setSubmitting(true);
    setError(null);
    try {
      const resp = await fetch("/api/jira/create-bug", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          project_key: project,
          issue_type: "Task",
          summary,
          description,
          assignee_email: (assignee === "__custom__" ? customAssignee : assignee) || null,
          finding_fingerprint: finding.fingerprint,
        }),
      });
      const contentType = resp.headers.get("content-type") || "";
      const data = contentType.includes("application/json")
        ? await resp.json()
        : { error: await resp.text() };
      if (!resp.ok) {
        setError(data.detail ? `${data.error}: ${data.detail}` : data.error || "Failed to create bug");
      } else {
        setResult(data);
        const resolvedAssignee = assignee === "__custom__" ? customAssignee : assignee;
        onCreated({ key: data.key, url: data.url, assignee: resolvedAssignee || "" });
      }
    } catch (e: any) {
      setError(e.message || "Failed to create Jira issue");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog open={open} onClose={onClose} maxWidth="md" fullWidth>
      <DialogTitle>Create Jira Issue</DialogTitle>
      <DialogContent>
        {result ? (
          <Alert severity="success" sx={{ mt: 1 }}>
            Created <a href={result.url} target="_blank" rel="noopener">{result.key}</a>
          </Alert>
        ) : (
          <Stack spacing={2} sx={{ mt: 1 }}>
            {jiraConfigured === false && (
              <Alert severity="warning">
                Jira is not configured on this server. Ask an admin to set WATCHDOG_JIRA_USER_EMAIL and WATCHDOG_JIRA_API_TOKEN.
              </Alert>
            )}
            {error && <Alert severity="error">{error}</Alert>}
            <FormControl fullWidth size="small">
              <InputLabel>Project</InputLabel>
              <Select value={project} label="Project" onChange={(e) => setProject(e.target.value)}>
                <MenuItem value="CI">CI</MenuItem>
              </Select>
            </FormControl>
            <TextField label="Summary" value={summary} onChange={(e) => setSummary(e.target.value)} fullWidth size="small" />
            <TextField label="Description" value={description} onChange={(e) => setDescription(e.target.value)} fullWidth multiline rows={8} size="small" />
            <FormControl fullWidth size="small">
              <InputLabel>Assignee (optional)</InputLabel>
              <Select value={assignee} label="Assignee (optional)" onChange={(e) => setAssignee(e.target.value)}>
                <MenuItem value="">None</MenuItem>
                <MenuItem value="__custom__">Other...</MenuItem>
              </Select>
            </FormControl>
            {assignee === "__custom__" && (
              <TextField label="Assignee email" value={customAssignee} onChange={(e) => setCustomAssignee(e.target.value)} fullWidth size="small" placeholder="user@example.com" />
            )}
          </Stack>
        )}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>{result ? "Close" : "Cancel"}</Button>
        {!result && (
          <Button onClick={handleSubmit} variant="contained" disabled={submitting || !summary || jiraConfigured === false}>
            {submitting ? "Creating..." : "Create Issue"}
          </Button>
        )}
      </DialogActions>
    </Dialog>
  );
}

function InvestigationDetails({
  finding,
  onInvestigationUpdate,
}: {
  finding: Finding;
  onInvestigationUpdate: (investigation: Investigation) => void;
}) {
  const inv = finding.investigation;
  const [showRaw, setShowRaw] = useState(false);
  const [showChat, setShowChat] = useState(false);
  const [reinvestigating, setReinvestigating] = useState(false);
  const [reinvestigateError, setReinvestigateError] = useState<string | null>(null);
  const [bugDialogOpen, setBugDialogOpen] = useState(false);
  const [jiraInfo, setJiraInfo] = useState<{ key: string; url: string; assignee: string } | null>(null);

  const handleReinvestigate = async () => {
    setReinvestigating(true);
    setReinvestigateError(null);
    try {
      const result = await reinvestigateFinding(finding.fingerprint);
      if (result.investigation) {
        onInvestigationUpdate(result.investigation);
      }
    } catch (e) {
      setReinvestigateError(e instanceof Error ? e.message : "Reinvestigation failed");
    } finally {
      setReinvestigating(false);
    }
  };

  const actionButtons = (
    <Box sx={{ pt: 1, display: "flex", alignItems: "center", gap: 1, flexWrap: "wrap" }}>
      <Button
        size="small"
        variant={showChat ? "contained" : "outlined"}
        startIcon={<ChatBubbleOutlineIcon />}
        onClick={() => setShowChat(!showChat)}
        sx={{ textTransform: "none" }}
      >
        Chat
      </Button>
      {inv && (
        <Button
          size="small"
          variant="outlined"
          startIcon={reinvestigating ? <CircularProgress size={14} color="inherit" /> : <RefreshIcon />}
          onClick={handleReinvestigate}
          disabled={reinvestigating}
          sx={{ textTransform: "none" }}
        >
          Reinvestigate
        </Button>
      )}
      {inv && (
        <Button
          size="small"
          variant="outlined"
          color="warning"
          startIcon={<BugReportIcon />}
          onClick={() => setBugDialogOpen(true)}
          sx={{ textTransform: "none" }}
        >
          Create Issue
        </Button>
      )}
      {jiraInfo && (
        <Chip
          label={`${jiraInfo.key}${jiraInfo.assignee ? ` > ${jiraInfo.assignee}` : ""}`}
          size="small"
          color="info"
          component="a"
          href={jiraInfo.url}
          target="_blank"
          clickable
        />
      )}
      <CreateBugDialog open={bugDialogOpen} onClose={() => setBugDialogOpen(false)} finding={finding} onCreated={setJiraInfo} />
    </Box>
  );

  if (!inv) {
    return (
      <Stack spacing={1} sx={{ mt: 1 }}>
        <Alert severity="info">
          Not investigated yet. Chat to ask questions or run a Deep Scan.
        </Alert>
        {reinvestigateError && <Alert severity="error">{reinvestigateError}</Alert>}
        {actionButtons}
        {showChat && <FindingChatPanel fingerprint={finding.fingerprint} />}
      </Stack>
    );
  }

  return (
    <Stack spacing={2} sx={{ mt: 1 }}>
      <Box>
        <Typography variant="subtitle2" color="primary.main">Root Cause</Typography>
        <Typography variant="body2">{inv.root_cause}</Typography>
      </Box>
      <Box>
        <Typography variant="subtitle2" color="error.main">Impact</Typography>
        <Typography variant="body2">{inv.impact}</Typography>
      </Box>
      <Box>
        <Typography variant="subtitle2" color="success.main">Suggested Fix</Typography>
        <Typography variant="body2" sx={{ whiteSpace: "pre-wrap" }}>{inv.suggested_fix}</Typography>
        {inv.fix_location && (
          <Typography variant="caption" color="text.secondary">
            Location: {inv.fix_location}
          </Typography>
        )}
      </Box>
      {inv.evidence.length > 0 && (
        <Box>
          <Typography variant="subtitle2">Evidence</Typography>
          <Stack spacing={0.5} sx={{ pl: 1 }}>
            {inv.evidence.map((e, i) => (
              <Typography key={i} variant="caption" color="text.secondary">
                {e}
              </Typography>
            ))}
          </Stack>
        </Box>
      )}
      <Box sx={{ display: "flex", gap: 0.5, flexWrap: "wrap" }}>
        <Typography variant="caption" color="text.secondary" sx={{ mr: 1 }}>Tools:</Typography>
        {[...new Set(inv.tools_used)].map((t) => (
          <Chip key={t} label={t} size="small" variant="outlined" sx={{ fontSize: "0.65rem", height: 20 }} />
        ))}
      </Box>
      {inv.raw_reasoning && (
        <Box>
          <Button size="small" onClick={() => setShowRaw(!showRaw)} sx={{ textTransform: "none", p: 0 }}>
            {showRaw ? "Hide" : "Show"} full reasoning
          </Button>
          <Collapse in={showRaw}>
            <Box sx={{ mt: 1, p: 1.5, bgcolor: "grey.900", borderRadius: 1, maxHeight: 400, overflow: "auto" }}>
              <Typography variant="caption" sx={{ whiteSpace: "pre-wrap", fontFamily: "monospace", color: "grey.300" }}>
                {inv.raw_reasoning}
              </Typography>
            </Box>
          </Collapse>
        </Box>
      )}
      {reinvestigateError && <Alert severity="error">{reinvestigateError}</Alert>}
      {actionButtons}
      {showChat && <FindingChatPanel fingerprint={finding.fingerprint} />}
    </Stack>
  );
}

function timeAgo(isoString: string | null): string {
  if (!isoString) return "";
  const diff = Date.now() - new Date(isoString).getTime();
  const minutes = Math.floor(diff / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

export default function Findings() {
  const [data, setData] = useState<FindingsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [severityFilter, setSeverityFilter] = useState<string>("all");
  const [categoryFilter, setCategoryFilter] = useState<string>("all");

  useEffect(() => {
    fetchFindings()
      .then(setData)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  const handleInvestigationUpdate = (fingerprint: string, investigation: Investigation) => {
    setData((prev) =>
      prev
        ? {
            ...prev,
            findings: prev.findings.map((f) =>
              f.fingerprint === fingerprint ? { ...f, investigation } : f
            ),
          }
        : prev
    );
  };

  const handleDismiss = async (fingerprint: string) => {
    try {
      await deleteFinding(fingerprint);
      setData((prev) => prev ? {
        ...prev,
        findings: prev.findings.filter((f) => f.fingerprint !== fingerprint),
        total_findings: prev.total_findings - 1,
      } : prev);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to dismiss");
    }
  };

  if (loading) {
    return <Box sx={{ display: "flex", justifyContent: "center", py: 8 }}><CircularProgress /></Box>;
  }
  if (error) return <Alert severity="error">{error}</Alert>;

  const findings = data?.findings || [];
  const categories = [...new Set(findings.map((f) => f.category))];

  const filtered = findings.filter((f) => {
    if (severityFilter !== "all" && f.severity !== severityFilter) return false;
    if (categoryFilter !== "all" && f.category !== categoryFilter) return false;
    return true;
  });

  return (
    <Stack spacing={3}>
      <Box sx={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <Typography variant="h4">Findings</Typography>
        <Typography variant="body2" color="text.secondary">
          {filtered.length} of {findings.length} findings
        </Typography>
      </Box>

      <Box sx={{ display: "flex", gap: 2 }}>
        <FormControl size="small" sx={{ minWidth: 140 }}>
          <InputLabel>Severity</InputLabel>
          <Select value={severityFilter} label="Severity" onChange={(e) => setSeverityFilter(e.target.value)}>
            <MenuItem value="all">All</MenuItem>
            <MenuItem value="critical">Critical</MenuItem>
            <MenuItem value="warning">Warning</MenuItem>
            <MenuItem value="low">Low</MenuItem>
          </Select>
        </FormControl>
        <FormControl size="small" sx={{ minWidth: 140 }}>
          <InputLabel>Category</InputLabel>
          <Select value={categoryFilter} label="Category" onChange={(e) => setCategoryFilter(e.target.value)}>
            <MenuItem value="all">All</MenuItem>
            {categories.map((c) => (
              <MenuItem key={c} value={c}>{c}</MenuItem>
            ))}
          </Select>
        </FormControl>
      </Box>

      {filtered.map((f) => (
        <Accordion
          key={f.fingerprint}
          disableGutters
          defaultExpanded={!!f.investigation}
          sx={{ bgcolor: "background.paper", "&:before": { display: "none" } }}
        >
          <AccordionSummary expandIcon={<ExpandMoreIcon />}>
            <Box sx={{ display: "flex", alignItems: "center", gap: 1.5, width: "100%" }}>
              {f.status === "new" && (
                <Chip label="NEW" size="small" color="info" sx={{ fontWeight: 700, fontSize: "0.65rem" }} />
              )}
              <Chip
                label={f.severity.toUpperCase()}
                size="small"
                color={f.severity === "critical" ? "error" : f.severity === "warning" ? "warning" : "default"}
              />
              <Chip label={f.category} size="small" variant="outlined" />
              <Typography variant="body2" sx={{ fontWeight: 600 }}>
                {f.resource}
              </Typography>
              <Typography variant="body2" color="text.secondary" sx={{ ml: "auto", mr: 2 }}>
                {f.symptom}
              </Typography>
              <Typography variant="caption" color="text.secondary" sx={{ whiteSpace: "nowrap" }}>
                {f.first_seen === f.last_seen ? timeAgo(f.first_seen) : `since ${timeAgo(f.first_seen)}`}
              </Typography>
              {f.jira_issue && (
                <Chip
                  label={f.jira_issue.key}
                  size="small"
                  color="info"
                  icon={<OpenInNewIcon sx={{ fontSize: 14 }} />}
                  component="a"
                  href={f.jira_issue.url}
                  target="_blank"
                  clickable
                  onClick={(e: React.MouseEvent) => e.stopPropagation()}
                />
              )}
              {f.investigation && (
                <Chip label={`${f.investigation.confidence} confidence`} size="small" color="success" />
              )}
              <Tooltip title="Dismiss finding">
                <IconButton
                  size="small"
                  onClick={(e) => { e.stopPropagation(); handleDismiss(f.fingerprint); }}
                  sx={{ ml: 0.5, opacity: 0.5, "&:hover": { opacity: 1, color: "error.main" } }}
                >
                  <DeleteOutlineIcon fontSize="small" />
                </IconButton>
              </Tooltip>
            </Box>
          </AccordionSummary>
          <AccordionDetails>
            <InvestigationDetails
              finding={f}
              onInvestigationUpdate={(investigation) => handleInvestigationUpdate(f.fingerprint, investigation)}
            />
          </AccordionDetails>
        </Accordion>
      ))}
    </Stack>
  );
}
