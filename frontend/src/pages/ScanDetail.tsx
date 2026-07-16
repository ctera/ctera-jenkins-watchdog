import {
  Alert,
  Box,
  Button,
  Chip,
  Divider,
  LinearProgress,
  Paper,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Typography,
} from "@mui/material";
import ArrowBackIcon from "@mui/icons-material/ArrowBack";
import CancelOutlinedIcon from "@mui/icons-material/CancelOutlined";
import { useCallback, useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import PageHeader from "../components/PageHeader";
import { ErrorPanel, LoadingPanel } from "../components/StatePanel";
import StatusChip from "../components/StatusChip";
import { useScanEvents } from "../hooks/useScanEvents";
import { cancelScan, getScan, type Scan } from "../services/api";
import { formatDate, formatTokens, formatUsd, titleCase } from "../utils/format";
import {
  analysisProgress,
  isAnalysisActive,
  isCollectionActive,
  scanStageLabel,
  scanWorkflowStatus,
} from "../utils/scan";

const STAGES = ["queued", "detecting", "findings_stored", "correlating", "reconciling", "investigating", "planning_actions", "completed"];

export default function ScanDetailPage() {
  const { scanId } = useParams();
  const navigate = useNavigate();
  const [scan, setScan] = useState<Scan | null>(null);
  const [loading, setLoading] = useState(true);
  const [cancelling, setCancelling] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const refresh = useCallback(async () => {
    if (!scanId) return;
    try {
      setScan(await getScan(scanId));
      setError(null);
    } catch (requestError) {
      setError(requestError);
    } finally {
      setLoading(false);
    }
  }, [scanId]);

  useEffect(() => void refresh(), [refresh]);
  const { events, connection } = useScanEvents(scanId, refresh, false);

  useEffect(() => {
    if (events.length) void refresh();
  }, [events.length, refresh]);

  const workflowActive = Boolean(scan && (isCollectionActive(scan) || isAnalysisActive(scan)));
  useEffect(() => {
    if (!workflowActive) return;
    const timer = window.setInterval(() => void refresh(), 2_500);
    return () => window.clearInterval(timer);
  }, [refresh, workflowActive]);

  async function cancel() {
    if (!scanId) return;
    setCancelling(true);
    try {
      await cancelScan(scanId);
      await refresh();
    } catch (requestError) {
      setError(requestError);
    } finally {
      setCancelling(false);
    }
  }

  if (loading) return <LoadingPanel label="Loading scan" />;
  if (error && !scan) return <ErrorPanel error={error} />;
  if (!scan) return <Alert severity="warning">Scan not found</Alert>;

  const collectionActive = isCollectionActive(scan);
  const analysisActive = isAnalysisActive(scan);
  const workflow = scanWorkflowStatus(scan);
  const progress = ((STAGES.indexOf(scan.stage) + 1) / STAGES.length) * 100;
  const llmUsage = scan.llm_usage ?? {};
  const analysis = scan.analysis;
  const showAnalysis = Boolean(
    analysis?.candidate_count
    || analysis?.selected_count
    || (analysis && analysis.status !== "not_started")
    || !collectionActive,
  );

  return (
    <Box>
      <Button startIcon={<ArrowBackIcon />} onClick={() => navigate("/scans")} sx={{ mb: 1 }}>
        Scans
      </Button>
      <PageHeader
        title={`${titleCase(scan.mode)} scan`}
        subtitle={scan.id}
        actions={
          collectionActive ? (
            <Button
              color="error"
              variant="outlined"
              startIcon={<CancelOutlinedIcon />}
              disabled={cancelling || Boolean(scan.cancel_requested_at)}
              onClick={() => void cancel()}
            >
              {scan.cancel_requested_at ? "Cancellation requested" : cancelling ? "Stopping" : "Cancel scan"}
            </Button>
          ) : undefined
        }
      />

      {Boolean(error) && <Box sx={{ mb: 2 }}><ErrorPanel error={error} /></Box>}
      {scan.failure_summary && <Alert severity="error" sx={{ mb: 2 }}>{scan.failure_summary}</Alert>}
      {scan.coverage_status && !collectionActive && scan.coverage_status !== "complete" && (
        <Alert severity="warning" sx={{ mb: 2 }}>
          Detector coverage was {titleCase(scan.coverage_status)}. Incident results may be incomplete and unresolved conditions were preserved.
        </Alert>
      )}
      {analysisActive && !collectionActive && (
        <Alert severity="info" sx={{ mb: 2 }}>
          Collection is complete. Agent analysis is still running for {analysis?.active_count ?? 0} selected investigation{analysis?.active_count === 1 ? "" : "s"}.
        </Alert>
      )}

      <Paper variant="outlined" sx={{ mb: 2.5, overflow: "hidden" }}>
        <Box sx={{ p: { xs: 2, md: 2.5 } }}>
          <Stack direction={{ xs: "column", md: "row" }} gap={2.5} justifyContent="space-between">
            <Stack direction="row" gap={1} alignItems="center" flexWrap="wrap">
              <StatusChip value={workflow.value} size="medium" />
              <Chip label={scanStageLabel(scan.stage)} variant="outlined" />
              <Typography variant="body2" color="text.secondary">
                Attempt {scan.attempt_count || 1}
              </Typography>
            </Stack>
            <Stack direction={{ xs: "column", sm: "row" }} gap={{ xs: 0.5, sm: 3 }}>
              <Meta label="Created" value={formatDate(scan.created_at)} />
              <Meta label="Started" value={formatDate(scan.started_at)} />
              <Meta label="Collection ended" value={formatDate(scan.completed_at)} />
            </Stack>
          </Stack>
          <Box sx={{ mt: 2.25 }}>
            <Stack direction="row" justifyContent="space-between" gap={2} sx={{ mb: 0.75 }}>
              <Typography variant="caption" color="text.secondary">Collection</Typography>
              <Typography variant="caption" color="text.secondary">{collectionActive ? scanStageLabel(scan.stage) : titleCase(scan.status)}</Typography>
            </Stack>
            <LinearProgress
              variant="determinate"
              value={collectionActive ? Math.max(4, progress) : 100}
              sx={{ height: 6, "& .MuiLinearProgress-bar": { transition: "none" } }}
            />
            {showAnalysis && (
              <Box sx={{ mt: 1.75 }}>
                <Stack direction="row" justifyContent="space-between" gap={2} sx={{ mb: 0.75 }}>
                  <Typography variant="caption" color="text.secondary">Agent analysis</Typography>
                  <Typography variant="caption" color="text.secondary">
                    {analysis?.succeeded_count ?? 0} succeeded · {analysis?.failed_count ?? 0} failed · {analysis?.active_count ?? 0} active
                  </Typography>
                </Stack>
                <LinearProgress
                  color={analysis?.failed_count || analysis?.budget_deferred_count ? "warning" : "primary"}
                  variant="determinate"
                  value={analysisProgress(analysis)}
                  sx={{ height: 6, "& .MuiLinearProgress-bar": { transition: "none" } }}
                />
              </Box>
            )}
          </Box>
        </Box>
        <Divider />
        <Box sx={{ px: { xs: 2, md: 2.5 }, py: 1.5, bgcolor: "#f8f9fa" }}>
          <Stack direction="row" gap={1} flexWrap="wrap">
            {scan.categories.length ? scan.categories.map((category) => <Chip key={category} size="small" label={titleCase(category)} />) : <Chip size="small" label="All categories" />}
          </Stack>
        </Box>
      </Paper>

      {showAnalysis && (
        <Box sx={{ mb: 2.5 }}>
          <Stack direction={{ xs: "column", sm: "row" }} justifyContent="space-between" alignItems={{ xs: "flex-start", sm: "center" }} gap={1} sx={{ mb: 1.25 }}>
            <Box>
              <Typography variant="h6">Agent selection and analysis</Typography>
              <Typography variant="body2" color="text.secondary">
                {analysis?.candidate_count ?? 0} incident candidates evaluated; {analysis?.selected_count ?? 0} selected for agent investigation.
              </Typography>
            </Box>
            <StatusChip value={analysis?.status ?? "not_started"} />
          </Stack>

          <Box
            sx={{
              display: "grid",
              gridTemplateColumns: { xs: "repeat(2, minmax(0, 1fr))", sm: "repeat(3, minmax(0, 1fr))", lg: "repeat(5, minmax(0, 1fr))" },
              border: "1px solid",
              borderColor: "divider",
              borderRadius: 1,
              overflow: "hidden",
              mb: 1.5,
            }}
          >
            <AnalysisMetric label="Candidates" value={analysis?.candidate_count ?? 0} />
            <AnalysisMetric label="Selected" value={analysis?.selected_count ?? 0} />
            <AnalysisMetric label="Queued" value={analysis?.queued_count ?? 0} />
            <AnalysisMetric label="Running" value={analysis?.running_count ?? 0} />
            <AnalysisMetric label="Succeeded" value={analysis?.succeeded_count ?? 0} />
            <AnalysisMetric label="Failed" value={analysis?.failed_count ?? 0} tone="danger" />
            <AnalysisMetric label="Reused" value={analysis?.reused_count ?? 0} />
            <AnalysisMetric label="Deferred" value={analysis?.deferred_count ?? 0} />
            <AnalysisMetric label="Manual only" value={analysis?.manual_only_count ?? 0} />
            <AnalysisMetric label="Budget deferred" value={analysis?.budget_deferred_count ?? 0} tone="warning" />
          </Box>

          <TableContainer component={Paper} variant="outlined">
            <Table sx={{ minWidth: 860 }}>
              <TableHead><TableRow><TableCell>Incident</TableCell><TableCell width={130}>Decision</TableCell><TableCell>Reason</TableCell><TableCell width={130}>Agent status</TableCell><TableCell width={190}>Result</TableCell></TableRow></TableHead>
              <TableBody>
                {!analysis?.items?.length ? (
                  <TableRow><TableCell colSpan={5}><Typography color="text.secondary" sx={{ py: 2, textAlign: "center" }}>No incident candidates were recorded for this scan</Typography></TableCell></TableRow>
                ) : (analysis.items ?? []).map((item) => (
                  <TableRow hover key={item.incident_id} onClick={() => navigate(`/incidents/${item.incident_id}`)} sx={{ cursor: "pointer" }}>
                    <TableCell>
                      <Typography variant="body2" fontWeight={650}>{item.incident_title}</Typography>
                      <Typography variant="caption" color="text.secondary">{titleCase(item.severity)} · {item.incident_id}</Typography>
                    </TableCell>
                    <TableCell><StatusChip value={item.outcome} /></TableCell>
                    <TableCell>
                      <Typography variant="body2">{item.reason}</Typography>
                      <Typography variant="caption" color="text.secondary">{titleCase(item.reason_code)}</Typography>
                    </TableCell>
                    <TableCell>{item.request_status ? <StatusChip value={item.request_status} /> : <Typography color="text.secondary">-</Typography>}</TableCell>
                    <TableCell>
                      <Typography variant="body2" color={item.error_summary ? "error.main" : "text.secondary"}>
                        {item.error_summary || (item.investigation_id ? "Investigation available" : "-")}
                      </Typography>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableContainer>
        </Box>
      )}

      {showAnalysis && (
        <Box sx={{ mb: 2.5, py: 1.5, borderTop: "1px solid", borderBottom: "1px solid", borderColor: "divider" }}>
          <Typography variant="h6" sx={{ mb: 1 }}>Live agent cost</Typography>
          <Stack direction={{ xs: "column", sm: "row" }} gap={{ xs: 1, sm: 4 }}>
            <Meta label="Model calls" value={String(llmUsage.call_count ?? 0)} />
            <Meta label="Input" value={formatTokens(llmUsage.prompt_tokens)} />
            <Meta label="Output" value={formatTokens(llmUsage.completion_tokens)} />
            <Meta label="Cache read" value={formatTokens(llmUsage.cache_read_input_tokens)} />
            <Meta label="Estimated cost" value={formatUsd(llmUsage.estimated_cost_usd)} />
          </Stack>
        </Box>
      )}

      <Typography variant="h6" sx={{ mb: 1.25 }}>Detector coverage</Typography>
      <TableContainer component={Paper} variant="outlined" sx={{ mb: 2.5 }}>
        <Table sx={{ minWidth: 760 }}>
          <TableHead><TableRow><TableCell>Check</TableCell><TableCell>Coverage</TableCell><TableCell>Status</TableCell><TableCell>Findings</TableCell><TableCell>Duration</TableCell></TableRow></TableHead>
          <TableBody>
            {(scan.checks ?? []).length === 0 ? (
              <TableRow><TableCell colSpan={5}><Typography color="text.secondary" sx={{ py: 2, textAlign: "center" }}>Checks have not started</Typography></TableCell></TableRow>
            ) : (scan.checks ?? []).map((check) => (
              <TableRow key={check.name}>
                <TableCell>
                  <Typography variant="body2" fontWeight={650}>{titleCase(check.name)}</Typography>
                  {check.failure_summary && <Typography variant="caption" color="error.main">{check.failure_summary}</Typography>}
                </TableCell>
                <TableCell>{check.categories.map(titleCase).join(", ")}</TableCell>
                <TableCell><StatusChip value={check.status} /></TableCell>
                <TableCell>{check.finding_count}</TableCell>
                <TableCell>{checkDuration(check.started_at, check.completed_at)}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </TableContainer>

      <Stack direction="row" justifyContent="space-between" alignItems="center" sx={{ mb: 1.25 }}>
        <Typography variant="h6">Event timeline</Typography>
        <Typography variant="caption" color={connection === "error" ? "error.main" : "text.secondary"}>
          {workflowActive ? (connection === "live" ? "Live" : titleCase(connection)) : `${events.length} events`}
        </Typography>
      </Stack>
      <Paper variant="outlined" sx={{ overflow: "hidden" }}>
        {events.length === 0 ? (
          <Box sx={{ p: 4, textAlign: "center" }}><Typography color="text.secondary">No events recorded</Typography></Box>
        ) : events.map((event, index) => (
          <Box key={`${event.sequence}-${event.type}`} sx={{ px: { xs: 2, md: 2.5 }, py: 1.75, borderTop: index ? "1px solid" : 0, borderColor: "divider" }}>
            <Stack direction={{ xs: "column", sm: "row" }} gap={{ xs: 0.5, sm: 2 }}>
              <Typography variant="caption" color="text.secondary" sx={{ width: 80, flexShrink: 0 }}>
                #{event.sequence}
              </Typography>
              <Box sx={{ minWidth: 0, flex: 1 }}>
                <Typography variant="body2" fontWeight={650}>{titleCase(event.type)}</Typography>
                <Typography variant="caption" color="text.secondary">{formatDate(event.occurred_at)}</Typography>
                {Object.keys(event.payload).length > 0 && (
                  <Typography component="pre" variant="caption" sx={{ mt: 0.75, mb: 0, whiteSpace: "pre-wrap", overflowWrap: "anywhere", color: "text.secondary", fontFamily: "ui-monospace, monospace" }}>
                    {JSON.stringify(event.payload, null, 2)}
                  </Typography>
                )}
              </Box>
            </Stack>
          </Box>
        ))}
      </Paper>
    </Box>
  );
}

function Meta({ label, value }: { label: string; value: string }) {
  return (
    <Box>
      <Typography variant="caption" color="text.secondary">{label}</Typography>
      <Typography variant="body2" fontWeight={600}>{value}</Typography>
    </Box>
  );
}

function AnalysisMetric({ label, value, tone }: { label: string; value: number; tone?: "danger" | "warning" }) {
  const color = tone === "danger" ? "error.main" : tone === "warning" ? "warning.main" : "text.primary";
  return (
    <Box sx={{ minWidth: 0, px: 1.75, py: 1.5, borderRight: "1px solid", borderBottom: "1px solid", borderColor: "divider" }}>
      <Typography variant="caption" color="text.secondary">{label}</Typography>
      <Typography variant="h6" color={color}>{value.toLocaleString()}</Typography>
    </Box>
  );
}

function checkDuration(startedAt: string | null, completedAt: string | null): string {
  if (!startedAt) return "-";
  const end = completedAt ? new Date(completedAt).getTime() : Date.now();
  const seconds = Math.max(0, Math.round((end - new Date(startedAt).getTime()) / 1000));
  return `${seconds}s`;
}
