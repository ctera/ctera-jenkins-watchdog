import { Alert, Box, CircularProgress, Typography } from "@mui/material";

export function LoadingPanel({ label = "Loading" }: { label?: string }) {
  return (
    <Box sx={{ minHeight: 180, display: "grid", placeItems: "center" }}>
      <Box sx={{ textAlign: "center" }}>
        <CircularProgress size={28} />
        <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
          {label}
        </Typography>
      </Box>
    </Box>
  );
}

export function ErrorPanel({ error }: { error: unknown }) {
  return <Alert severity="error">{error instanceof Error ? error.message : "Request failed"}</Alert>;
}

export function EmptyPanel({ label }: { label: string }) {
  return (
    <Box sx={{ py: 7, textAlign: "center", borderBlock: "1px solid", borderColor: "divider" }}>
      <Typography color="text.secondary">{label}</Typography>
    </Box>
  );
}
