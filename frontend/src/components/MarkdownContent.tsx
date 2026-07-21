import { Box } from "@mui/material";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

function normalizeMarkdown(content: string): string {
  return content
    .replace(/\r\n?/g, "\n")
    .replace(/([.!?:;])\s+(\d+\.\s+(?=(?:\*\*|__)))/g, "$1\n$2");
}

export default function MarkdownContent({ content }: { content: string }) {
  return (
    <Box
      sx={{
        color: "text.primary",
        overflowWrap: "anywhere",
        "& > :first-of-type": { mt: 0 },
        "& > :last-child": { mb: 0 },
        "& h1": { fontSize: "1.25rem", lineHeight: 1.35, mt: 2.5, mb: 1, fontWeight: 700 },
        "& h2": { fontSize: "1.1rem", lineHeight: 1.4, mt: 2.25, mb: 0.75, fontWeight: 700 },
        "& h3": { fontSize: "1rem", lineHeight: 1.45, mt: 2, mb: 0.5, fontWeight: 700 },
        "& p, & li": { fontSize: "0.875rem", lineHeight: 1.65 },
        "& p": { my: 1, whiteSpace: "pre-line" },
        "& ul, & ol": { my: 1, pl: 3 },
        "& li + li": { mt: 0.5 },
        "& blockquote": { my: 1.5, mx: 0, pl: 1.5, borderLeft: "3px solid", borderColor: "divider", color: "text.secondary" },
        "& hr": { my: 2, border: 0, borderTop: "1px solid", borderColor: "divider" },
        "& code": { px: 0.5, py: 0.15, bgcolor: "#eef0f2", borderRadius: 0.5, fontFamily: "ui-monospace, SFMono-Regular, monospace", fontSize: "0.82rem" },
        "& pre": { my: 1.5, p: 1.5, overflowX: "auto", bgcolor: "#202124", color: "#f8f9fa", borderRadius: 1 },
        "& pre code": { p: 0, bgcolor: "transparent", color: "inherit" },
        "& table": { width: "100%", my: 1.5, borderCollapse: "collapse", fontSize: "0.82rem" },
        "& th, & td": { px: 1, py: 0.75, border: "1px solid", borderColor: "divider", textAlign: "left", verticalAlign: "top" },
        "& th": { bgcolor: "#f8f9fa", fontWeight: 700 },
        "& a": { color: "primary.main" },
      }}
    >
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{normalizeMarkdown(content)}</ReactMarkdown>
    </Box>
  );
}
