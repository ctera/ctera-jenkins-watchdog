import {
  Alert,
  Box,
  Button,
  Chip,
  Divider,
  IconButton,
  LinearProgress,
  MenuItem,
  Paper,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  TablePagination,
  TextField,
  Tooltip,
  Typography,
} from "@mui/material";
import ArrowBackIcon from "@mui/icons-material/ArrowBack";
import CancelOutlinedIcon from "@mui/icons-material/CancelOutlined";
import OpenInNewIcon from "@mui/icons-material/OpenInNew";
import { useCallback, useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import PageHeader from "../components/PageHeader";
import { ErrorPanel, LoadingPanel } from "../components/StatePanel";
import StatusChip from "../components/StatusChip";
import { useScanEvents } from "../hooks/useScanEvents";
import { ApiError, cancelScan, getScan, getScanJenkinsFailures, type JenkinsFailureReport, type Scan } from "../services/api";
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
  const [jenkinsFailures, setJenkinsFailures] = useState<JenkinsFailureReport | null>(null);
  const [failurePage, setFailurePage] = useState(0);
  const [failureRowsPerPage, setFailureRowsPerPage] = useState(50);
  const [failureStatus, setFailureStatus] = useState("");
  const [failureJobInput, setFailureJobInput] = useState("");
  const [failureJob, setFailureJob] = useState("");

  const refresh = useCallback(async () => {
    if (!scanId) return;
    try {
      const nextScan = await getScan(scanId);
      let nextFailures = (nextScan.jenkins_failures as JenkinsFailureReport | null | undefined) ?? null;
      if (nextFailures && (failurePage > 0 || failureRowsPerPage !== 50 || failureStatus || failureJob)) {
        nextFailures = await getScanJenkinsFailures(
          scanId,
          failurePage * failureRowsPerPage,
          failureRowsPerPage,
          { status: failureStatus || undefined, job: failureJob || undefined },
        );
      }
      setScan(nextScan);
      setJenkinsFailures(nextFailures);
      setError(null);
    } catch (requestError) {
      if (requestError instanceof ApiError && requestError.status === 404) setJenkinsFailures(null);
      else setError(requestError);
    } finally {
      setLoading(false);
    }
  }, [failureJob, failurePage, failureRowsPerPage, failureStatus, scanId]);

  useEffect(() => void refresh(), [refresh]);
  const { events, connection } = useScanEvents(scanId, refresh, false);

  useEffect(() => {
    if (events.length) void refresh();
  }, [events.length, refresh]);

  const failureWorkflowActive = Boolean(
    jenkinsFailures && ["collecting", "investigating", "waiting_budget"].includes(jenkinsFailures.status),
  );
  const workflowActive = Boolean(scan && (isCollectionActive(scan) || isAnalysisActive(scan) || failureWorkflowActive));
  useEffect(() => {
    if (!workflowActive) return;
    const timer = window.setInterval(
      () => void refresh(),
      jenkinsFailures?.status === "waiting_budget" ? 60_000 : 2_500,
    );
    return () => window.clearInterval(timer);
  }, [jenkinsFailures?.status, refresh, workflowActive]);

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
  const workflow = jenkinsFailures
    ? reportWorkflowStatus(jenkinsFailures.status)
    : scanWorkflowStatus(scan);
  const progress = ((STAGES.indexOf(scan.stage) + 1) / STAGES.length) * 100;
  const llmUsage = scan.llm_usage ?? {};
  const analysis = scan.analysis;
  const failedBuildInventory = scanFailedBuildInventory(scan);
  const costBudgetDeferred = analysis?.budget_metric === "cost_usd";
  const allBudgetDeferred = Boolean(
    analysis?.status === "budget_deferred"
    || (
      analysis?.candidate_count
      && analysis.budget_deferred_count === analysis.candidate_count
      && analysis.selected_count === 0
    ),
  );
  const showAnalysis = !jenkinsFailures && Boolean(
    analysis?.candidate_count
      || analysis?.selected_count
      || (analysis && analysis.status !== "not_started")
      || !collectionActive,
  );
  const reportCompleted = jenkinsFailures ? terminalReportBuildCount(jenkinsFailures) : 0;

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
      {!jenkinsFailures && analysisActive && !collectionActive && (
        <Alert severity="info" sx={{ mb: 2 }}>
          Collection is complete. Agent analysis is still running for {analysis?.active_count ?? 0} selected investigation{analysis?.active_count === 1 ? "" : "s"}.
        </Alert>
      )}
      {!jenkinsFailures && allBudgetDeferred && (
        <Alert severity="warning" sx={{ mb: 2 }}>
          <Typography variant="body2" fontWeight={700}>
            Agent analysis was skipped: daily automatic {costBudgetDeferred ? "cost" : "token"} budget exhausted.
          </Typography>
          <Typography variant="body2">
            {analysis?.budget_deferred_count ?? 0} of {analysis?.candidate_count ?? 0} candidates were deferred before an investigation was queued.
            {costBudgetDeferred && analysis?.budget_limit_usd != null
              ? ` Projected usage was ${formatUsd(analysis.budget_projected_usd)} against the ${formatUsd(analysis.budget_limit_usd)} automatic allowance.`
              : ""}
            {` This scan used ${Number(llmUsage.call_count ?? 0).toLocaleString()} model calls and ${formatUsd(llmUsage.estimated_cost_usd)}.`}
            {analysis?.budget_reset_at
              ? ` A later scan after ${formatDate(analysis.budget_reset_at)} can reconsider them.`
              : " A later scan can reconsider them when budget is available."}
          </Typography>
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
            {jenkinsFailures ? (
              <Box sx={{ mt: 1.75 }}>
                <Stack direction="row" justifyContent="space-between" gap={2} sx={{ mb: 0.75 }}>
                  <Typography variant="caption" color="text.secondary">Failed-build investigations</Typography>
                  <Typography variant="caption" color="text.secondary">
                    {jenkinsFailures.status === "failed"
                      ? "Collection failed"
                      : `${reportCompleted} of ${jenkinsFailures.failures_found} terminal`}
                  </Typography>
                </Stack>
                <LinearProgress
                  color={jenkinsFailures.status === "failed" || jenkinsFailures.status === "waiting_budget" ? "warning" : "primary"}
                  variant="determinate"
                  value={jenkinsFailures.failures_found
                    ? (reportCompleted / jenkinsFailures.failures_found) * 100
                    : jenkinsFailures.status === "collecting" ? 4 : 100}
                  sx={{ height: 6, "& .MuiLinearProgress-bar": { transition: "none" } }}
                />
              </Box>
            ) : showAnalysis && (
              <Box sx={{ mt: 1.75 }}>
                <Stack direction="row" justifyContent="space-between" gap={2} sx={{ mb: 0.75 }}>
                  <Typography variant="caption" color="text.secondary">Agent analysis</Typography>
                  <Typography variant="caption" color="text.secondary">
                    {allBudgetDeferred
                      ? `Not run · ${analysis?.budget_deferred_count ?? 0} budget deferred`
                      : `${analysis?.succeeded_count ?? 0} complete · ${analysis?.partial_count ?? 0} partial · ${analysis?.failed_count ?? 0} failed · ${analysis?.active_count ?? 0} active`}
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

      {jenkinsFailures && (
        <JenkinsFailureInventory
          report={jenkinsFailures}
          jobInput={failureJobInput}
          status={failureStatus}
          page={failurePage}
          rowsPerPage={failureRowsPerPage}
          onJobInputChange={setFailureJobInput}
          onApplyJob={() => { setFailurePage(0); setFailureJob(failureJobInput.trim()); }}
          onStatusChange={(value) => { setFailurePage(0); setFailureStatus(value); }}
          onPageChange={setFailurePage}
          onRowsPerPageChange={(value) => { setFailurePage(0); setFailureRowsPerPage(value); }}
          onOpenBuild={(buildId) => navigate(`/jenkins/builds/${buildId}?scan=${encodeURIComponent(scan.id)}`)}
        />
      )}

      {showAnalysis && (
        <Box sx={{ mb: 2.5 }}>
          <Stack direction={{ xs: "column", sm: "row" }} justifyContent="space-between" alignItems={{ xs: "flex-start", sm: "center" }} gap={1} sx={{ mb: 1.25 }}>
            <Box>
              <Typography variant="h6">Agent selection and analysis</Typography>
              <Typography variant="body2" color="text.secondary">
                {allBudgetDeferred
                  ? `${analysis?.candidate_count ?? 0} incident candidates considered; all were deferred before agent investigation.`
                  : `${analysis?.candidate_count ?? 0} incident candidates considered; ${analysis?.selected_count ?? 0} admitted for agent investigation.`}
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
            {allBudgetDeferred ? (
              <>
                <AnalysisMetric label="Considered" value={analysis?.candidate_count ?? 0} />
                <AnalysisMetric label="Queued" value={analysis?.queued_count ?? 0} />
                <AnalysisMetric label="Budget deferred" value={analysis?.budget_deferred_count ?? 0} tone="warning" />
              </>
            ) : (
              <>
                <AnalysisMetric label="Considered" value={analysis?.candidate_count ?? 0} />
                <AnalysisMetric label="Admitted" value={analysis?.selected_count ?? 0} />
                <AnalysisMetric label="Queued" value={analysis?.queued_count ?? 0} />
                <AnalysisMetric label="Running" value={analysis?.running_count ?? 0} />
                <AnalysisMetric label="Complete" value={analysis?.succeeded_count ?? 0} />
                <AnalysisMetric label="Partial" value={analysis?.partial_count ?? 0} tone="warning" />
                <AnalysisMetric label="Failed" value={analysis?.failed_count ?? 0} tone="danger" />
                <AnalysisMetric label="Reused" value={analysis?.reused_count ?? 0} />
                <AnalysisMetric label="Deferred" value={analysis?.deferred_count ?? 0} />
                <AnalysisMetric label="Manual only" value={analysis?.manual_only_count ?? 0} />
                <AnalysisMetric label="Budget deferred" value={analysis?.budget_deferred_count ?? 0} tone="warning" />
              </>
            )}
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
                    <TableCell>{item.investigation_status || item.request_status ? <StatusChip value={item.investigation_status ?? item.request_status ?? "unknown"} /> : <Typography color="text.secondary">-</Typography>}</TableCell>
                    <TableCell>
                      <Typography variant="body2" color={item.investigation_status === "partial" ? "warning.main" : item.error_summary ? "error.main" : "text.secondary"}>
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
          <Typography variant="h6" sx={{ mb: 1 }}>Agent cost for this scan</Typography>
          <Stack direction={{ xs: "column", sm: "row" }} gap={{ xs: 1, sm: 4 }}>
            <Meta label="Model calls" value={String(llmUsage.call_count ?? 0)} />
            <Meta label="Input" value={formatTokens(llmUsage.prompt_tokens)} />
            <Meta label="Output" value={formatTokens(llmUsage.completion_tokens)} />
            <Meta label="Cache read" value={formatTokens(llmUsage.cache_read_input_tokens)} />
            <Meta label="Estimated cost" value={formatUsd(llmUsage.estimated_cost_usd)} />
          </Stack>
        </Box>
      )}

      {!jenkinsFailures && failedBuildInventory && (
        <Box sx={{ mb: 2.5 }}>
          <Typography variant="h6">Failed builds in window</Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 1.25 }}>
            {failedBuildInventory.rows.length.toLocaleString()} failed build{failedBuildInventory.rows.length === 1 ? "" : "s"} across {failedBuildInventory.jobCount.toLocaleString()} job{failedBuildInventory.jobCount === 1 ? "" : "s"} in the {failedBuildInventory.windowHours}-hour collection window.
          </Typography>
          <TableContainer component={Paper} variant="outlined" sx={{ maxHeight: 440 }}>
            <Table stickyHeader size="small" sx={{ minWidth: 760 }}>
              <TableHead><TableRow><TableCell>Job</TableCell><TableCell width={100}>Build</TableCell><TableCell width={110}>Result</TableCell><TableCell width={190}>Started</TableCell><TableCell width={110}>Duration</TableCell><TableCell width={56} aria-label="Jenkins link" /></TableRow></TableHead>
              <TableBody>
                {failedBuildInventory.rows.length === 0 ? (
                  <TableRow><TableCell colSpan={6}><Typography color="text.secondary" sx={{ py: 2, textAlign: "center" }}>No failed Jenkins builds were found in this window</Typography></TableCell></TableRow>
                ) : failedBuildInventory.rows.map((build) => (
                  <TableRow key={`${build.jobName}#${build.buildNumber}`}>
                    <TableCell><Typography variant="body2" fontWeight={650}>{build.jobName}</Typography></TableCell>
                    <TableCell>#{build.buildNumber}</TableCell>
                    <TableCell><StatusChip value={build.result.toLowerCase()} /></TableCell>
                    <TableCell>{formatDate(new Date(build.timestampMs).toISOString())}</TableCell>
                    <TableCell>{build.durationMinutes.toLocaleString()}m</TableCell>
                    <TableCell>
                      {build.url ? (
                        <Tooltip title="Open in Jenkins">
                          <IconButton
                            component="a"
                            href={build.url}
                            target="_blank"
                            rel="noreferrer"
                            size="small"
                            aria-label={`Open ${build.jobName} build ${build.buildNumber} in Jenkins`}
                          >
                            <OpenInNewIcon fontSize="small" />
                          </IconButton>
                        </Tooltip>
                      ) : null}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableContainer>
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

function JenkinsFailureInventory({
  report,
  jobInput,
  status,
  page,
  rowsPerPage,
  onJobInputChange,
  onApplyJob,
  onStatusChange,
  onPageChange,
  onRowsPerPageChange,
  onOpenBuild,
}: {
  report: JenkinsFailureReport;
  jobInput: string;
  status: string;
  page: number;
  rowsPerPage: number;
  onJobInputChange: (value: string) => void;
  onApplyJob: () => void;
  onStatusChange: (value: string) => void;
  onPageChange: (value: number) => void;
  onRowsPerPageChange: (value: number) => void;
  onOpenBuild: (buildId: string) => void;
}) {
  const completed = terminalReportBuildCount(report);
  return (
    <Box sx={{ mb: 2.5 }}>
      <Stack direction={{ xs: "column", md: "row" }} justifyContent="space-between" gap={1.5} sx={{ mb: 1.25 }}>
        <Box>
          <Typography variant="h6">Failed Jenkins builds</Typography>
          <Typography variant="body2" color="text.secondary">
            {report.status === "failed" && !report.collected_at
              ? `Jenkins coverage could not be established for the window from ${formatDate(report.window_started_at)} to ${formatDate(report.window_ended_at)}. No zero-failure conclusion was recorded.`
              : `${report.failures_found.toLocaleString()} failed build${report.failures_found === 1 ? "" : "s"} found across ${report.jobs_discovered.toLocaleString()} checked jobs between ${formatDate(report.window_started_at)} and ${formatDate(report.window_ended_at)}.`}
          </Typography>
        </Box>
        <Stack direction="row" gap={0.75} flexWrap="wrap" alignItems="center">
          <StatusChip value={report.status} />
          <Chip size="small" variant="outlined" label={`${completed} of ${report.failures_found} analyzed`} />
          {Object.entries(report.counts).map(([value, count]) => (
            <Chip key={value} size="small" label={`${count} ${titleCase(value)}`} color={value === "explained" ? "success" : value === "waiting_budget" ? "warning" : "default"} />
          ))}
        </Stack>
      </Stack>

      {report.budget_reset_at && (
        <Alert severity="warning" sx={{ mb: 1.25 }}>
          Agent investigations are paused by the daily budget and will resume automatically after {formatDate(report.budget_reset_at)}. Every failed build remains in this scan.
        </Alert>
      )}
      {report.status === "failed" && (
        <Alert severity="error" sx={{ mb: 1.25 }}>
          Jenkins collection failed before this scan could establish complete build coverage
          {report.error_summary ? `: ${report.error_summary}` : "."}
        </Alert>
      )}
      {report.coverage_exceptions.length > 0 && (
        <Alert severity="warning" sx={{ mb: 1.25 }}>
          {report.coverage_exceptions.length.toLocaleString()} Jenkins job coverage exception{report.coverage_exceptions.length === 1 ? "" : "s"} recorded for this scan.
        </Alert>
      )}

      <Stack direction={{ xs: "column", sm: "row" }} gap={1} sx={{ mb: 1.25 }}>
        <TextField
          size="small"
          label="Filter job"
          value={jobInput}
          onChange={(event) => onJobInputChange(event.target.value)}
          onKeyDown={(event) => { if (event.key === "Enter") onApplyJob(); }}
          sx={{ minWidth: { sm: 320 } }}
        />
        <Button variant="outlined" onClick={onApplyJob}>Apply</Button>
        <TextField
          select
          size="small"
          label="Agent status"
          value={status}
          onChange={(event) => onStatusChange(event.target.value)}
          sx={{ minWidth: 180 }}
        >
          <MenuItem value="">All statuses</MenuItem>
          {["queued", "running", "waiting_budget", "explained", "evidence_gap", "agent_failed", "cancelled"].map((value) => (
            <MenuItem key={value} value={value}>{titleCase(value)}</MenuItem>
          ))}
        </TextField>
      </Stack>

      <TableContainer component={Paper} variant="outlined">
        <Table size="small" sx={{ minWidth: 900 }}>
          <TableHead>
            <TableRow>
              <TableCell>Job</TableCell>
              <TableCell width={100}>Result</TableCell>
              <TableCell width={180}>Started</TableCell>
              <TableCell width={110}>Duration</TableCell>
              <TableCell width={140}>Source</TableCell>
              <TableCell width={150}>Agent analysis</TableCell>
              <TableCell width={56} aria-label="Jenkins link" />
            </TableRow>
          </TableHead>
          <TableBody>
            {report.builds.length === 0 ? (
              <TableRow><TableCell colSpan={7}><Typography color="text.secondary" sx={{ py: 2, textAlign: "center" }}>No failed builds match these filters</Typography></TableCell></TableRow>
            ) : report.builds.map((build) => (
              <TableRow
                hover
                key={build.id}
                tabIndex={0}
                onClick={() => onOpenBuild(build.build_id)}
                onKeyDown={(event) => { if (event.key === "Enter") onOpenBuild(build.build_id); }}
                sx={{ cursor: "pointer" }}
              >
                <TableCell>
                  <Typography variant="body2" fontWeight={650}>{build.job_name}</Typography>
                  <Typography variant="caption" color="text.secondary">Build #{build.build_number}</Typography>
                </TableCell>
                <TableCell><StatusChip value={build.result.toLowerCase()} /></TableCell>
                <TableCell>{formatDate(build.started_at)}</TableCell>
                <TableCell>{buildDuration(build.duration_ms)}</TableCell>
                <TableCell>{String(build.source.change_number ?? build.source.repository ?? "-")}</TableCell>
                <TableCell><StatusChip value={build.status} /></TableCell>
                <TableCell>
                  <Tooltip title="Open in Jenkins">
                    <IconButton
                      component="a"
                      href={build.url}
                      target="_blank"
                      rel="noreferrer"
                      size="small"
                      aria-label={`Open ${build.job_name} build ${build.build_number} in Jenkins`}
                      onClick={(event) => event.stopPropagation()}
                    >
                      <OpenInNewIcon fontSize="small" />
                    </IconButton>
                  </Tooltip>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
        <TablePagination
          component="div"
          count={report.total_builds}
          page={page}
          rowsPerPage={rowsPerPage}
          rowsPerPageOptions={[25, 50, 100]}
          onPageChange={(_, value) => onPageChange(value)}
          onRowsPerPageChange={(event) => onRowsPerPageChange(Number(event.target.value))}
        />
      </TableContainer>
    </Box>
  );
}

type FailedBuildRow = {
  jobName: string;
  buildNumber: number;
  result: string;
  timestampMs: number;
  durationMinutes: number;
  url: string;
};

function scanFailedBuildInventory(scan: Scan): { rows: FailedBuildRow[]; jobCount: number; windowHours: number } | null {
  const execution = (scan.checks ?? []).find((check) => check.name === "jenkins_failed_builds");
  if (!execution || execution.status !== "succeeded") return null;
  const rawRows = execution.summary.recent_failed_builds;
  const rows = Array.isArray(rawRows)
    ? rawRows.flatMap((value): FailedBuildRow[] => {
      if (!value || typeof value !== "object" || Array.isArray(value)) return [];
      const item = value as Record<string, unknown>;
      const jobName = typeof item.job_name === "string" ? item.job_name : "";
      const buildNumber = Number(item.build_number);
      const timestampMs = Number(item.timestamp_ms);
      if (!jobName || !Number.isFinite(buildNumber) || !Number.isFinite(timestampMs)) return [];
      return [{
        jobName,
        buildNumber,
        result: typeof item.result === "string" ? item.result : "failure",
        timestampMs,
        durationMinutes: Number.isFinite(Number(item.duration_minutes)) ? Number(item.duration_minutes) : 0,
        url: typeof item.url === "string" ? item.url : "",
      }];
    })
    : [];
  const jobCount = Number(execution.summary.failed_job_count);
  const windowHours = Number(execution.summary.window_hours);
  return {
    rows,
    jobCount: Number.isFinite(jobCount) ? jobCount : new Set(rows.map((item) => item.jobName)).size,
    windowHours: Number.isFinite(windowHours) ? windowHours : 4,
  };
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

function buildDuration(milliseconds: number): string {
  const minutes = milliseconds / 60_000;
  if (minutes >= 60) return `${(minutes / 60).toFixed(1)}h`;
  if (minutes >= 1) return `${Math.round(minutes)}m`;
  return `${Math.max(0, Math.round(milliseconds / 1000))}s`;
}

function terminalReportBuildCount(report: JenkinsFailureReport): number {
  return ["explained", "evidence_gap", "agent_failed", "cancelled"]
    .reduce((total, key) => total + (report.counts[key] ?? 0), 0);
}

function reportWorkflowStatus(status: string): { value: string; label: string } {
  if (status === "collecting") return { value: "scanning", label: "Scanning" };
  if (status === "investigating") return { value: "analyzing", label: "Analyzing" };
  if (status === "waiting_budget") return { value: "waiting_budget", label: "Waiting budget" };
  if (status === "failed" || status === "cancelled") return { value: "complete_with_issues", label: "Complete with issues" };
  return { value: "complete", label: "Complete" };
}
