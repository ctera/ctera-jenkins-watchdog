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
import { useCallback, useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import ChatPanel from "../components/ChatPanel";
import PageHeader from "../components/PageHeader";
import { ErrorPanel, LoadingPanel } from "../components/StatePanel";
import StatusChip from "../components/StatusChip";
import {
  getIncident,
  reinvestigateIncident,
  suppressIncident,
  unsuppressIncident,
  type IncidentDetail,
} from "../services/api";
import { formatDate, titleCase } from "../utils/format";

const tabs = ["Overview", "Observations", "Investigation", "Actions", "Chat"];

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
              disabled={working}
              onClick={() => void reinvestigate()}
            >
              Reinvestigate
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
            <Chip variant="outlined" label={`Occurrence #${incident.occurrence_number}`} />
            <Chip variant="outlined" label={titleCase(String(incident.source.kind ?? "unknown"))} />
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
          {tabs.map((label, index) => (
            <Tab key={label} label={label === "Observations" || label === "Actions" ? `${label} (${label === "Observations" ? detail.observations.length : detail.actions.length})` : label} id={`incident-tab-${index}`} />
          ))}
        </Tabs>
        <Box sx={{ p: { xs: 2, md: 2.5 } }}>
          {tab === 0 && <Overview detail={detail} />}
          {tab === 1 && <Observations detail={detail} />}
          {tab === 2 && <InvestigationView detail={detail} />}
          {tab === 3 && <ActionsView detail={detail} onOpen={(id) => navigate(`/actions/${id}`)} />}
          {tab === 4 && <ChatPanel incidentId={incident.id} />}
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
  return (
    <Stack gap={3}>
      <Box>
        <Typography variant="h6" sx={{ mb: 1.25 }}>Source association</Typography>
        <KeyValues value={incident.source} />
      </Box>
      <Divider />
      <Box>
        <Typography variant="h6" sx={{ mb: 1.25 }}>Triage</Typography>
        <Stack direction={{ xs: "column", sm: "row" }} gap={3}>
          <Meta label="Actionability" value={incident.actionability ? titleCase(incident.actionability) : "-"} />
          <Meta label="Classification" value={incident.classification ? titleCase(incident.classification) : "-"} />
          <Meta label="Priority" value={incident.priority ? titleCase(incident.priority) : "-"} />
        </Stack>
      </Box>
      <Divider />
      <Box>
        <Typography variant="h6" sx={{ mb: 1.25 }}>Occurrences</Typography>
        <Stack divider={<Divider flexItem />}>
          {detail.occurrences.map((occurrence) => (
            <Stack key={occurrence.id} direction={{ xs: "column", md: "row" }} gap={2} sx={{ py: 1.5 }}>
              <Typography fontWeight={700} sx={{ width: 110 }}>#{occurrence.number}</Typography>
              <Box sx={{ flex: 1 }}>
                <Typography variant="body2">{formatDate(occurrence.opened_at)} to {formatDate(occurrence.resolved_at)}</Typography>
                <Typography variant="caption" color="text.secondary">
                  {occurrence.responsible_checks.map(titleCase).join(", ")}
                </Typography>
              </Box>
              <Typography variant="body2" color="text.secondary">{occurrence.observation_identities.length} observations</Typography>
            </Stack>
          ))}
        </Stack>
      </Box>
    </Stack>
  );
}

function Observations({ detail }: { detail: IncidentDetail }) {
  if (!detail.observations.length) return <Typography color="text.secondary">No observations</Typography>;
  return (
    <Stack gap={2} divider={<Divider flexItem />}>
      {detail.observations.map((item) => (
        <Box key={`${item.scan_id}-${item.stable_identity}`} sx={{ py: 1 }}>
          <Stack direction={{ xs: "column", sm: "row" }} justifyContent="space-between" gap={1}>
            <Box sx={{ minWidth: 0 }}>
              <Typography fontWeight={650}>{item.summary}</Typography>
              <Typography variant="caption" color="text.secondary">{item.resource_id} · {formatDate(item.observed_at)}</Typography>
            </Box>
            <Stack direction="row" gap={1} alignItems="center">
              <StatusChip value={item.severity} />
              <Chip size="small" variant="outlined" label={titleCase(item.category)} />
            </Stack>
          </Stack>
          {Object.keys(item.evidence).length > 0 && (
            <Typography component="pre" variant="caption" sx={{ mt: 1.25, p: 1.5, bgcolor: "#f8f9fa", border: "1px solid", borderColor: "divider", whiteSpace: "pre-wrap", overflowWrap: "anywhere", fontFamily: "ui-monospace, monospace" }}>
              {JSON.stringify(item.evidence, null, 2)}
            </Typography>
          )}
        </Box>
      ))}
    </Stack>
  );
}

function InvestigationView({ detail }: { detail: IncidentDetail }) {
  const investigation = detail.latest_investigation;
  if (!investigation) return <Typography color="text.secondary">No investigation recorded</Typography>;
  return (
    <Stack gap={2.5}>
      <Stack direction="row" gap={1} flexWrap="wrap">
        <StatusChip value={investigation.status} />
        {investigation.confidence && <Chip size="small" label={`${titleCase(investigation.confidence)} confidence`} variant="outlined" />}
        <Chip size="small" label={investigation.model} variant="outlined" />
      </Stack>
      {investigation.error_summary && <Alert severity="error">{investigation.error_summary}</Alert>}
      <KeyValues value={investigation.result} />
      <Divider />
      <Stack direction={{ xs: "column", md: "row" }} gap={3}>
        <Meta label="Evidence hash" value={investigation.evidence_hash} />
        <Meta label="Prompt" value={investigation.prompt_version} />
        <Meta label="Completed" value={formatDate(investigation.completed_at)} />
      </Stack>
      {Object.keys(investigation.usage).length > 0 && <KeyValues value={investigation.usage} />}
    </Stack>
  );
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

function KeyValues({ value }: { value: Record<string, unknown> }) {
  const entries = Object.entries(value);
  if (!entries.length) return <Typography color="text.secondary">Unknown</Typography>;
  return (
    <Box sx={{ display: "grid", gridTemplateColumns: { xs: "minmax(0, 1fr)", sm: "repeat(2, minmax(0, 1fr))" }, gap: 1.5 }}>
      {entries.map(([key, item]) => (
        <Box key={key} sx={{ minWidth: 0 }}>
          <Typography variant="caption" color="text.secondary">{titleCase(key)}</Typography>
          <Typography variant="body2" sx={{ overflowWrap: "anywhere", whiteSpace: "pre-wrap" }}>
            {typeof item === "string" || typeof item === "number" || typeof item === "boolean" ? String(item) : JSON.stringify(item)}
          </Typography>
        </Box>
      ))}
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
