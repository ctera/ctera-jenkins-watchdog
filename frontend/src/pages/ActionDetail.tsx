import {
  Alert,
  Box,
  Button,
  Chip,
  Divider,
  Paper,
  Stack,
  Typography,
} from "@mui/material";
import ArrowBackIcon from "@mui/icons-material/ArrowBack";
import ReplayIcon from "@mui/icons-material/Replay";
import OpenInNewIcon from "@mui/icons-material/OpenInNew";
import { useCallback, useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import PageHeader from "../components/PageHeader";
import MarkdownContent from "../components/MarkdownContent";
import { ErrorPanel, LoadingPanel } from "../components/StatePanel";
import StatusChip from "../components/StatusChip";
import { getAction, retryAction, type ActionDetail } from "../services/api";
import { formatDate, titleCase } from "../utils/format";

export default function ActionDetailPage() {
  const { actionId } = useParams();
  const navigate = useNavigate();
  const [detail, setDetail] = useState<ActionDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [retrying, setRetrying] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const refresh = useCallback(async () => {
    if (!actionId) return;
    try {
      setDetail(await getAction(actionId));
      setError(null);
    } catch (requestError) {
      setError(requestError);
    } finally {
      setLoading(false);
    }
  }, [actionId]);

  useEffect(() => void refresh(), [refresh]);

  async function retry() {
    if (!actionId) return;
    setRetrying(true);
    try {
      await retryAction(actionId);
      await refresh();
    } catch (requestError) {
      setError(requestError);
    } finally {
      setRetrying(false);
    }
  }

  if (loading) return <LoadingPanel label="Loading action" />;
  if (error && !detail) return <ErrorPanel error={error} />;
  if (!detail) return <Alert severity="warning">Action not found</Alert>;
  const action = detail.action;

  return (
    <Box>
      <Button startIcon={<ArrowBackIcon />} onClick={() => navigate("/actions")} sx={{ mb: 1 }}>Actions</Button>
      <PageHeader
        title={titleCase(action.action_type)}
        subtitle={action.id}
        actions={
          <Stack direction="row" gap={1}>
            <Button variant="outlined" onClick={() => navigate(`/incidents/${action.incident_id}`)}>Incident</Button>
            {action.status === "permanently_failed" && (
              <Button variant="contained" startIcon={<ReplayIcon />} disabled={retrying} onClick={() => void retry()}>
                {retrying ? "Scheduling" : "Retry"}
              </Button>
            )}
          </Stack>
        }
      />
      {Boolean(error) && <Box sx={{ mb: 2 }}><ErrorPanel error={error} /></Box>}
      {action.failure_summary && <Alert severity="error" sx={{ mb: 2 }}>{action.failure_summary}</Alert>}

      <Paper variant="outlined" sx={{ p: { xs: 2, md: 2.5 }, mb: 2.5 }}>
        <Stack direction={{ xs: "column", lg: "row" }} justifyContent="space-between" gap={2.5}>
          <Stack direction="row" gap={1} flexWrap="wrap" alignItems="center">
            <StatusChip value={action.status} size="medium" />
            <Chip label={action.destination} variant="outlined" sx={{ maxWidth: { xs: "100%", md: 440 }, "& .MuiChip-label": { overflow: "hidden", textOverflow: "ellipsis" } }} />
          </Stack>
          <Stack direction={{ xs: "column", sm: "row" }} gap={{ xs: 0.5, sm: 3 }}>
            <Meta label="Created" value={formatDate(action.created_at)} />
            <Meta label="Completed" value={formatDate(action.completed_at)} />
            <Meta label="Cycle" value={String(action.retry_cycle)} />
          </Stack>
        </Stack>
        {action.external_reference && (
          <Stack direction="row" gap={0.75} alignItems="center" sx={{ mt: 2 }}>
            <OpenInNewIcon fontSize="small" color="action" />
            <Typography variant="body2" sx={{ overflowWrap: "anywhere" }}>{action.external_reference}</Typography>
          </Stack>
        )}
      </Paper>

      <Stack direction={{ xs: "column", lg: "row" }} gap={2.5} alignItems="flex-start">
        <Box sx={{ flex: 1, width: "100%", minWidth: 0 }}>
          <Typography variant="h6" sx={{ mb: 1.25 }}>Rendered payload</Typography>
          <Paper variant="outlined" sx={{ p: 2, bgcolor: "#f8f9fa" }}>
            {typeof action.rendered_payload.body === "string" ? (
              <Stack gap={1.5}>
                {typeof action.rendered_payload.subject === "string" && (
                  <Box>
                    <Typography variant="caption" color="text.secondary">Subject</Typography>
                    <Typography variant="body2" fontWeight={650}>{action.rendered_payload.subject.trim()}</Typography>
                  </Box>
                )}
                <Divider />
                <MarkdownContent content={action.rendered_payload.body} />
              </Stack>
            ) : (
              <Typography component="pre" variant="body2" sx={{ m: 0, whiteSpace: "pre-wrap", overflowWrap: "anywhere", fontFamily: "ui-monospace, monospace" }}>
                {JSON.stringify(action.rendered_payload, null, 2)}
              </Typography>
            )}
          </Paper>
        </Box>
        <Box sx={{ flex: 1, width: "100%", minWidth: 0 }}>
          <Typography variant="h6" sx={{ mb: 1.25 }}>Delivery attempts</Typography>
          <Paper variant="outlined" sx={{ overflow: "hidden" }}>
            {detail.attempts.length === 0 ? (
              <Box sx={{ p: 3, textAlign: "center" }}><Typography color="text.secondary">No attempts yet</Typography></Box>
            ) : detail.attempts.map((attempt, index) => (
              <Box key={attempt.id} sx={{ p: 2, borderTop: index ? "1px solid" : 0, borderColor: "divider" }}>
                <Stack direction="row" justifyContent="space-between" gap={1}>
                  <Box>
                    <Typography variant="body2" fontWeight={650}>Cycle {attempt.retry_cycle}, attempt {attempt.attempt_number}</Typography>
                    <Typography variant="caption" color="text.secondary">{formatDate(attempt.started_at)}</Typography>
                  </Box>
                  <StatusChip value={attempt.status} />
                </Stack>
                {attempt.error_summary && <Typography variant="body2" color="error.main" sx={{ mt: 1 }}>{attempt.error_summary}</Typography>}
                {Object.keys(attempt.response_metadata).length > 0 && (
                  <>
                    <Divider sx={{ my: 1 }} />
                    <Typography variant="caption" color="text.secondary" sx={{ overflowWrap: "anywhere" }}>
                      {JSON.stringify(attempt.response_metadata)}
                    </Typography>
                  </>
                )}
              </Box>
            ))}
          </Paper>
        </Box>
      </Stack>
    </Box>
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
