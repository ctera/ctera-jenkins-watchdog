import {
  Alert,
  Box,
  Button,
  Checkbox,
  Chip,
  FormControl,
  InputLabel,
  LinearProgress,
  ListItemText,
  MenuItem,
  Paper,
  Select,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  ToggleButton,
  ToggleButtonGroup,
  Tooltip,
  Typography,
} from "@mui/material";
import PlayArrowIcon from "@mui/icons-material/PlayArrow";
import OpenInNewIcon from "@mui/icons-material/OpenInNew";
import RefreshIcon from "@mui/icons-material/Refresh";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import PageHeader from "../components/PageHeader";
import { EmptyPanel, ErrorPanel, LoadingPanel } from "../components/StatePanel";
import StatusChip from "../components/StatusChip";
import { useScanEvents } from "../hooks/useScanEvents";
import { ApiError, createScan, listScans, type Scan } from "../services/api";
import { formatDate, formatRelative, titleCase } from "../utils/format";
import {
  analysisProgress,
  isAnalysisActive,
  isCollectionActive,
  scanStageLabel,
  scanWorkflowStatus,
} from "../utils/scan";

const CATEGORIES = [
  ["jenkins_controller", "Jenkins controller"],
  ["jenkins_agent", "Jenkins agents"],
  ["jenkins_queue", "Jenkins queue"],
  ["jenkins_pipeline_pattern", "Pipeline patterns"],
  ["jenkins_failed_build", "Failed builds"],
  ["jenkins_build", "Build activity"],
  ["k8s_workload", "Kubernetes workloads"],
  ["k8s_event", "Kubernetes events"],
  ["k8s_node", "Kubernetes nodes"],
] as const;

const STAGE_INDEX: Record<string, number> = {
  queued: 0,
  detecting: 1,
  findings_stored: 2,
  correlating: 3,
  reconciling: 4,
  investigating: 5,
  planning_actions: 6,
  completed: 7,
};

export default function Scans() {
  const navigate = useNavigate();
  const [mode, setMode] = useState<"regular" | "deep">("regular");
  const [categories, setCategories] = useState<string[]>([]);
  const [scans, setScans] = useState<Scan[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const refresh = useCallback(async () => {
    try {
      const page = await listScans();
      setScans(page.items);
      setCursor(page.next_cursor);
      setError(null);
    } catch (requestError) {
      setError(requestError);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => void refresh(), [refresh]);

  const collectionActive = useMemo(() => scans.find(isCollectionActive), [scans]);
  const active = useMemo(
    () => scans.find((scan) => isCollectionActive(scan) || isAnalysisActive(scan)),
    [scans],
  );
  const { events, connection } = useScanEvents(active?.id, refresh);

  useEffect(() => {
    if (events.length) void refresh();
  }, [events.length, refresh]);

  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => void refresh(), 2_500);
    return () => window.clearInterval(timer);
  }, [active, refresh]);

  async function start() {
    setStarting(true);
    setError(null);
    try {
      const scan = await createScan(mode, categories);
      setScans((current) => [scan, ...current.filter((item) => item.id !== scan.id)]);
      navigate(`/scans/${scan.id}`);
    } catch (requestError) {
      if (requestError instanceof ApiError && requestError.status === 409) await refresh();
      else setError(requestError);
    } finally {
      setStarting(false);
    }
  }

  async function loadMore() {
    if (!cursor) return;
    const page = await listScans(cursor);
    setScans((current) => [...current, ...page.items]);
    setCursor(page.next_cursor);
  }

  const progress = active
    ? isCollectionActive(active)
      ? ((STAGE_INDEX[active.stage] ?? 0) / 7) * 100
      : analysisProgress(active.analysis)
    : 0;
  const activeWorkflow = active ? scanWorkflowStatus(active) : null;

  return (
    <Box>
      <PageHeader
        title="Scans"
        actions={
          <Tooltip title="Refresh scans">
            <Button variant="outlined" startIcon={<RefreshIcon />} onClick={() => void refresh()}>
              Refresh
            </Button>
          </Tooltip>
        }
      />

      {Boolean(error) && <Box sx={{ mb: 2 }}><ErrorPanel error={error} /></Box>}

      <Paper variant="outlined" sx={{ p: { xs: 2, md: 2.5 }, mb: 2.5 }}>
        <Stack direction={{ xs: "column", lg: "row" }} gap={2} alignItems={{ xs: "stretch", lg: "center" }}>
          <ToggleButtonGroup
            exclusive
            size="small"
            value={mode}
            onChange={(_, value) => value && setMode(value)}
            aria-label="Scan mode"
            sx={{ flexShrink: 0 }}
          >
            <ToggleButton value="regular">Regular</ToggleButton>
            <ToggleButton value="deep">Deep</ToggleButton>
          </ToggleButtonGroup>
          <FormControl size="small" sx={{ minWidth: { xs: 0, sm: 310 }, flex: 1 }}>
            <InputLabel id="scan-categories-label">Categories</InputLabel>
            <Select
              labelId="scan-categories-label"
              multiple
              value={categories}
              label="Categories"
              onChange={(event) => setCategories(typeof event.target.value === "string" ? event.target.value.split(",") : event.target.value)}
              renderValue={(selected) => selected.length ? `${selected.length} selected` : "All categories"}
            >
              {CATEGORIES.map(([value, label]) => (
                <MenuItem key={value} value={value}>
                  <Checkbox checked={categories.includes(value)} size="small" />
                  <ListItemText primary={label} />
                </MenuItem>
              ))}
            </Select>
          </FormControl>
          <Button
            variant="contained"
            startIcon={<PlayArrowIcon />}
            disabled={starting || Boolean(collectionActive)}
            onClick={() => void start()}
            sx={{ minWidth: 138 }}
          >
            {starting ? "Queuing" : "Start scan"}
          </Button>
        </Stack>
      </Paper>

      {active && (
        <Paper variant="outlined" sx={{ mb: 2.5, overflow: "hidden" }}>
          <Box sx={{ p: { xs: 2, md: 2.5 } }}>
            <Stack direction="row" justifyContent="space-between" gap={2} alignItems="flex-start">
              <Box sx={{ minWidth: 0 }}>
                <Stack direction="row" gap={1} alignItems="center" flexWrap="wrap">
                  <Typography variant="h6">Active {active.mode} scan</Typography>
                  <StatusChip value={activeWorkflow?.value ?? active.status} />
                  <Chip
                    size="small"
                    variant="outlined"
                    label={isCollectionActive(active)
                      ? scanStageLabel(active.stage)
                      : `${active.analysis?.succeeded_count ?? 0} of ${active.analysis?.selected_count ?? 0} investigations complete`}
                  />
                </Stack>
                <Typography variant="body2" color="text.secondary" sx={{ mt: 0.75 }}>
                  Started {formatRelative(active.started_at ?? active.created_at)} · attempt {active.attempt_count || 1}
                </Typography>
              </Box>
              <Tooltip title="Open scan">
                <Button endIcon={<OpenInNewIcon />} onClick={() => navigate(`/scans/${active.id}`)}>
                  Details
                </Button>
              </Tooltip>
            </Stack>
          </Box>
          <LinearProgress variant="determinate" value={Math.max(4, progress)} sx={{ height: 5 }} />
          {events.length > 0 && (
            <Box sx={{ px: { xs: 2, md: 2.5 }, py: 1.5, bgcolor: "#f8f9fa", borderTop: "1px solid", borderColor: "divider" }}>
              <Typography variant="caption" color="text.secondary">
                {connection === "live" ? "Live" : "Reconnecting"} · {activeWorkflow?.label ?? scanStageLabel(active.stage)}
              </Typography>
            </Box>
          )}
        </Paper>
      )}

      <Typography variant="h6" sx={{ mb: 1.25 }}>History</Typography>
      {loading ? (
        <LoadingPanel label="Loading scans" />
      ) : scans.length === 0 ? (
        <EmptyPanel label="No scans yet" />
      ) : (
        <>
          <TableContainer component={Paper} variant="outlined">
            <Table sx={{ minWidth: 760 }}>
              <TableHead>
                <TableRow>
                  <TableCell>Overall</TableCell>
                  <TableCell>Collection</TableCell>
                  <TableCell>Agent analysis</TableCell>
                  <TableCell>Mode</TableCell>
                  <TableCell>Categories</TableCell>
                  <TableCell>Created</TableCell>
                  <TableCell>Collection time</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {scans.map((scan) => (
                  <TableRow
                    hover
                    key={scan.id}
                    onClick={() => navigate(`/scans/${scan.id}`)}
                    tabIndex={0}
                    onKeyDown={(event) => event.key === "Enter" && navigate(`/scans/${scan.id}`)}
                    sx={{ cursor: "pointer" }}
                  >
                    <TableCell><StatusChip value={scanWorkflowStatus(scan).value} /></TableCell>
                    <TableCell>
                      <StatusChip value={scan.status} />
                      {isCollectionActive(scan) && <Typography variant="caption" display="block" color="text.secondary" sx={{ mt: 0.5 }}>{scanStageLabel(scan.stage)}</Typography>}
                    </TableCell>
                    <TableCell>
                      {scan.analysis?.status && scan.analysis.status !== "not_started"
                        ? <StatusChip value={scan.analysis.status} />
                        : <Typography variant="body2" color="text.secondary">No candidates</Typography>}
                      {Boolean(scan.analysis?.candidate_count) && (
                        <Typography variant="caption" display="block" color="text.secondary" sx={{ mt: 0.5 }}>
                          {scan.analysis?.status === "budget_deferred"
                            ? `${scan.analysis.budget_deferred_count} of ${scan.analysis.candidate_count} budget deferred`
                            : `${scan.analysis?.selected_count ?? 0} admitted of ${scan.analysis?.candidate_count ?? 0}`}
                        </Typography>
                      )}
                    </TableCell>
                    <TableCell>{titleCase(scan.mode)}</TableCell>
                    <TableCell>{scan.categories.length ? `${scan.categories.length} selected` : "All"}</TableCell>
                    <TableCell>{formatDate(scan.created_at)}</TableCell>
                    <TableCell>{duration(scan)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableContainer>
          {cursor && <Button sx={{ mt: 1.5 }} onClick={() => void loadMore()}>Load more</Button>}
        </>
      )}
    </Box>
  );
}

function duration(scan: Scan): string {
  if (!scan.started_at) return "-";
  const end = scan.completed_at ? new Date(scan.completed_at).getTime() : Date.now();
  const seconds = Math.max(0, Math.round((end - new Date(scan.started_at).getTime()) / 1000));
  if (seconds >= 60) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  return `${seconds}s`;
}
