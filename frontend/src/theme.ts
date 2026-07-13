import { createTheme } from "@mui/material/styles";

export const theme = createTheme({
  palette: {
    mode: "light",
    primary: { main: "#b42318", dark: "#7a271a", light: "#fecdca" },
    secondary: { main: "#087e6a" },
    error: { main: "#b42318" },
    warning: { main: "#b54708" },
    success: { main: "#067647" },
    info: { main: "#175cd3" },
    background: { default: "#f5f6f7", paper: "#ffffff" },
    text: { primary: "#202124", secondary: "#5f6368" },
    divider: "#dfe3e8",
  },
  typography: {
    fontFamily: "Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
    h4: { fontSize: "1.75rem", fontWeight: 700, letterSpacing: 0 },
    h5: { fontSize: "1.35rem", fontWeight: 700, letterSpacing: 0 },
    h6: { fontSize: "1rem", fontWeight: 700, letterSpacing: 0 },
    subtitle1: { fontWeight: 600, letterSpacing: 0 },
    button: { fontWeight: 650, letterSpacing: 0, textTransform: "none" },
    body1: { letterSpacing: 0 },
    body2: { letterSpacing: 0 },
    caption: { letterSpacing: 0 },
  },
  shape: { borderRadius: 6 },
  components: {
    MuiButton: { defaultProps: { disableElevation: true } },
    MuiCard: {
      styleOverrides: {
        root: { backgroundImage: "none", border: "1px solid #dfe3e8", boxShadow: "none" },
      },
    },
    MuiPaper: {
      styleOverrides: { root: { backgroundImage: "none" } },
    },
    MuiChip: {
      styleOverrides: { root: { fontWeight: 600, borderRadius: 5 } },
    },
    MuiTableCell: {
      styleOverrides: {
        head: { color: "#5f6368", fontWeight: 700, backgroundColor: "#f8f9fa" },
        root: { borderColor: "#e6e8eb" },
      },
    },
    MuiTooltip: { defaultProps: { arrow: true } },
  },
});
