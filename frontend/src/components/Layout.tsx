import {
  AppBar,
  Box,
  Button,
  Container,
  Toolbar,
  Typography,
} from "@mui/material";
import DashboardIcon from "@mui/icons-material/Dashboard";
import BugReportIcon from "@mui/icons-material/BugReport";
import ConfirmationNumberIcon from "@mui/icons-material/ConfirmationNumber";
import ChatIcon from "@mui/icons-material/Chat";
import { Outlet, useLocation, useNavigate } from "react-router-dom";

const navItems = [
  { label: "Dashboard", path: "/", icon: <DashboardIcon fontSize="small" /> },
  { label: "Findings", path: "/findings", icon: <BugReportIcon fontSize="small" /> },
  { label: "Jira Issues", path: "/jira", icon: <ConfirmationNumberIcon fontSize="small" /> },
  { label: "Chat", path: "/chat", icon: <ChatIcon fontSize="small" /> },
];

export default function Layout() {
  const location = useLocation();
  const navigate = useNavigate();

  return (
    <Box sx={{ display: "flex", flexDirection: "column", minHeight: "100vh" }}>
      <AppBar position="sticky" elevation={0} sx={{ bgcolor: "background.paper", borderBottom: "1px solid rgba(255,255,255,0.06)" }}>
        <Toolbar>
          <Typography variant="h6" sx={{ mr: 4, fontWeight: 700, color: "primary.main" }}>
            Jenkins Watchdog
          </Typography>
          <Box sx={{ display: "flex", gap: 1 }}>
            {navItems.map((item) => (
              <Button
                key={item.path}
                startIcon={item.icon}
                onClick={() => navigate(item.path)}
                variant={location.pathname === item.path ? "contained" : "text"}
                size="small"
                sx={{ textTransform: "none" }}
              >
                {item.label}
              </Button>
            ))}
          </Box>
        </Toolbar>
      </AppBar>
      <Container maxWidth="xl" sx={{ py: 3, flex: 1 }}>
        <Outlet />
      </Container>
    </Box>
  );
}
