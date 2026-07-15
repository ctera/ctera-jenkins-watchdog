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
  TextField,
  Typography,
} from "@mui/material";
import RefreshIcon from "@mui/icons-material/Refresh";
import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import PageHeader from "../components/PageHeader";
import { SourceSummary } from "../components/SourceAttribution";
import { EmptyPanel, ErrorPanel, LoadingPanel } from "../components/StatePanel";
import StatusChip from "../components/StatusChip";
import { listIncidents, type Incident, type IncidentFilters } from "../services/api";
import { formatRelative, titleCase } from "../utils/format";

export default function Incidents() {
  const navigate = useNavigate();
  const [filters, setFilters] = useState<IncidentFilters>({ status: "open" });
  const [search, setSearch] = useState("");
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

  const visibleIncidents = incidents
    .filter((incident) => !search.trim() || `${incident.title} ${incident.domain}`.toLowerCase().includes(search.trim().toLowerCase()))
    .sort((left, right) => {
      const severity = { critical: 2, warning: 1, low: 0 };
      return (severity[right.severity as keyof typeof severity] ?? 0) - (severity[left.severity as keyof typeof severity] ?? 0)
        || right.affected_resource_count - left.affected_resource_count;
    });

  return (
    <Box>
      <PageHeader
        title="Incidents"
        actions={<Button variant="outlined" startIcon={<RefreshIcon />} onClick={() => void refresh()}>Refresh</Button>}
      />
      <Stack direction={{ xs: "column", sm: "row" }} gap={1.5} sx={{ mb: 2.5 }}>
        <TextField size="small" label="Search conditions" value={search} onChange={(event) => setSearch(event.target.value)} sx={{ minWidth: { xs: 0, sm: 240 } }} />
        <Filter label="Status" value={filters.status ?? ""} onChange={(value) => setFilter("status", value as IncidentFilters["status"])} options={["open", "resolved", "suppressed"]} />
        <Filter label="Severity" value={filters.severity ?? ""} onChange={(value) => setFilter("severity", value as IncidentFilters["severity"])} options={["critical", "warning", "low"]} />
        <Filter label="Source" value={filters.source_type ?? ""} onChange={(value) => setFilter("source_type", value as IncidentFilters["source_type"])} options={["merge_request", "repository", "pipeline", "multiple", "infrastructure", "unknown"]} />
      </Stack>

      {Boolean(error) && <Box sx={{ mb: 2 }}><ErrorPanel error={error} /></Box>}
      {loading ? <LoadingPanel label="Loading incidents" /> : visibleIncidents.length === 0 ? <EmptyPanel label="No matching incidents" /> : (
        <>
          <TableContainer component={Paper} variant="outlined">
            <Table sx={{ minWidth: 960, tableLayout: "fixed" }}>
              <TableHead>
                <TableRow>
                  <TableCell sx={{ width: 105 }}>Severity</TableCell>
                  <TableCell sx={{ width: 320 }}>Incident</TableCell>
                  <TableCell sx={{ width: 75 }}>Affected</TableCell>
                  <TableCell sx={{ width: 70 }}>Area</TableCell>
                  <TableCell sx={{ width: 145 }}>Source</TableCell>
                  <TableCell sx={{ width: 90 }}>Status</TableCell>
                  <TableCell sx={{ width: 70 }}>First seen</TableCell>
                  <TableCell sx={{ width: 70 }}>Last seen</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {visibleIncidents.map((incident) => (
                  <TableRow
                    hover
                    key={incident.id}
                    tabIndex={0}
                    onClick={() => navigate(`/incidents/${incident.id}`)}
                    onKeyDown={(event) => event.key === "Enter" && navigate(`/incidents/${incident.id}`)}
                    sx={{ cursor: "pointer" }}
                  >
                    <TableCell><StatusChip value={incident.severity} /></TableCell>
                    <TableCell sx={{ maxWidth: 320 }}>
                      <Typography variant="body2" fontWeight={650} noWrap>{incident.title}</Typography>
                      <Typography variant="caption" color="text.secondary">Occurrence #{incident.occurrence_number}</Typography>
                    </TableCell>
                    <TableCell>{incident.affected_resource_count}</TableCell>
                    <TableCell>{titleCase(incident.domain)}</TableCell>
                    <TableCell><SourceSummary source={incident.source} /></TableCell>
                    <TableCell><StatusChip value={incident.status} /></TableCell>
                    <TableCell>{formatRelative(incident.first_seen_at ?? incident.created_at)}</TableCell>
                    <TableCell>{formatRelative(incident.last_seen_at ?? incident.updated_at ?? incident.created_at)}</TableCell>
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
