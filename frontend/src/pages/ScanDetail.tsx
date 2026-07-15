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
import { formatDate, titleCase } from "../utils/format";

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

  const active = scan.status === "queued" || scan.status === "running";
  const progress = ((STAGES.indexOf(scan.stage) + 1) / STAGES.length) * 100;

  return (
    <Box>
      <Button startIcon={<ArrowBackIcon />} onClick={() => navigate("/scans")} sx={{ mb: 1 }}>
        Scans
      </Button>
      <PageHeader
        title={`${titleCase(scan.mode)} scan`}
        subtitle={scan.id}
        actions={
          active ? (
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
      {scan.coverage_status && !active && scan.coverage_status !== "complete" && (
        <Alert severity="warning" sx={{ mb: 2 }}>
          Detector coverage was {titleCase(scan.coverage_status)}. Incident results may be incomplete and unresolved conditions were preserved.
        </Alert>
      )}

      <Paper variant="outlined" sx={{ mb: 2.5, overflow: "hidden" }}>
        <Box sx={{ p: { xs: 2, md: 2.5 } }}>
          <Stack direction={{ xs: "column", md: "row" }} gap={2.5} justifyContent="space-between">
            <Stack direction="row" gap={1} alignItems="center" flexWrap="wrap">
              <StatusChip value={scan.status} size="medium" />
              <Chip label={titleCase(scan.stage)} variant="outlined" />
              <Typography variant="body2" color="text.secondary">
                Attempt {scan.attempt_count || 1}
              </Typography>
            </Stack>
            <Stack direction={{ xs: "column", sm: "row" }} gap={{ xs: 0.5, sm: 3 }}>
              <Meta label="Created" value={formatDate(scan.created_at)} />
              <Meta label="Started" value={formatDate(scan.started_at)} />
              <Meta label="Completed" value={formatDate(scan.completed_at)} />
            </Stack>
          </Stack>
          <Box sx={{ mt: 2 }}>
            <LinearProgress
              variant="determinate"
              value={active ? Math.max(4, progress) : 100}
              sx={{ height: 6, "& .MuiLinearProgress-bar": { transition: "none" } }}
            />
          </Box>
        </Box>
        <Divider />
        <Box sx={{ px: { xs: 2, md: 2.5 }, py: 1.5, bgcolor: "#f8f9fa" }}>
          <Stack direction="row" gap={1} flexWrap="wrap">
            {scan.categories.length ? scan.categories.map((category) => <Chip key={category} size="small" label={titleCase(category)} />) : <Chip size="small" label="All categories" />}
          </Stack>
        </Box>
      </Paper>

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
          {active ? (connection === "live" ? "Live" : titleCase(connection)) : `${events.length} events`}
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

function checkDuration(startedAt: string | null, completedAt: string | null): string {
  if (!startedAt) return "-";
  const end = completedAt ? new Date(completedAt).getTime() : Date.now();
  const seconds = Math.max(0, Math.round((end - new Date(startedAt).getTime()) / 1000));
  return `${seconds}s`;
}
