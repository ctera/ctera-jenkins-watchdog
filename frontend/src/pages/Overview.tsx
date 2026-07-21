import {
  Alert,
  Box,
  Button,
  Chip,
  Collapse,
  CircularProgress,
  Divider,
  IconButton,
  InputAdornment,
  Link,
  MenuItem,
  Paper,
  Stack,
  Tab,
  Tabs,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  TextField,
  ToggleButton,
  ToggleButtonGroup,
  Tooltip,
  Typography,
} from "@mui/material";
import ChevronRightIcon from "@mui/icons-material/ChevronRight";
import ExpandMoreIcon from "@mui/icons-material/ExpandMore";
import OpenInNewIcon from "@mui/icons-material/OpenInNew";
import RefreshIcon from "@mui/icons-material/Refresh";
import SearchIcon from "@mui/icons-material/Search";
import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import PageHeader from "../components/PageHeader";
import { SourceSummary } from "../components/SourceAttribution";
import { ErrorPanel, LoadingPanel } from "../components/StatePanel";
import StatusChip from "../components/StatusChip";
import {
  getJenkinsWorkspace,
  getOverview,
  listJenkinsFailures,
  type JenkinsBuild,
  type JenkinsExecution,
  type JenkinsFailurePattern,
  type JenkinsJobFamily,
  type JenkinsMultibranchFamily,
  type JenkinsWorkspace,
  type Overview as OperationalOverview,
} from "../services/api";
import { formatRelative, formatTokens, formatUsd, titleCase } from "../utils/format";

type JenkinsView = "new" | "executions" | "recurring" | "all" | "jobs" | "multibranch";
type FailureResult = "FAILURE" | "UNSTABLE" | "ABORTED";
type UnknownRecord = Record<string, unknown>;

interface JenkinsDataset {
  workspace: JenkinsWorkspace;
  operational: OperationalOverview;
  failures: JenkinsBuild[];
  failureCursor: string | null;
  failureTotal: number;
}

const windows = [
  { label: "4h", value: 4 },
  { label: "24h", value: 24 },
  { label: "7d", value: 168 },
  { label: "30d", value: 720 },
];

export default function OverviewPage() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const windowHours = parseWindow(searchParams.get("window"));
  const view = parseView(searchParams.get("view"));
  const [dataset, setDataset] = useState<JenkinsDataset | null>(null);
  const [queryText, setQueryText] = useState("");
  const [jobQuery, setJobQuery] = useState("");
  const [result, setResult] = useState<FailureResult | "">("");
  const [initialLoading, setInitialLoading] = useState(true);
  const [transitioning, setTransitioning] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [failureLoadingMore, setFailureLoadingMore] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [expandedFamily, setExpandedFamily] = useState<string | null>(null);
  const [reloadVersion, setReloadVersion] = useState(0);
  const requestSequence = useRef(0);
  const cache = useRef(new Map<string, JenkinsDataset>());
  const datasetRef = useRef<JenkinsDataset | null>(null);
  const currentKey = useRef("");

  const updateLocation = useCallback((updates: Record<string, string>) => {
    setSearchParams((current) => {
      const next = new URLSearchParams(current);
      Object.entries(updates).forEach(([name, value]) => next.set(name, value));
      return next;
    });
  }, [setSearchParams]);

  useEffect(() => {
    if (searchParams.get("window") === String(windowHours) && searchParams.get("view") === view) return;
    const next = new URLSearchParams(searchParams);
    next.set("window", String(windowHours));
    next.set("view", view);
    setSearchParams(next, { replace: true });
  }, [searchParams, setSearchParams, view, windowHours]);

  const failureFilters = useMemo(
    () => view === "new"
      ? { view: "new" as const }
      : {
          view: "all" as const,
          job: view === "all" ? jobQuery || undefined : undefined,
          result: view === "all" ? result || undefined : undefined,
        },
    [jobQuery, result, view],
  );
  const datasetKey = `${windowHours}:${view}:${failureFilters.job ?? ""}:${failureFilters.result ?? ""}`;

  useEffect(() => {
    void reloadVersion;
    const sequence = ++requestSequence.current;
    const controller = new AbortController();
    currentKey.current = datasetKey;
    const cached = cache.current.get(datasetKey);
    if (cached) {
      datasetRef.current = cached;
      setDataset(cached);
      setInitialLoading(false);
      setTransitioning(false);
    } else {
      setTransitioning(datasetRef.current !== null);
      setInitialLoading(datasetRef.current === null);
    }
    setRefreshing(true);
    setFailureLoadingMore(false);
    setError(null);

    void Promise.all([
      getJenkinsWorkspace(windowHours, 100, controller.signal),
      listJenkinsFailures(windowHours, failureFilters, 100, undefined, controller.signal),
      getOverview(controller.signal),
    ])
      .then(([workspace, failurePage, operational]) => {
        if (sequence !== requestSequence.current) return;
        const next: JenkinsDataset = {
          workspace,
          operational,
          failures: failurePage.items,
          failureCursor: failurePage.next_cursor,
          failureTotal: failurePage.total_count,
        };
        cache.current.set(datasetKey, next);
        datasetRef.current = next;
        setDataset(next);
        setInitialLoading(false);
        setTransitioning(false);
        setError(null);
      })
      .catch((requestError) => {
        if (sequence === requestSequence.current && (!(requestError instanceof DOMException) || requestError.name !== "AbortError")) {
          setError(requestError);
          setInitialLoading(false);
          setTransitioning(!cached && datasetRef.current !== null);
        }
      })
      .finally(() => {
        if (sequence === requestSequence.current) setRefreshing(false);
      });
    return () => controller.abort();
  }, [datasetKey, failureFilters, reloadVersion, windowHours]);

  const refresh = useCallback(() => setReloadVersion((current) => current + 1), []);

  async function loadMoreFailures() {
    if (!dataset?.failureCursor || failureLoadingMore) return;
    const requestKey = datasetKey;
    setFailureLoadingMore(true);
    try {
      const page = await listJenkinsFailures(
        windowHours,
        failureFilters,
        100,
        dataset.failureCursor,
      );
      if (requestKey !== currentKey.current) return;
      setDataset((current) => {
        if (!current) return current;
        const known = new Set(current.failures.map((item) => item.id));
        const next = {
          ...current,
          failures: [...current.failures, ...page.items.filter((item) => !known.has(item.id))],
          failureCursor: page.next_cursor,
          failureTotal: page.total_count,
        };
        datasetRef.current = next;
        return next;
      });
      setError(null);
    } catch (requestError) {
      setError(requestError);
    } finally {
      setFailureLoadingMore(false);
    }
  }

  const workspace = dataset?.workspace ?? null;
  const operational = dataset?.operational ?? null;
  const failures = dataset?.failures ?? [];
  const failureCursor = dataset?.failureCursor ?? null;
  const failureTotal = dataset?.failureTotal ?? 0;
  const summary = asRecord(workspace?.summary);
  const dailyUsage = asRecord(operational?.llm_usage);
  const sync = asRecord(summary.sync);
  const syncStats = asRecord(sync.stats);
  const syncErrors = Array.isArray(syncStats.errors) ? syncStats.errors.length : 0;
  const retentionLimited = numberValue(summary.retention_limited_job_count);
  const newFailureTotal = numberValue(summary.new_failure_count);
  const syncTime = sync.status === "running"
    ? `Catalog sync started ${formatRelative(stringValue(sync.started_at))}`
    : `Last catalog sync ${formatRelative(stringValue(sync.completed_at) || stringValue(sync.updated_at))}`;
  const subtitle = workspace
    ? `${numberValue(summary.job_count).toLocaleString()} jobs indexed · updated ${formatRelative(workspace.generated_at)}`
    : undefined;

  const tabs = useMemo(
    () => [
      { value: "new", label: "New failures", count: newFailureTotal },
      { value: "executions", label: "Failure chains" },
      { value: "recurring", label: "Recurring", count: workspace?.recurring_patterns.length },
      { value: "all", label: "All failures" },
      { value: "jobs", label: "Jobs by volume" },
      { value: "multibranch", label: "Multibranch", count: workspace?.multibranch.length },
    ],
    [newFailureTotal, workspace],
  );

  if (initialLoading && !workspace) return <LoadingPanel label="Loading Jenkins build history" />;
  if (error && !workspace) return <ErrorPanel error={error} />;
  if (!workspace) return null;

  const failureResults = (
    <>
      <BuildTable
        builds={failures}
        navigate={navigate}
        empty={view === "new" ? "No new failures in this window" : "No failed builds match these filters"}
      />
      <Stack direction={{ xs: "column", sm: "row" }} alignItems={{ xs: "stretch", sm: "center" }} justifyContent="space-between" gap={1} sx={{ p: 1.5, borderTop: "1px solid", borderColor: "divider" }}>
        <Typography variant="caption" color="text.secondary">
          {failures.length.toLocaleString()} of {failureTotal.toLocaleString()} {view === "new" ? "new " : ""}failures
        </Typography>
        {failureCursor && <Button onClick={() => void loadMoreFailures()} disabled={failureLoadingMore}>{failureLoadingMore ? "Loading" : "Load more failures"}</Button>}
      </Stack>
    </>
  );

  return (
    <Box>
      <PageHeader
        title="Jenkins failures"
        subtitle={subtitle}
        actions={
          <Stack direction="row" alignItems="center" gap={1}>
            <ToggleButtonGroup
              exclusive
              size="small"
              value={windowHours}
              onChange={(_, next: number | null) => {
                if (!next) return;
                updateLocation({ window: String(next) });
              }}
              aria-label="Build history window"
            >
              {windows.map((item) => <ToggleButton key={item.value} value={item.value}>{item.label}</ToggleButton>)}
            </ToggleButtonGroup>
            {refreshing && <CircularProgress size={18} aria-label="Loading build history" />}
            <Tooltip title="Refresh Jenkins data">
              <span>
                <IconButton onClick={refresh} disabled={refreshing} aria-label="Refresh Jenkins data">
                  <RefreshIcon />
                </IconButton>
              </span>
            </Tooltip>
          </Stack>
        }
      />

      <Box sx={{ position: "relative", minHeight: 360 }} aria-busy={transitioning && refreshing}>
        {transitioning && (
          <Box
            sx={{
              position: "absolute",
              inset: 0,
              zIndex: 3,
              display: "flex",
              alignItems: "flex-start",
              justifyContent: "center",
              pt: 8,
              bgcolor: "rgba(255, 255, 255, 0.78)",
              backdropFilter: "blur(1px)",
            }}
          >
            {error ? (
              <Stack gap={1.25} sx={{ width: "min(460px, calc(100% - 32px))" }}>
                <ErrorPanel error={error} />
                <Button variant="contained" startIcon={<RefreshIcon />} onClick={refresh} sx={{ alignSelf: "center" }}>
                  Retry {windowLabel(windowHours)} history
                </Button>
              </Stack>
            ) : (
              <Stack direction="row" alignItems="center" gap={1.25}>
                <CircularProgress size={22} />
                <Typography variant="body2" fontWeight={650}>Loading {windowLabel(windowHours)} history</Typography>
              </Stack>
            )}
          </Box>
        )}

        {Boolean(error) && !transitioning && <Box sx={{ mb: 2 }}><ErrorPanel error={error} /></Box>}
        {retentionLimited > 0 && (
          <Alert severity="warning" sx={{ mb: 2 }}>
            {retentionLimited.toLocaleString()} jobs have retention-limited history in this window.
          </Alert>
        )}
        {sync.status === "partial" && (
          <Alert severity="warning" sx={{ mb: 2 }}>
            Latest catalog sync completed with {syncErrors || "some"} enrichment error{syncErrors === 1 ? "" : "s"}.
          </Alert>
        )}

        <Paper variant="outlined" sx={{ mb: 2.5, overflow: "hidden" }}>
        <Box
          sx={{
            display: "grid",
            gridTemplateColumns: { xs: "repeat(2, minmax(0, 1fr))", sm: "repeat(3, minmax(0, 1fr))", lg: "repeat(6, minmax(0, 1fr))" },
            gap: 0,
          }}
        >
          <Metric label="Jobs indexed" value={numberValue(summary.job_count)} />
          <Metric label="Jobs active" value={numberValue(summary.active_job_count)} />
          <Metric label="Builds" value={numberValue(summary.build_count)} />
          <Metric label="Failed builds" value={numberValue(summary.failure_build_count)} tone="danger" />
          <Metric label="New failures" value={numberValue(summary.new_failure_count)} tone="warning" />
          <Metric label="Build wall time" value={`${numberValue(summary.cumulative_wall_hours).toLocaleString()}h`} />
        </Box>
        <Divider />
        <Stack direction={{ xs: "column", sm: "row" }} alignItems={{ xs: "flex-start", sm: "center" }} gap={1.5} sx={{ px: 2, py: 1.25, bgcolor: "#f8f9fa" }}>
          <StatusChip value={String(sync.status ?? "never_run")} />
          <Typography variant="caption" color="text.secondary">
            {syncTime}
          </Typography>
          <Typography variant="caption" color="text.secondary">
            {numberValue(summary.exact_job_count).toLocaleString()} exact histories · {retentionLimited.toLocaleString()} retention-limited
          </Typography>
          <Typography variant="caption" color="text.secondary">
            {numberValue(summary.enriched_failure_count).toLocaleString()} causes analyzed · {numberValue(summary.pending_failure_analysis_count).toLocaleString()} awaiting evidence
          </Typography>
          <Typography variant="caption" color="text.secondary" sx={{ ml: { sm: "auto" } }}>
            {numberValue(summary.running_build_count).toLocaleString()} running · {formatTokens(dailyUsage.total_tokens)} · {formatUsd(dailyUsage.estimated_cost_usd)} today
          </Typography>
        </Stack>
        </Paper>

        <Paper variant="outlined" sx={{ overflow: "hidden" }}>
        <Tabs
          value={view}
          onChange={(_, next: JenkinsView) => updateLocation({ view: next })}
          variant="scrollable"
          scrollButtons="auto"
          sx={{ px: 1, minHeight: 48, borderBottom: "1px solid", borderColor: "divider" }}
        >
          {tabs.map((tab) => (
            <Tab
              key={tab.value}
              value={tab.value}
              label={<Stack direction="row" gap={0.75} alignItems="center"><span>{tab.label}</span>{tab.count !== undefined && <Chip size="small" label={tab.count} />}</Stack>}
            />
          ))}
        </Tabs>

        {view === "new" && failureResults}
        {view === "executions" && <ExecutionTable executions={workspace.active_executions} navigate={navigate} />}
        {view === "recurring" && <PatternTable patterns={workspace.recurring_patterns} navigate={navigate} />}
        {view === "all" && (
          <Box>
            <Stack direction={{ xs: "column", sm: "row" }} gap={1.25} sx={{ p: 1.5, borderBottom: "1px solid", borderColor: "divider" }}>
              <TextField
                size="small"
                placeholder="Filter by Jenkins job"
                value={queryText}
                onChange={(event) => setQueryText(event.target.value)}
                onKeyDown={(event) => event.key === "Enter" && setJobQuery(queryText.trim())}
                sx={{ minWidth: { sm: 320 } }}
                InputProps={{
                  endAdornment: (
                    <InputAdornment position="end">
                      <Tooltip title="Apply job filter">
                        <IconButton size="small" onClick={() => setJobQuery(queryText.trim())} aria-label="Apply job filter"><SearchIcon fontSize="small" /></IconButton>
                      </Tooltip>
                    </InputAdornment>
                  ),
                }}
              />
              <TextField
                select
                size="small"
                label="Result"
                value={result}
                onChange={(event) => setResult(event.target.value as FailureResult | "")}
                sx={{ minWidth: 150 }}
              >
                <MenuItem value="">All results</MenuItem>
                <MenuItem value="FAILURE">Failure</MenuItem>
                <MenuItem value="UNSTABLE">Unstable</MenuItem>
                <MenuItem value="ABORTED">Aborted</MenuItem>
              </TextField>
            </Stack>
            {failureResults}
          </Box>
        )}
        {view === "jobs" && <JobFamilyTable jobs={workspace.busy_jobs} />}
        {view === "multibranch" && (
          <MultibranchTable
            families={workspace.multibranch}
            expanded={expandedFamily}
            onToggle={(name) => setExpandedFamily((current) => current === name ? null : name)}
          />
        )}
        </Paper>
      </Box>
    </Box>
  );
}

function Metric({ label, value, tone }: { label: string; value: string | number; tone?: "danger" | "warning" }) {
  const color = tone === "danger" ? "error.main" : tone === "warning" ? "warning.main" : "text.primary";
  return (
    <Box sx={{ px: 2, py: 2, minWidth: 0, borderRight: "1px solid", borderBottom: { xs: "1px solid", lg: 0 }, borderColor: "divider" }}>
      <Typography variant="caption" color="text.secondary">{label}</Typography>
      <Typography variant="h5" color={color} sx={{ mt: 0.25, overflowWrap: "anywhere" }}>{typeof value === "number" ? value.toLocaleString() : value}</Typography>
    </Box>
  );
}

function BuildTable({ builds, navigate, empty }: { builds: JenkinsBuild[]; navigate: ReturnType<typeof useNavigate>; empty: string }) {
  return (
    <TableContainer>
      <Table size="small" sx={{ minWidth: 1080, tableLayout: "fixed" }}>
        <TableHead><TableRow><TableCell width={70}>Priority</TableCell><TableCell width={140}>Signal</TableCell><TableCell width={250}>Job / build</TableCell><TableCell width={110}>Result</TableCell><TableCell width={190}>What failed</TableCell><TableCell width={140}>Source</TableCell><TableCell width={100}>Started</TableCell><TableCell width={80}>Duration</TableCell></TableRow></TableHead>
        <TableBody>
          {builds.length === 0 ? <EmptyRow columns={8} text={empty} /> : builds.map((build) => (
            <TableRow hover key={build.id} onClick={() => navigate(`/jenkins/builds/${build.id}`)} sx={{ cursor: "pointer" }}>
              <TableCell><Priority score={build.priority_score} reasons={build.priority_reasons ?? []} /></TableCell>
              <TableCell><StatusChip value={build.novelty} /></TableCell>
              <TableCell sx={{ width: 250, maxWidth: 250 }}>
                <Stack direction="row" alignItems="center" gap={0.75}>
                  <Box sx={{ minWidth: 0, maxWidth: 210 }}>
                    <Typography variant="body2" fontWeight={700} noWrap title={build.job_name}>{build.job_name}</Typography>
                    <Typography variant="caption" color="text.secondary">#{build.build_number}{build.head_name ? ` · ${build.head_name}` : ""}</Typography>
                  </Box>
                  <Tooltip title="Open in Jenkins">
                    <Link href={build.url} target="_blank" rel="noreferrer" onClick={(event) => event.stopPropagation()} aria-label={`Open ${build.job_name} build in Jenkins`}><OpenInNewIcon sx={{ fontSize: 16 }} /></Link>
                  </Tooltip>
                </Stack>
              </TableCell>
              <TableCell><StatusChip value={build.result.toLowerCase()} /></TableCell>
              <TableCell sx={{ width: 190 }}>
                <Typography variant="body2" fontWeight={650} sx={{ display: "-webkit-box", WebkitLineClamp: 3, WebkitBoxOrient: "vertical", overflow: "hidden" }}>{build.failure_summary || titleCase(build.failure_classification)}</Typography>
                <Typography variant="caption" color="text.secondary">{build.failed_stage || titleCase(build.failure_classification)}</Typography>
              </TableCell>
              <TableCell sx={{ width: 140, overflow: "hidden" }}><SourceSummary source={build} /></TableCell>
              <TableCell><Typography variant="body2">{formatRelative(build.started_at)}</Typography></TableCell>
              <TableCell>{duration(build.duration_ms)}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </TableContainer>
  );
}

function ExecutionTable({ executions, navigate }: { executions: JenkinsExecution[]; navigate: ReturnType<typeof useNavigate> }) {
  return (
    <TableContainer>
      <Table sx={{ minWidth: 980 }}>
        <TableHead><TableRow><TableCell width={74}>Priority</TableCell><TableCell>Logical execution</TableCell><TableCell width={150}>Class</TableCell><TableCell width={120}>Builds</TableCell><TableCell width={180}>Source</TableCell><TableCell width={120}>Last failure</TableCell></TableRow></TableHead>
        <TableBody>
          {executions.length === 0 ? <EmptyRow columns={6} text="No failure chains in this window" /> : executions.map((execution) => (
            <TableRow hover key={execution.logical_run_key} onClick={() => navigate(`/jenkins/builds/${execution.primary_build_id}`)} sx={{ cursor: "pointer" }}>
              <TableCell><Priority score={execution.priority_score} reasons={execution.priority_reasons} /></TableCell>
              <TableCell>
                <Typography variant="body2" fontWeight={700}>{execution.root_job} #{execution.root_build_number}</Typography>
                <Typography variant="caption" color="text.secondary">{execution.title}</Typography>
              </TableCell>
              <TableCell><Chip size="small" variant="outlined" label={titleCase(execution.classification)} /></TableCell>
              <TableCell>{execution.affected_build_count}{execution.propagated_build_count > 0 && <Typography component="span" variant="caption" color="text.secondary"> · {execution.propagated_build_count} propagated</Typography>}</TableCell>
              <TableCell><SourceSummary source={execution} /></TableCell>
              <TableCell>{formatRelative(execution.last_seen_at)}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </TableContainer>
  );
}

function PatternTable({ patterns, navigate }: { patterns: JenkinsFailurePattern[]; navigate: ReturnType<typeof useNavigate> }) {
  return (
    <TableContainer>
      <Table sx={{ minWidth: 900 }}>
        <TableHead><TableRow><TableCell width={74}>Priority</TableCell><TableCell>Recurring problem</TableCell><TableCell width={120}>Occurrences</TableCell><TableCell width={120}>Jobs</TableCell><TableCell width={130}>Failed time</TableCell><TableCell width={120}>Last seen</TableCell></TableRow></TableHead>
        <TableBody>
          {patterns.length === 0 ? <EmptyRow columns={6} text="No repeated failure signatures yet" /> : patterns.map((pattern) => (
            <TableRow hover key={pattern.signature} onClick={() => navigate(`/jenkins/builds/${pattern.latest_build_id}`)} sx={{ cursor: "pointer" }}>
              <TableCell><Priority score={pattern.priority_score} reasons={[]} /></TableCell>
              <TableCell><Typography variant="body2" fontWeight={700}>{pattern.title}</Typography><Typography variant="caption" color="text.secondary">{titleCase(pattern.classification)} · {pattern.signature}</Typography></TableCell>
              <TableCell>{pattern.occurrence_count}</TableCell>
              <TableCell><Tooltip title={pattern.affected_jobs.join("\n")}><Chip size="small" variant="outlined" label={pattern.affected_jobs.length} /></Tooltip></TableCell>
              <TableCell>{pattern.failed_wall_hours.toLocaleString()}h</TableCell>
              <TableCell>{formatRelative(pattern.last_seen_at)}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </TableContainer>
  );
}

function JobFamilyTable({ jobs }: { jobs: JenkinsJobFamily[] }) {
  return (
    <TableContainer>
      <Table sx={{ minWidth: 1000 }}>
        <TableHead><TableRow><TableCell>Jenkins job</TableCell><TableCell width={100}>Runs</TableCell><TableCell width={120}>Failure rate</TableCell><TableCell width={150}>Results</TableCell><TableCell width={120}>Wall time</TableCell><TableCell width={130}>Median / p95</TableCell><TableCell width={120}>Latest</TableCell></TableRow></TableHead>
        <TableBody>
          {jobs.length === 0 ? <EmptyRow columns={7} text="No jobs ran in this window" /> : jobs.map((job) => (
            <TableRow key={job.job_name}>
              <TableCell><Stack direction="row" alignItems="center" gap={0.75}><Box sx={{ minWidth: 0 }}><Typography variant="body2" fontWeight={700}>{job.job_name}</Typography><Typography variant="caption" color="text.secondary">{titleCase(job.job_type)}{job.head_type !== "unknown" ? ` · ${titleCase(job.head_type)}` : ""}</Typography></Box><Tooltip title="Open in Jenkins"><Link href={job.url} target="_blank" rel="noreferrer" aria-label={`Open ${job.job_name} in Jenkins`}><OpenInNewIcon sx={{ fontSize: 16 }} /></Link></Tooltip></Stack></TableCell>
              <TableCell>{job.run_count.toLocaleString()}</TableCell>
              <TableCell><Typography color={job.failure_rate >= 20 ? "error.main" : "text.primary"} fontWeight={650}>{job.failure_rate.toFixed(1)}%</Typography></TableCell>
              <TableCell><ResultCounts counts={job.result_counts} /></TableCell>
              <TableCell>{job.wall_hours.toLocaleString()}h</TableCell>
              <TableCell>{compactDuration(job.median_duration_minutes)} / {compactDuration(job.p95_duration_minutes)}</TableCell>
              <TableCell><StatusChip value={job.latest_result.toLowerCase()} /><Typography variant="caption" display="block" color="text.secondary" sx={{ mt: 0.5 }}>{formatRelative(job.last_build_at)}</Typography></TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </TableContainer>
  );
}

function MultibranchTable({ families, expanded, onToggle }: { families: JenkinsMultibranchFamily[]; expanded: string | null; onToggle: (name: string) => void }) {
  return (
    <TableContainer>
      <Table sx={{ minWidth: 900 }}>
        <TableHead><TableRow><TableCell width={48} /><TableCell>Multibranch pipeline</TableCell><TableCell width={120}>Active heads</TableCell><TableCell width={100}>Runs</TableCell><TableCell width={190}>Head types</TableCell><TableCell width={180}>Results</TableCell></TableRow></TableHead>
        <TableBody>
          {families.length === 0 ? <EmptyRow columns={6} text="No multibranch pipelines found" /> : families.map((family) => {
            const open = expanded === family.parent;
            const children = family.children.map(asRecord);
            return (
              <Fragment key={family.parent}>
                <TableRow hover onClick={() => onToggle(family.parent)} sx={{ cursor: "pointer" }}>
                  <TableCell><IconButton size="small" aria-label={`${open ? "Collapse" : "Expand"} ${family.parent}`}>{open ? <ExpandMoreIcon /> : <ChevronRightIcon />}</IconButton></TableCell>
                  <TableCell><Stack direction="row" alignItems="center" gap={0.75}><Typography variant="body2" fontWeight={700}>{family.parent}</Typography><Tooltip title="Open in Jenkins"><Link href={family.url} target="_blank" rel="noreferrer" onClick={(event) => event.stopPropagation()} aria-label={`Open ${family.parent} in Jenkins`}><OpenInNewIcon sx={{ fontSize: 16 }} /></Link></Tooltip></Stack></TableCell>
                  <TableCell>{family.active_child_count} / {family.child_count}</TableCell>
                  <TableCell>{family.run_count}</TableCell>
                  <TableCell><HeadCounts counts={family.head_counts} /></TableCell>
                  <TableCell><ResultCounts counts={family.result_counts} /></TableCell>
                </TableRow>
                <TableRow>
                  <TableCell colSpan={6} sx={{ p: 0, borderBottom: open ? undefined : 0 }}>
                    <Collapse in={open} timeout="auto" unmountOnExit>
                      <Box sx={{ bgcolor: "#fafbfc", px: { xs: 2, md: 7 }, py: 1.25 }}>
                        <Table size="small" aria-label={`${family.parent} branch and change request heads`}>
                          <TableHead><TableRow><TableCell>Head</TableCell><TableCell>Type</TableCell><TableCell>Repository</TableCell><TableCell>Runs</TableCell><TableCell>Results</TableCell><TableCell>Last build</TableCell></TableRow></TableHead>
                          <TableBody>
                            {children.map((child) => (
                              <TableRow key={stringValue(child.job_name)}>
                                <TableCell><Typography variant="body2" fontWeight={650}>{stringValue(child.head_name) || stringValue(child.job_name)}</Typography></TableCell>
                                <TableCell><StatusChip value={stringValue(child.head_type) || "unknown"} /></TableCell>
                                <TableCell>{stringValue(child.repository) || "-"}</TableCell>
                                <TableCell>{numberValue(child.run_count)}</TableCell>
                                <TableCell><ResultCounts counts={asNumberRecord(child.result_counts)} /></TableCell>
                                <TableCell>{formatRelative(stringValue(child.last_build_at))}</TableCell>
                              </TableRow>
                            ))}
                          </TableBody>
                        </Table>
                      </Box>
                    </Collapse>
                  </TableCell>
                </TableRow>
              </Fragment>
            );
          })}
        </TableBody>
      </Table>
    </TableContainer>
  );
}

function Priority({ score, reasons }: { score: number; reasons: string[] }) {
  const color = score >= 60 ? "error.main" : score >= 30 ? "warning.main" : "text.primary";
  return <Tooltip title={reasons.length ? reasons.join(" · ") : "Priority based on blockage, recurrence, wall time, fanout, and source impact"}><Typography fontWeight={750} color={color}>{score}</Typography></Tooltip>;
}

function ResultCounts({ counts }: { counts: Record<string, number> }) {
  const ordered = ["FAILURE", "UNSTABLE", "ABORTED", "SUCCESS", "RUNNING"];
  const visible = ordered.filter((name) => counts[name]);
  return <Stack direction="row" gap={0.5} flexWrap="wrap">{visible.map((name) => <Tooltip key={name} title={titleCase(name.toLowerCase())}><Chip size="small" variant="outlined" label={`${name.slice(0, 1)} ${counts[name]}`} /></Tooltip>)}</Stack>;
}

function HeadCounts({ counts }: { counts: Record<string, number> }) {
  return <Stack direction="row" gap={0.5} flexWrap="wrap">{Object.entries(counts).filter(([, count]) => count > 0).map(([name, count]) => <Chip key={name} size="small" variant="outlined" label={`${titleCase(name)} ${count}`} />)}</Stack>;
}

function EmptyRow({ columns, text }: { columns: number; text: string }) {
  return <TableRow><TableCell colSpan={columns}><Typography color="text.secondary" sx={{ py: 4, textAlign: "center" }}>{text}</Typography></TableCell></TableRow>;
}

function duration(milliseconds: number): string {
  return compactDuration(milliseconds / 60_000);
}

function compactDuration(minutes: number): string {
  if (minutes >= 1440) return `${(minutes / 1440).toFixed(1)}d`;
  if (minutes >= 60) return `${(minutes / 60).toFixed(1)}h`;
  return `${Math.round(minutes)}m`;
}

function asRecord(value: unknown): UnknownRecord {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as UnknownRecord : {};
}

function asNumberRecord(value: unknown): Record<string, number> {
  return Object.fromEntries(Object.entries(asRecord(value)).filter((entry): entry is [string, number] => typeof entry[1] === "number"));
}

function numberValue(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function stringValue(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function parseWindow(value: string | null): number {
  const parsed = Number(value);
  return windows.some((item) => item.value === parsed) ? parsed : 168;
}

function parseView(value: string | null): JenkinsView {
  const views: JenkinsView[] = ["new", "executions", "recurring", "all", "jobs", "multibranch"];
  return views.includes(value as JenkinsView) ? value as JenkinsView : "new";
}

function windowLabel(hours: number): string {
  return windows.find((item) => item.value === hours)?.label ?? `${hours}h`;
}
