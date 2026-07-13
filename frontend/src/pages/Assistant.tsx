import { Box, Paper } from "@mui/material";
import ChatPanel from "../components/ChatPanel";
import PageHeader from "../components/PageHeader";

export default function Assistant() {
  return (
    <Box>
      <PageHeader title="Assistant" />
      <Paper variant="outlined" sx={{ p: { xs: 2, md: 2.5 } }}>
        <ChatPanel />
      </Paper>
    </Box>
  );
}
