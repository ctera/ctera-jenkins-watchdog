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
import { listActions, type Action, type ActionFilters } from "../services/api";
import { formatRelative, titleCase } from "../utils/format";

const statuses = ["pending", "running", "retry_scheduled", "succeeded", "permanently_failed"];
const types = ["email", "jira_create", "jira_update", "github_comment", "gitlab_comment"];

export default function Actions() {
  const navigate = useNavigate();
  const [filters, setFilters] = useState<ActionFilters>({});
  const [actions, setActions] = useState<Action[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const page = await listActions(filters);
      setActions(page.items);
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
    const page = await listActions(filters, cursor);
    setActions((current) => [...current, ...page.items]);
    setCursor(page.next_cursor);
  }

  return (
    <Box>
      <PageHeader
        title="Actions"
        actions={<Button variant="outlined" startIcon={<RefreshIcon />} onClick={() => void refresh()}>Refresh</Button>}
      />
      <Stack direction={{ xs: "column", sm: "row" }} gap={1.5} sx={{ mb: 2.5 }}>
        <Filter
          label="Status"
          value={filters.status ?? ""}
          options={statuses}
          onChange={(value) => setFilters((current) => ({ ...current, status: (value || undefined) as ActionFilters["status"] }))}
        />
        <Filter
          label="Type"
          value={filters.action_type ?? ""}
          options={types}
          onChange={(value) => setFilters((current) => ({ ...current, action_type: (value || undefined) as ActionFilters["action_type"] }))}
        />
      </Stack>
      {Boolean(error) && <Box sx={{ mb: 2 }}><ErrorPanel error={error} /></Box>}
      {loading ? <LoadingPanel label="Loading actions" /> : actions.length === 0 ? <EmptyPanel label="No matching actions" /> : (
        <>
          <TableContainer component={Paper} variant="outlined">
            <Table sx={{ minWidth: 800 }}>
              <TableHead><TableRow><TableCell>Status</TableCell><TableCell>Type</TableCell><TableCell>Destination</TableCell><TableCell>Attempts</TableCell><TableCell>Next attempt</TableCell><TableCell>Created</TableCell></TableRow></TableHead>
              <TableBody>
                {actions.map((action) => (
                  <TableRow
                    hover
                    key={action.id}
                    tabIndex={0}
                    onClick={() => navigate(`/actions/${action.id}`)}
                    onKeyDown={(event) => event.key === "Enter" && navigate(`/actions/${action.id}`)}
                    sx={{ cursor: "pointer" }}
                  >
                    <TableCell><StatusChip value={action.status} /></TableCell>
                    <TableCell>{titleCase(action.action_type)}</TableCell>
                    <TableCell sx={{ maxWidth: 320 }}><Typography variant="body2" noWrap>{action.destination}</Typography></TableCell>
                    <TableCell>{action.attempt_count}</TableCell>
                    <TableCell>{formatRelative(action.next_attempt_at)}</TableCell>
                    <TableCell>{formatRelative(action.created_at)}</TableCell>
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

function Filter({ label, value, options, onChange }: { label: string; value: string; options: string[]; onChange: (value: string) => void }) {
  return (
    <FormControl size="small" sx={{ minWidth: { xs: 0, sm: 210 } }}>
      <InputLabel>{label}</InputLabel>
      <Select value={value} label={label} onChange={(event) => onChange(event.target.value)}>
        <MenuItem value="">All</MenuItem>
        {options.map((option) => <MenuItem value={option} key={option}>{titleCase(option)}</MenuItem>)}
      </Select>
    </FormControl>
  );
}
