import {
  AppBar,
  BottomNavigation,
  BottomNavigationAction,
  Box,
  Container,
  Divider,
  Drawer,
  List,
  ListItemButton,
  ListItemIcon,
  ListItemText,
  Toolbar,
  Typography,
  useMediaQuery,
  useTheme,
} from "@mui/material";
import RadarIcon from "@mui/icons-material/Radar";
import DashboardOutlinedIcon from "@mui/icons-material/DashboardOutlined";
import ReportProblemOutlinedIcon from "@mui/icons-material/ReportProblemOutlined";
import SendOutlinedIcon from "@mui/icons-material/SendOutlined";
import ForumOutlinedIcon from "@mui/icons-material/ForumOutlined";
import ShieldOutlinedIcon from "@mui/icons-material/ShieldOutlined";
import { Outlet, useLocation, useNavigate } from "react-router-dom";

const drawerWidth = 248;

const navItems = [
  { label: "Scans", path: "/scans", icon: <RadarIcon /> },
  { label: "Jenkins", path: "/overview", icon: <DashboardOutlinedIcon /> },
  { label: "Incidents", path: "/incidents", icon: <ReportProblemOutlinedIcon /> },
  { label: "Actions", path: "/actions", icon: <SendOutlinedIcon /> },
  { label: "Assistant", path: "/assistant", icon: <ForumOutlinedIcon /> },
];

function selectedPath(pathname: string): string {
  return navItems.find((item) => pathname.startsWith(item.path))?.path ?? "/scans";
}

export default function Layout() {
  const location = useLocation();
  const navigate = useNavigate();
  const theme = useTheme();
  const desktop = useMediaQuery(theme.breakpoints.up("md"));
  const active = selectedPath(location.pathname);

  const brand = (
    <Toolbar sx={{ minHeight: 64, px: 2.25, gap: 1.25 }}>
      <Box
        sx={{
          width: 34,
          height: 34,
          display: "grid",
          placeItems: "center",
          color: "common.white",
          bgcolor: "primary.main",
          borderRadius: 1,
          flexShrink: 0,
        }}
      >
        <ShieldOutlinedIcon fontSize="small" />
      </Box>
      <Box sx={{ minWidth: 0 }}>
        <Typography variant="subtitle1" noWrap>
          Jenkins Watchdog
        </Typography>
      </Box>
    </Toolbar>
  );

  return (
    <Box sx={{ minHeight: "100vh", display: "flex" }}>
      {desktop ? (
        <Drawer
          variant="permanent"
          sx={{
            width: drawerWidth,
            flexShrink: 0,
            "& .MuiDrawer-paper": { width: drawerWidth, boxSizing: "border-box", borderRightColor: "divider" },
          }}
        >
          {brand}
          <Divider />
          <List sx={{ px: 1.25, py: 2 }}>
            {navItems.map((item) => (
              <ListItemButton
                key={item.path}
                selected={active === item.path}
                onClick={() => navigate(item.path)}
                sx={{ mb: 0.5, minHeight: 44 }}
              >
                <ListItemIcon sx={{ minWidth: 38 }}>{item.icon}</ListItemIcon>
                <ListItemText primary={item.label} primaryTypographyProps={{ fontWeight: 650 }} />
              </ListItemButton>
            ))}
          </List>
        </Drawer>
      ) : (
        <AppBar position="fixed" color="inherit" elevation={0} sx={{ borderBottom: "1px solid", borderColor: "divider" }}>
          {brand}
        </AppBar>
      )}

      <Box component="main" sx={{ flex: 1, minWidth: 0, pt: { xs: 8, md: 0 }, pb: { xs: 9, md: 0 } }}>
        <Container maxWidth="xl" sx={{ py: { xs: 2.5, md: 3.5 }, px: { xs: 2, sm: 3 } }}>
          <Outlet />
        </Container>
      </Box>

      {!desktop && (
        <BottomNavigation
          showLabels
          value={active}
          onChange={(_, path) => navigate(path)}
          sx={{
            position: "fixed",
            zIndex: theme.zIndex.appBar,
            insetInline: 0,
            bottom: 0,
            height: 68,
            borderTop: "1px solid",
            borderColor: "divider",
          }}
        >
          {navItems.map((item) => (
            <BottomNavigationAction
              key={item.path}
              value={item.path}
              label={item.label}
              icon={item.icon}
              sx={{ minWidth: 0, px: 0.5 }}
            />
          ))}
        </BottomNavigation>
      )}
    </Box>
  );
}
