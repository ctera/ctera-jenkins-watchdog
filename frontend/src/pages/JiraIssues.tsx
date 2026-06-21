import { useEffect, useState } from "react";
import {
  Box,
  Chip,
  CircularProgress,
  Link,
  Paper,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Typography,
} from "@mui/material";
import OpenInNewIcon from "@mui/icons-material/OpenInNew";

interface JiraIssue {
  key: string;
  url: string;
  project: string;
  issue_type: string;
  summary: string;
  assignee: string;
  status: string;
  created_at: string;
}

function formatDate(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" }) +
    " " + d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
}

export default function JiraIssues() {
  const [issues, setIssues] = useState<JiraIssue[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetch("/api/jira/issues")
      .then((r) => r.json())
      .then((data) => setIssues(data.issues || []))
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  if (loading) {
    return (
      <Box sx={{ display: "flex", justifyContent: "center", py: 6 }}>
        <CircularProgress />
      </Box>
    );
  }

  if (issues.length === 0) {
    return (
      <Box sx={{ textAlign: "center", py: 6 }}>
        <Typography color="text.secondary">No Jira issues created yet. Use the "Create Issue" button on a finding to open one.</Typography>
      </Box>
    );
  }

  return (
    <Box>
      <Typography variant="h5" sx={{ mb: 2, fontWeight: 600 }}>
        Jira Issues ({issues.length})
      </Typography>
      <TableContainer component={Paper} sx={{ bgcolor: "background.paper" }}>
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell sx={{ fontWeight: 600 }}>Issue</TableCell>
              <TableCell sx={{ fontWeight: 600 }}>Summary</TableCell>
              <TableCell sx={{ fontWeight: 600 }}>Assignee</TableCell>
              <TableCell sx={{ fontWeight: 600 }}>Status</TableCell>
              <TableCell sx={{ fontWeight: 600 }}>Project</TableCell>
              <TableCell sx={{ fontWeight: 600 }}>Type</TableCell>
              <TableCell sx={{ fontWeight: 600 }}>Created</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {issues.map((issue) => (
              <TableRow key={issue.key} hover>
                <TableCell>
                  <Link href={issue.url} target="_blank" rel="noopener" sx={{ display: "flex", alignItems: "center", gap: 0.5, fontWeight: 600 }}>
                    {issue.key} <OpenInNewIcon sx={{ fontSize: 14 }} />
                  </Link>
                </TableCell>
                <TableCell sx={{ maxWidth: 400, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {issue.summary}
                </TableCell>
                <TableCell>
                  {issue.assignee ? (
                    <Chip label={issue.assignee} size="small" variant="outlined" />
                  ) : (
                    <Typography variant="caption" color="text.secondary">Unassigned</Typography>
                  )}
                </TableCell>
                <TableCell>
                  <Chip label={issue.status || "Unknown"} size="small" color={issue.status === "Done" ? "success" : "default"} variant="outlined" />
                </TableCell>
                <TableCell>
                  <Chip label={issue.project} size="small" />
                </TableCell>
                <TableCell>
                  <Chip label={issue.issue_type} size="small" variant="outlined" />
                </TableCell>
                <TableCell>
                  <Typography variant="caption">{formatDate(issue.created_at)}</Typography>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </TableContainer>
    </Box>
  );
}
