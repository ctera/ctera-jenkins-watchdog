import { Box, IconButton, Stack, TextField, Tooltip, Typography } from "@mui/material";
import SendIcon from "@mui/icons-material/Send";
import { useState, type FormEvent } from "react";
import { chat } from "../services/api";

interface Message {
  id: number;
  role: "user" | "assistant" | "error";
  content: string;
}

export default function ChatPanel({ incidentId }: { incidentId?: string }) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [message, setMessage] = useState("");
  const [sending, setSending] = useState(false);

  async function send() {
    const content = message.trim();
    if (!content || sending) return;
    const id = Date.now();
    setMessages((current) => [...current, { id, role: "user", content }]);
    setMessage("");
    setSending(true);
    try {
      const response = await chat(content, incidentId);
      setMessages((current) => [...current, { id: id + 1, role: "assistant", content: response }]);
    } catch (error) {
      setMessages((current) => [
        ...current,
        { id: id + 1, role: "error", content: error instanceof Error ? error.message : "Reasoning failed" },
      ]);
    } finally {
      setSending(false);
    }
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    void send();
  }

  return (
    <Stack sx={{ minHeight: 420 }}>
      <Box sx={{ flex: 1, py: 1, display: "flex", flexDirection: "column", gap: 1.5 }} aria-live="polite">
        {messages.length === 0 && (
          <Box sx={{ py: 8, textAlign: "center" }}>
            <Typography color="text.secondary">No conversation yet</Typography>
          </Box>
        )}
        {messages.map((item) => (
          <Box
            key={item.id}
            sx={{
              alignSelf: item.role === "user" ? "flex-end" : "flex-start",
              maxWidth: { xs: "92%", md: "75%" },
              px: 1.75,
              py: 1.25,
              borderRadius: 1.5,
              bgcolor: item.role === "user" ? "#f0f4ff" : item.role === "error" ? "#fee4e2" : "#f1f3f4",
              border: "1px solid",
              borderColor: item.role === "error" ? "#fecdca" : "divider",
            }}
          >
            <Typography variant="body2" sx={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
              {item.content}
            </Typography>
          </Box>
        ))}
        {sending && <Typography variant="body2" color="text.secondary">Analyzing...</Typography>}
      </Box>
      <Box component="form" onSubmit={submit} sx={{ borderTop: "1px solid", borderColor: "divider", pt: 2 }}>
        <Stack direction="row" gap={1} alignItems="flex-end">
          <TextField
            fullWidth
            multiline
            maxRows={5}
            label={incidentId ? "Message about this incident" : "Operational question"}
            value={message}
            onChange={(event) => setMessage(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                void send();
              }
            }}
          />
          <Tooltip title="Send message">
            <span>
              <IconButton type="submit" color="primary" disabled={!message.trim() || sending} aria-label="Send message">
                <SendIcon />
              </IconButton>
            </span>
          </Tooltip>
        </Stack>
      </Box>
    </Stack>
  );
}
