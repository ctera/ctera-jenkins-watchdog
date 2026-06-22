import { useEffect, useRef, useState } from "react";
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  Chip,
  CircularProgress,
  Grid,
  IconButton,
  Paper,
  Stack,
  Tooltip,
  Typography,
} from "@mui/material";
import DeleteOutlineIcon from "@mui/icons-material/DeleteOutline";
import OpenInNewIcon from "@mui/icons-material/OpenInNew";
import PlayArrowIcon from "@mui/icons-material/PlayArrow";
import StopIcon from "@mui/icons-material/Stop";
import { deleteFinding, fetchFindings, type FindingsResponse } from "../services/api";
import { useScan } from "../context/ScanContext";

function StatCard({ value, label, color }: { value: number | string; label: string; color?: string }) {
  return (
    <Card>
      <CardContent sx={{ textAlign: "center", py: 3 }}>
        <Typography variant="h3" sx={{ fontWeight: 700, color: color || "text.primary" }}>
          {value}
        </Typography>
        <Typography variant="body2" color="text.secondary">
          {label}
        </Typography>
      </CardContent>
    </Card>
  );
}

export default function Dashboard() {
  const [data, setData] = useState<FindingsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const { scan, startScan, stopScan } = useScan();
  const logRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    fetchFindings()
      .then(setData)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    if (!scan.scanning && scan.events.length > 0) {
      fetchFindings().then(setData).catch(() => {});
    }
  }, [scan.scanning]);

  useEffect(() => {
    if (logRef.current) {
      logRef.current.scrollTop = logRef.current.scrollHeight;
    }
  }, [scan.events.length]);

  if (loading) {
    return (
      <Box sx={{ display: "flex", justifyContent: "center", py: 8 }}>
        <CircularProgress />
      </Box>
    );
  }

  const findings = data?.findings || [];
  const critical = findings.filter((f) => f.severity === "critical").length;
  const warnings = findings.filter((f) => f.severity === "warning").length;
  const investigated = findings.filter((f) => f.investigation).length;

  const handleDismiss = async (fingerprint: string) => {
    try {
      await deleteFinding(fingerprint);
      setData((prev) => prev ? {
        ...prev,
        findings: prev.findings.filter((f) => f.fingerprint !== fingerprint),
        total_findings: prev.total_findings - 1,
      } : prev);
    } catch {}
  };

  return (
    <Stack spacing={3}>
      <Box sx={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <Box>
          <Typography variant="h4">Jenkins Agent Dashboard</Typography>
          {data?.last_scan && (
            <Typography variant="body2" color="text.secondary">
              Last scan: {new Date(data.last_scan).toLocaleString()}
            </Typography>
          )}
        </Box>
        <Box sx={{ display: "flex", gap: 1 }}>
          {scan.scanning ? (
            <Button
              variant="contained"
              color="error"
              startIcon={scan.stopping ? <CircularProgress size={16} color="inherit" /> : <StopIcon />}
              onClick={stopScan}
              disabled={scan.stopping}
              size="large"
            >
              {scan.stopping ? "Stopping..." : "Stop Scan"}
            </Button>
          ) : (
            <Button
              variant="contained"
              startIcon={<PlayArrowIcon />}
              onClick={startScan}
              size="large"
            >
              Run Scan
            </Button>
          )}
        </Box>
      </Box>

      {error && <Alert severity="error">{error}</Alert>}

      {scan.events.length > 0 && (
        <Paper variant="outlined" sx={{ p: 2 }}>
          <Typography variant="subtitle2" sx={{ mb: 1, fontWeight: 600 }}>
            {scan.scanning ? "Scanning... " : "Complete: "}{scan.status}
          </Typography>
          <Box
            ref={logRef}
            sx={{
              maxHeight: 200,
              overflow: "auto",
              fontFamily: "monospace",
              fontSize: "0.75rem",
              bgcolor: "background.default",
              p: 1,
              borderRadius: 1,
            }}
          >
            {scan.events.map((evt, i) => (
              <Box key={i} sx={{ color: evt.type === "investigation_complete" ? "success.main" : evt.type === "tool_call" ? "info.main" : evt.type === "error" || evt.type === "investigation_error" ? "error.main" : "text.secondary" }}>
                {evt.type === "scan_started" && `> Scan ${evt.scan_id} started`}
                {evt.type === "detection_complete" && `Detection: ${evt.total_findings} findings`}
                {evt.type === "investigation_plan" && `Will investigate ${evt.count} findings`}
                {evt.type === "investigation_start" && `-- [${evt.index}/${evt.total}] ${evt.resource}`}
                {evt.type === "tool_call" && `   > ${evt.tool}(${Object.keys(evt.args || {}).join(", ")})`}
                {evt.type === "reasoning" && `   Analyzing: ${evt.content?.slice(0, 120)}...`}
                {evt.type === "investigation_complete" && `   Done: ${evt.confidence}: ${evt.root_cause?.slice(0, 100)}`}
                {evt.type === "investigation_error" && `   Error: ${evt.error}`}
                {evt.type === "error" && `ERROR: ${evt.message}`}
                {evt.type === "scan_complete" && `Complete: ${evt.total_findings} findings, ${evt.investigations_performed} investigated (${evt.duration_s}s)`}
                {evt.type === "scan_stopped" && `Stopped after ${evt.duration_s}s`}
              </Box>
            ))}
          </Box>
        </Paper>
      )}

      <Grid container spacing={2}>
        <Grid size={{ xs: 6, md: 3 }}>
          <StatCard value={findings.length} label="Total Findings" />
        </Grid>
        <Grid size={{ xs: 6, md: 3 }}>
          <StatCard value={critical} label="Critical" color="error.main" />
        </Grid>
        <Grid size={{ xs: 6, md: 3 }}>
          <StatCard value={warnings} label="Warnings" color="warning.main" />
        </Grid>
        <Grid size={{ xs: 6, md: 3 }}>
          <StatCard value={investigated} label="Investigated" color="info.main" />
        </Grid>
      </Grid>

      <Typography variant="h5">Findings Overview</Typography>
      <Stack spacing={1}>
        {findings
          .sort((a, b) => {
            const order = { critical: 0, warning: 1, low: 2, info: 3 };
            return (order[a.severity as keyof typeof order] ?? 9) - (order[b.severity as keyof typeof order] ?? 9);
          })
          .map((f) => (
            <Card key={f.fingerprint} sx={{ "&:hover": { borderColor: "primary.main" }, transition: "border-color 0.2s" }}>
              <CardContent sx={{ py: 1.5, "&:last-child": { pb: 1.5 } }}>
                <Box sx={{ display: "flex", alignItems: "center", gap: 1.5 }}>
                  {f.status === "new" && (
                    <Chip label="NEW" size="small" color="info" sx={{ fontWeight: 700, fontSize: "0.65rem" }} />
                  )}
                  <Chip
                    label={f.severity.toUpperCase()}
                    size="small"
                    color={f.severity === "critical" ? "error" : f.severity === "warning" ? "warning" : "default"}
                  />
                  <Typography variant="body2" sx={{ fontWeight: 600, flex: 1 }}>
                    {f.resource}
                  </Typography>
                  <Typography variant="body2" color="text.secondary" sx={{ flex: 2 }}>
                    {f.symptom}
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
                    />
                  )}
                  {f.investigation && (
                    <Chip label={f.investigation.confidence} size="small" color="success" variant="outlined" />
                  )}
                  <Tooltip title="Dismiss">
                    <IconButton
                      size="small"
                      onClick={() => handleDismiss(f.fingerprint)}
                      sx={{ opacity: 0.5, "&:hover": { opacity: 1, color: "error.main" } }}
                    >
                      <DeleteOutlineIcon fontSize="small" />
                    </IconButton>
                  </Tooltip>
                </Box>
                {f.investigation && (
                  <Typography variant="body2" color="text.secondary" sx={{ mt: 1, ml: 8 }}>
                    Root cause: {f.investigation.root_cause.slice(0, 150)}...
                  </Typography>
                )}
              </CardContent>
            </Card>
          ))}
      </Stack>
    </Stack>
  );
}
