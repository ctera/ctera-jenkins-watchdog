import {
  Box,
  Button,
  FormControl,
  InputLabel,
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
  Typography,
} from "@mui/material";
import RefreshIcon from "@mui/icons-material/Refresh";
import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import PageHeader from "../components/PageHeader";
import { EmptyPanel, ErrorPanel, LoadingPanel } from "../components/StatePanel";
import StatusChip from "../components/StatusChip";
import { listIncidents, type Incident, type IncidentFilters } from "../services/api";
import { formatRelative, titleCase } from "../utils/format";

export default function Incidents() {
  const navigate = useNavigate();
  const [filters, setFilters] = useState<IncidentFilters>({});
  const [incidents, setIncidents] = useState<Incident[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const page = await listIncidents(filters);
      setIncidents(page.items);
      setCursor(page.next_cursor);
      setError(null);
    } catch (requestError) {
      setError(requestError);
    } finally {
      setLoading(false);
    }
  }, [filters]);

  useEffect(() => void refresh(), [refresh]);

  async function loadMore() {
    if (!cursor) return;
    const page = await listIncidents(filters, cursor);
    setIncidents((current) => [...current, ...page.items]);
    setCursor(page.next_cursor);
  }

  function setFilter<K extends keyof IncidentFilters>(name: K, value: IncidentFilters[K] | "") {
    setFilters((current) => ({ ...current, [name]: value || undefined }));
  }

  return (
    <Box>
      <PageHeader
        title="Incidents"
        actions={<Button variant="outlined" startIcon={<RefreshIcon />} onClick={() => void refresh()}>Refresh</Button>}
      />
      <Stack direction={{ xs: "column", sm: "row" }} gap={1.5} sx={{ mb: 2.5 }}>
        <Filter label="Status" value={filters.status ?? ""} onChange={(value) => setFilter("status", value as IncidentFilters["status"])} options={["open", "resolved", "suppressed"]} />
        <Filter label="Severity" value={filters.severity ?? ""} onChange={(value) => setFilter("severity", value as IncidentFilters["severity"])} options={["critical", "warning", "low"]} />
        <Filter label="Source" value={filters.source_type ?? ""} onChange={(value) => setFilter("source_type", value as IncidentFilters["source_type"])} options={["merge_request", "infrastructure", "unknown"]} />
      </Stack>

      {Boolean(error) && <Box sx={{ mb: 2 }}><ErrorPanel error={error} /></Box>}
      {loading ? <LoadingPanel label="Loading incidents" /> : incidents.length === 0 ? <EmptyPanel label="No matching incidents" /> : (
        <>
          <TableContainer component={Paper} variant="outlined">
            <Table sx={{ minWidth: 840 }}>
              <TableHead>
                <TableRow>
                  <TableCell>Severity</TableCell>
                  <TableCell>Incident</TableCell>
                  <TableCell>Status</TableCell>
                  <TableCell>Source</TableCell>
                  <TableCell>Occurrence</TableCell>
                  <TableCell>Updated</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {incidents.map((incident) => (
                  <TableRow
                    hover
                    key={incident.id}
                    tabIndex={0}
                    onClick={() => navigate(`/incidents/${incident.id}`)}
                    onKeyDown={(event) => event.key === "Enter" && navigate(`/incidents/${incident.id}`)}
                    sx={{ cursor: "pointer" }}
                  >
                    <TableCell><StatusChip value={incident.severity} /></TableCell>
                    <TableCell sx={{ maxWidth: 420 }}>
                      <Typography variant="body2" fontWeight={650} noWrap>{incident.title}</Typography>
                      <Typography variant="caption" color="text.secondary">{incident.correlation_rule_id}</Typography>
                    </TableCell>
                    <TableCell><StatusChip value={incident.status} /></TableCell>
                    <TableCell>{titleCase(String(incident.source.kind ?? "unknown"))}</TableCell>
                    <TableCell>#{incident.occurrence_number}</TableCell>
                    <TableCell>{formatRelative(incident.updated_at ?? incident.created_at)}</TableCell>
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

function Filter({ label, value, onChange, options }: { label: string; value: string; onChange: (value: string) => void; options: string[] }) {
  return (
    <FormControl size="small" sx={{ minWidth: { xs: 0, sm: 170 } }}>
      <InputLabel>{label}</InputLabel>
      <Select label={label} value={value} onChange={(event) => onChange(event.target.value)}>
        <MenuItem value="">All</MenuItem>
        {options.map((option) => <MenuItem key={option} value={option}>{titleCase(option)}</MenuItem>)}
      </Select>
    </FormControl>
  );
}
