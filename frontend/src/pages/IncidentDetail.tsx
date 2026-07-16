import {
  Alert,
  Box,
  Button,
  Chip,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Divider,
  Link,
  Paper,
  Stack,
  Tab,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Tabs,
  TextField,
  Typography,
} from "@mui/material";
import ArrowBackIcon from "@mui/icons-material/ArrowBack";
import NotificationsOffOutlinedIcon from "@mui/icons-material/NotificationsOffOutlined";
import NotificationsActiveOutlinedIcon from "@mui/icons-material/NotificationsActiveOutlined";
import PsychologyOutlinedIcon from "@mui/icons-material/PsychologyOutlined";
import OpenInNewIcon from "@mui/icons-material/OpenInNew";
import { useCallback, useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import ChatPanel from "../components/ChatPanel";
import MarkdownContent from "../components/MarkdownContent";
import { SourceDetails } from "../components/SourceAttribution";
import PageHeader from "../components/PageHeader";
import { ErrorPanel, LoadingPanel } from "../components/StatePanel";
import StatusChip from "../components/StatusChip";
import {
  getIncident,
  reinvestigateIncident,
  suppressIncident,
  unsuppressIncident,
  type IncidentDetail,
  type Observation,
} from "../services/api";
import { formatDate, formatTokens, formatUsd, titleCase } from "../utils/format";

const tabs = ["Summary", "Affected resources", "Investigation", "History", "Actions", "Chat"];

export default function IncidentDetailPage() {
  const { incidentId } = useParams();
  const navigate = useNavigate();
  const [detail, setDetail] = useState<IncidentDetail | null>(null);
  const [tab, setTab] = useState(0);
  const [loading, setLoading] = useState(true);
  const [working, setWorking] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [suppressOpen, setSuppressOpen] = useState(false);
  const [reason, setReason] = useState("");

  const refresh = useCallback(async () => {
    if (!incidentId) return;
    try {
      setDetail(await getIncident(incidentId));
      setError(null);
    } catch (requestError) {
      setError(requestError);
    } finally {
      setLoading(false);
    }
  }, [incidentId]);

  useEffect(() => void refresh(), [refresh]);

  const investigationActive = ["queued", "running"].includes(detail?.investigation_request?.status ?? "");
  useEffect(() => {
    if (!investigationActive) return;
    const timer = window.setInterval(() => void refresh(), 2500);
    return () => window.clearInterval(timer);
  }, [investigationActive, refresh]);

  async function suppress() {
    if (!incidentId || !reason.trim()) return;
    setWorking(true);
    try {
      await suppressIncident(incidentId, reason.trim());
      setSuppressOpen(false);
      setReason("");
      await refresh();
    } catch (requestError) {
      setError(requestError);
    } finally {
      setWorking(false);
    }
  }

  async function unsuppress() {
    if (!incidentId) return;
    setWorking(true);
    try {
      await unsuppressIncident(incidentId);
      await refresh();
    } catch (requestError) {
      setError(requestError);
    } finally {
      setWorking(false);
    }
  }

  async function reinvestigate() {
    if (!incidentId) return;
    setWorking(true);
    try {
      await reinvestigateIncident(incidentId);
      await refresh();
      setTab(2);
    } catch (requestError) {
      setError(requestError);
    } finally {
      setWorking(false);
    }
  }

  if (loading) return <LoadingPanel label="Loading incident" />;
  if (error && !detail) return <ErrorPanel error={error} />;
  if (!detail) return <Alert severity="warning">Incident not found</Alert>;
  const incident = detail.incident;

  return (
    <Box>
      <Button startIcon={<ArrowBackIcon />} onClick={() => navigate("/incidents")} sx={{ mb: 1 }}>
        Incidents
      </Button>
      <PageHeader
        title={incident.title}
        subtitle={incident.id}
        actions={
          <Stack direction="row" gap={1} flexWrap="wrap">
            <Button
              variant="outlined"
              startIcon={<PsychologyOutlinedIcon />}
              disabled={working || investigationActive}
              onClick={() => void reinvestigate()}
            >
              {investigationActive ? "Investigation in progress" : "Reinvestigate"}
            </Button>
            {incident.status === "suppressed" ? (
              <Button
                variant="outlined"
                startIcon={<NotificationsActiveOutlinedIcon />}
                disabled={working}
                onClick={() => void unsuppress()}
              >
                Unsuppress
              </Button>
            ) : (
              <Button
                color="warning"
                variant="outlined"
                startIcon={<NotificationsOffOutlinedIcon />}
                disabled={working}
                onClick={() => setSuppressOpen(true)}
              >
                Suppress
              </Button>
            )}
          </Stack>
        }
      />

      {Boolean(error) && <Box sx={{ mb: 2 }}><ErrorPanel error={error} /></Box>}
      <Paper variant="outlined" sx={{ mb: 2.5, p: { xs: 2, md: 2.5 } }}>
        <Stack direction={{ xs: "column", lg: "row" }} justifyContent="space-between" gap={2.5}>
          <Stack direction="row" gap={1} flexWrap="wrap" alignItems="center">
            <StatusChip value={incident.severity} size="medium" />
            <StatusChip value={incident.status} size="medium" />
            <Chip variant="outlined" label={`${incident.affected_resource_count} affected`} />
            <Chip variant="outlined" label={`Occurrence #${incident.occurrence_number}`} />
            <Chip variant="outlined" label={titleCase(incident.domain)} />
          </Stack>
          <Stack direction={{ xs: "column", sm: "row" }} gap={{ xs: 0.5, sm: 3 }}>
            <Meta label="Opened" value={formatDate(incident.created_at)} />
            <Meta label="Updated" value={formatDate(incident.updated_at)} />
            <Meta label="Resolved" value={formatDate(incident.resolved_at)} />
          </Stack>
        </Stack>
        {incident.suppressed_reason && (
          <Alert severity="info" sx={{ mt: 2 }}>
            Suppressed by {incident.suppressed_by}: {incident.suppressed_reason}
          </Alert>
        )}
      </Paper>

      <Paper variant="outlined" sx={{ overflow: "hidden" }}>
        <Tabs
          value={tab}
          onChange={(_, value) => setTab(value)}
          variant="scrollable"
          scrollButtons="auto"
          sx={{ px: 1, borderBottom: "1px solid", borderColor: "divider" }}
        >
          {tabs.map((label, index) => {
            const count = label === "Affected resources" ? (detail.current_observations ?? []).length : label === "Actions" ? detail.actions.length : null;
            return <Tab key={label} label={count === null ? label : `${label} (${count})`} id={`incident-tab-${index}`} />;
          })}
        </Tabs>
        <Box sx={{ p: { xs: 2, md: 2.5 } }}>
          {tab === 0 && <Overview detail={detail} />}
          {tab === 1 && <Observations detail={detail} />}
          {tab === 2 && <InvestigationView detail={detail} />}
          {tab === 3 && <History detail={detail} />}
          {tab === 4 && <ActionsView detail={detail} onOpen={(id) => navigate(`/actions/${id}`)} />}
          {tab === 5 && <ChatPanel incidentId={incident.id} />}
        </Box>
      </Paper>

      <Dialog open={suppressOpen} onClose={() => !working && setSuppressOpen(false)} fullWidth maxWidth="sm">
        <DialogTitle>Suppress incident</DialogTitle>
        <DialogContent>
          <TextField
            autoFocus
            fullWidth
            multiline
            minRows={3}
            label="Audit reason"
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            sx={{ mt: 1 }}
          />
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setSuppressOpen(false)} disabled={working}>Cancel</Button>
          <Button color="warning" variant="contained" onClick={() => void suppress()} disabled={working || !reason.trim()}>
            Suppress
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}

function Overview({ detail }: { detail: IncidentDetail }) {
  const incident = detail.incident;
  const investigation = detail.latest_investigation;
  const hasAssessment = investigation?.status === "succeeded" || investigation?.status === "partial";
  const analysisPartial = investigation?.status === "partial";
  const result = hasAssessment ? investigation.result : {};
  const requestActive = ["queued", "running"].includes(detail.investigation_request?.status ?? "");
  const analysisFailed = detail.investigation_request?.status === "failed" || (!requestActive && investigation?.status === "failed");
  const decision = detail.analysis_decision;
  return (
    <Stack gap={3}>
      <Box>
        <Typography variant="h6" sx={{ mb: 1.25 }}>What we know</Typography>
        <Typography variant="body1">
          This condition currently affects <strong>{incident.affected_resource_count}</strong> resource{incident.affected_resource_count === 1 ? "" : "s"}. It was first seen {formatDate(incident.first_seen_at ?? incident.created_at)} and last confirmed {formatDate(incident.last_seen_at ?? incident.updated_at)}.
        </Typography>
      </Box>
      {decision && (
        <Alert severity={decision.outcome === "budget_deferred" ? "warning" : decision.outcome === "selected" ? "info" : "success"}>
          <Typography variant="body2" fontWeight={700}>Agent selection: {titleCase(decision.outcome)}</Typography>
          <Typography variant="body2">{decision.reason}</Typography>
        </Alert>
      )}
      {analysisFailed && (
        <Alert severity="error">
          Agent analysis failed before producing a root-cause assessment. This is a Watchdog agent error, not an explanation of the incident. Use Reinvestigate to retry it.
        </Alert>
      )}
      {analysisPartial && (
        <Alert severity="warning">
          This is a partial agent assessment based on the evidence collected before analysis stopped.
          {investigation.error_summary && <Typography variant="caption" component="div" sx={{ mt: 0.5 }}>Diagnostic: {investigation.error_summary}</Typography>}
        </Alert>
      )}
      {textValue(result.quality_gate, "") && <Alert severity="warning">{textValue(result.quality_gate, "")}</Alert>}
      <Divider />
      <Box>
        <Typography variant="h6" sx={{ mb: 1.25 }}>Likely cause</Typography>
        <MarkdownContent content={textValue(result.root_cause, "Investigation has not produced a root-cause assessment yet.")} />
      </Box>
      <Divider />
      <Box>
        <Typography variant="h6" sx={{ mb: 1.25 }}>Recommended next action</Typography>
        <MarkdownContent content={textValue(result.suggested_fix, "Review the affected resources and their source-system links.")} />
      </Box>
      <Divider />
      <Stack direction={{ xs: "column", md: "row" }} gap={3}>
        <Meta label="Actionability" value={incident.actionability ? titleCase(incident.actionability) : "Unknown"} />
        <Meta label="Classification" value={incident.classification ? titleCase(incident.classification) : titleCase(incident.domain)} />
        <Meta label="Priority" value={incident.priority ? titleCase(incident.priority) : titleCase(incident.severity)} />
        <Meta label="Confidence" value={investigation?.confidence ? titleCase(investigation.confidence) : "Not assessed"} />
      </Stack>
      {Object.keys(incident.source).length > 0 && (
        <>
          <Divider />
          <Box>
            <Typography variant="h6" sx={{ mb: 1.25 }}>Source association</Typography>
            <SourceDetails source={incident.source} />
          </Box>
        </>
      )}
    </Stack>
  );
}

function History({ detail }: { detail: IncidentDetail }) {
  return (
    <Stack gap={2} divider={<Divider flexItem />}>
      {detail.occurrences.map((occurrence) => (
        <Stack key={occurrence.id} direction={{ xs: "column", md: "row" }} gap={2} sx={{ py: 1.5 }}>
          <Typography fontWeight={700} sx={{ width: 110 }}>Occurrence #{occurrence.number}</Typography>
          <Box sx={{ flex: 1 }}>
            <Typography variant="body2">{formatDate(occurrence.opened_at)} to {formatDate(occurrence.resolved_at)}</Typography>
            <Typography variant="caption" color="text.secondary">
              {occurrence.responsible_checks.map(titleCase).join(", ")}
            </Typography>
          </Box>
          <Typography variant="body2" color="text.secondary">{occurrence.observation_identities.length} unique resources observed</Typography>
        </Stack>
      ))}
    </Stack>
  );
}

function Observations({ detail }: { detail: IncidentDetail }) {
  const observations = [...(detail.current_observations ?? [])].sort(compareObservations);
  if (!observations.length) return <Typography color="text.secondary">No current affected resources</Typography>;
  return (
    <TableContainer>
      <Table sx={{ minWidth: 820 }}>
        <TableHead><TableRow><TableCell>Resource</TableCell><TableCell>Problem</TableCell><TableCell>Details</TableCell><TableCell>Severity</TableCell><TableCell>Observed</TableCell><TableCell align="right">Source</TableCell></TableRow></TableHead>
        <TableBody>
          {observations.map((item) => {
            const url = typeof item.evidence.url === "string" ? item.evidence.url : "";
            return (
              <TableRow key={`${item.scan_id}-${item.stable_identity}`}>
                <TableCell>
                  <Typography variant="body2" fontWeight={650}>{resourceLabel(item)}</Typography>
                  <Typography variant="caption" color="text.secondary">{item.resource_id}</Typography>
                </TableCell>
                <TableCell>{item.summary}</TableCell>
                <TableCell>{observationDetails(item)}</TableCell>
                <TableCell><StatusChip value={item.severity} /></TableCell>
                <TableCell>{formatDate(item.observed_at)}</TableCell>
                <TableCell align="right">{url ? <Link href={url} target="_blank" rel="noreferrer" aria-label={`Open ${resourceLabel(item)}`}><OpenInNewIcon fontSize="small" /></Link> : "-"}</TableCell>
              </TableRow>
            );
          })}
        </TableBody>
      </Table>
    </TableContainer>
  );
}

function InvestigationView({ detail }: { detail: IncidentDetail }) {
  const investigation = detail.latest_investigation;
  const request = detail.investigation_request;
  const decision = detail.analysis_decision;
  if (!investigation && !request && !decision) return <Typography color="text.secondary">No investigation recorded</Typography>;
  const hasAssessment = investigation?.status === "succeeded" || investigation?.status === "partial";
  const analysisPartial = investigation?.status === "partial";
  const requestActive = ["queued", "running"].includes(request?.status ?? "");
  const analysisFailed = request?.status === "failed" || (!requestActive && investigation?.status === "failed");
  const failureDetail = request?.error_summary || investigation?.error_summary;
  return (
    <Stack gap={2.5}>
      <Stack direction="row" gap={1} flexWrap="wrap">
        {request && (requestActive || analysisFailed || !investigation) && <StatusChip value={request.status} />}
        {request && <Chip size="small" label={`${titleCase(request.mode)} mode`} variant="outlined" />}
        {investigation && <StatusChip value={investigation.status} />}
        {hasAssessment && investigation.confidence && <Chip size="small" label={`${titleCase(investigation.confidence)} confidence`} variant="outlined" />}
        {hasAssessment && <Chip size="small" label={investigation.model} variant="outlined" />}
        {request && request.reserved_tokens > 0 && <Chip size="small" label={`${formatTokens(request.reserved_tokens)} reserved`} variant="outlined" />}
      </Stack>
      {decision && (
        <Alert severity={decision.outcome === "budget_deferred" ? "warning" : "info"}>
          <strong>{titleCase(decision.outcome)}:</strong> {decision.reason}
        </Alert>
      )}
      {request?.status === "queued" && <Alert severity="info">Agent analysis is queued.</Alert>}
      {request?.status === "running" && <Alert severity="info">Agent is gathering live evidence.</Alert>}
      {analysisPartial && (
        <Alert severity="warning">
          The agent retained a partial assessment from completed evidence reads. Review it before acting or run a focused investigation again.
          {failureDetail && <Typography variant="caption" component="div" sx={{ mt: 0.5 }}>Diagnostic: {failureDetail}</Typography>}
        </Alert>
      )}
      {analysisFailed && (
        <Alert severity="error">
          Agent analysis failed before producing a root-cause assessment. This is a Watchdog agent error, not the incident root cause. Use Reinvestigate to retry it.
          {failureDetail && <Typography variant="caption" component="div" sx={{ mt: 0.5 }}>Diagnostic: {failureDetail}</Typography>}
        </Alert>
      )}
      {hasAssessment && textValue(investigation.result.quality_gate, "") && <Alert severity="warning">{textValue(investigation.result.quality_gate, "")}</Alert>}
      {!hasAssessment && !analysisFailed && <Typography color="text.secondary">No completed agent assessment recorded.</Typography>}
      {hasAssessment && <>
      <Box>
        <Typography variant="h6" sx={{ mb: 1 }}>Root cause</Typography>
        <MarkdownContent content={textValue(investigation.result.root_cause)} />
      </Box>
      <Divider />
      <Box>
        <Typography variant="h6" sx={{ mb: 1 }}>Impact</Typography>
        <MarkdownContent content={textValue(investigation.result.impact)} />
      </Box>
      <Divider />
      <Box>
        <Typography variant="h6" sx={{ mb: 1 }}>Recommended action</Typography>
        <MarkdownContent content={textValue(investigation.result.suggested_fix)} />
      </Box>
      {Array.isArray(investigation.result.evidence) && (
        <>
          <Divider />
          <Box>
            <Typography variant="h6" sx={{ mb: 1 }}>Evidence used</Typography>
            <Stack component="ul" gap={0.75} sx={{ pl: 2.5, m: 0 }}>
              {investigation.result.evidence.map((item, index) => <Typography component="li" variant="body2" key={index}>{textValue(item)}</Typography>)}
            </Stack>
          </Box>
        </>
      )}
      <Divider />
      <Stack direction={{ xs: "column", md: "row" }} gap={3}>
        <Meta label="Evidence hash" value={investigation.evidence_hash} />
        <Meta label="Prompt" value={investigation.prompt_version} />
        <Meta label="Completed" value={formatDate(investigation.completed_at)} />
      </Stack>
      {Object.keys(investigation.usage).length > 0 && <UsageSummary usage={investigation.usage} />}
      {(investigation.model_calls ?? []).length > 0 && (
        <>
          <Divider />
          <Box>
            <Typography variant="h6" sx={{ mb: 1 }}>Model calls</Typography>
            <TableContainer>
              <Table size="small" sx={{ minWidth: 680 }}>
                <TableHead><TableRow><TableCell>Purpose</TableCell><TableCell>Model</TableCell><TableCell align="right">Input</TableCell><TableCell align="right">Output</TableCell><TableCell align="right">Cached</TableCell><TableCell align="right">Cost</TableCell></TableRow></TableHead>
                <TableBody>
                  {(investigation.model_calls ?? []).map((call) => (
                    <TableRow key={call.id}>
                      <TableCell>{titleCase(call.purpose)}</TableCell>
                      <TableCell>{call.model}</TableCell>
                      <TableCell align="right">{call.prompt_tokens.toLocaleString()}</TableCell>
                      <TableCell align="right">{call.completion_tokens.toLocaleString()}</TableCell>
                      <TableCell align="right">{call.cache_read_input_tokens.toLocaleString()}</TableCell>
                      <TableCell align="right">{formatUsd(call.estimated_cost_usd)}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </TableContainer>
          </Box>
        </>
      )}
      {Array.isArray(investigation.result.tool_trace) && investigation.result.tool_trace.length > 0 && (
        <>
          <Divider />
          <Box>
            <Typography variant="h6" sx={{ mb: 1 }}>Live evidence trace</Typography>
            <Stack gap={0.75}>
              {investigation.result.tool_trace.map((raw, index) => {
                const item = recordValue(raw);
                return (
                  <Stack key={`${textValue(item.tool)}-${index}`} direction="row" gap={1} alignItems="center">
                    <StatusChip value={item.ok ? "succeeded" : "failed"} />
                    <Typography variant="body2" fontWeight={650}>{titleCase(textValue(item.tool))}</Typography>
                    <Typography variant="caption" color="text.secondary">round {textValue(item.round)} · {textValue(item.duration_ms)} ms</Typography>
                  </Stack>
                );
              })}
            </Stack>
          </Box>
        </>
      )}
      </>}
    </Stack>
  );
}

function UsageSummary({ usage }: { usage: Record<string, unknown> }) {
  return (
    <Box>
      <Typography variant="h6" sx={{ mb: 1 }}>Investigation cost</Typography>
      <Stack direction={{ xs: "column", sm: "row" }} gap={{ xs: 1, sm: 4 }}>
        <Meta label="Model calls" value={String(usage.call_count ?? 0)} />
        <Meta label="Input" value={formatTokens(usage.prompt_tokens)} />
        <Meta label="Output" value={formatTokens(usage.completion_tokens)} />
        <Meta label="Cache read" value={formatTokens(usage.cache_read_input_tokens)} />
        <Meta label="Estimated cost" value={formatUsd(usage.estimated_cost_usd)} />
      </Stack>
    </Box>
  );
}

function resourceLabel(item: Observation): string {
  const jobName = item.evidence.job_name;
  const buildNumber = item.evidence.build_number;
  if (typeof jobName === "string") return buildNumber === undefined ? jobName : `${jobName} #${String(buildNumber)}`;
  const pool = item.evidence.agent_pool;
  if (typeof pool === "string" && pool) return pool;
  const queueTask = item.evidence.queue_task;
  if (typeof queueTask === "string" && queueTask) return queueTask;
  return item.resource_id;
}

function observationDetails(item: Observation): string {
  const details = [];
  if (typeof item.evidence.elapsed_hours === "number") details.push(`${item.evidence.elapsed_hours.toFixed(1)}h runtime`);
  if (typeof item.evidence.wait_minutes === "number") details.push(`${durationMinutes(item.evidence.wait_minutes)} waiting`);
  if (typeof item.evidence.reason === "string" && item.evidence.reason) details.push(item.evidence.reason);
  if (typeof item.evidence.node === "string" && item.evidence.node) details.push(item.evidence.node);
  if (typeof item.evidence.container === "string" && item.evidence.container) details.push(`container ${item.evidence.container}`);
  return details.join(" · ") || titleCase(item.category);
}

function compareObservations(left: Observation, right: Observation): number {
  const leftDuration = numericEvidence(left, "elapsed_hours") * 60 + numericEvidence(left, "wait_minutes");
  const rightDuration = numericEvidence(right, "elapsed_hours") * 60 + numericEvidence(right, "wait_minutes");
  return rightDuration - leftDuration || resourceLabel(left).localeCompare(resourceLabel(right));
}

function numericEvidence(item: Observation, key: string): number {
  const value = item.evidence[key];
  return typeof value === "number" ? value : 0;
}

function durationMinutes(value: number): string {
  if (value >= 1440) return `${(value / 1440).toFixed(1)}d`;
  if (value >= 60) return `${(value / 60).toFixed(1)}h`;
  return `${Math.round(value)}m`;
}

function textValue(value: unknown, fallback = "Not available"): string {
  if (typeof value === "string" && value.trim()) return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return fallback;
}

function recordValue(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function ActionsView({ detail, onOpen }: { detail: IncidentDetail; onOpen: (id: string) => void }) {
  if (!detail.actions.length) return <Typography color="text.secondary">No actions planned</Typography>;
  return (
    <TableContainer>
      <Table sx={{ minWidth: 650 }}>
        <TableHead><TableRow><TableCell>Type</TableCell><TableCell>Destination</TableCell><TableCell>Status</TableCell><TableCell>Attempts</TableCell></TableRow></TableHead>
        <TableBody>
          {detail.actions.map((action) => (
            <TableRow hover key={action.id} onClick={() => onOpen(action.id)} sx={{ cursor: "pointer" }}>
              <TableCell>{titleCase(action.action_type)}</TableCell>
              <TableCell>{action.destination}</TableCell>
              <TableCell><StatusChip value={action.status} /></TableCell>
              <TableCell>{action.attempt_count}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </TableContainer>
  );
}

function Meta({ label, value }: { label: string; value: string }) {
  return (
    <Box sx={{ minWidth: 0 }}>
      <Typography variant="caption" color="text.secondary">{label}</Typography>
      <Typography variant="body2" fontWeight={600} sx={{ overflowWrap: "anywhere" }}>{value}</Typography>
    </Box>
  );
}
