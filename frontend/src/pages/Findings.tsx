import { useEffect, useState } from "react";
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
import DeleteOutlineIcon from "@mui/icons-material/DeleteOutline";
import ExpandMoreIcon from "@mui/icons-material/ExpandMore";
import OpenInNewIcon from "@mui/icons-material/OpenInNew";
import { deleteFinding, fetchFindings, type Finding, type FindingsResponse } from "../services/api";

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

function InvestigationDetails({ finding }: { finding: Finding }) {
  const inv = finding.investigation;
  const [showRaw, setShowRaw] = useState(false);
  const [bugDialogOpen, setBugDialogOpen] = useState(false);
  const [jiraInfo, setJiraInfo] = useState<{ key: string; url: string; assignee: string } | null>(null);
  if (!inv) return <Typography color="text.secondary">Not investigated</Typography>;

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
      <Box sx={{ pt: 1, display: "flex", alignItems: "center", gap: 1, flexWrap: "wrap" }}>
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
        <Accordion key={f.fingerprint} disableGutters sx={{ bgcolor: "background.paper", "&:before": { display: "none" } }}>
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
            <InvestigationDetails finding={f} />
          </AccordionDetails>
        </Accordion>
      ))}
    </Stack>
  );
}
