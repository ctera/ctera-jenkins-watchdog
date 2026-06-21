import { useRef, useState } from "react";
import {
  Box,
  Card,
  CardContent,
  Chip,
  CircularProgress,
  IconButton,
  Stack,
  TextField,
  Typography,
} from "@mui/material";
import SendIcon from "@mui/icons-material/Send";
import StopIcon from "@mui/icons-material/Stop";
import { streamChat, type ChatEvent } from "../services/api";

interface Message {
  role: "user" | "assistant";
  content: string;
  toolCalls?: { name: string; success?: boolean }[];
}

export default function Chat() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [currentTools, setCurrentTools] = useState<{ name: string; success?: boolean }[]>([]);
  const abortRef = useRef<AbortController | null>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const sessionRef = useRef<string | null>(null);

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  };

  const handleSend = async () => {
    const text = input.trim();
    if (!text || streaming) return;

    setInput("");
    setMessages((prev) => [...prev, { role: "user", content: text }]);
    setStreaming(true);
    setCurrentTools([]);

    const controller = new AbortController();
    abortRef.current = controller;

    let assistantContent = "";
    const tools: { name: string; success?: boolean }[] = [];

    setMessages((prev) => [...prev, { role: "assistant", content: "" }]);

    try {
      for await (const event of streamChat(text, sessionRef.current, controller.signal)) {
        switch (event.type) {
          case "token":
            assistantContent += event.content || "";
            setMessages((prev) => {
              const updated = [...prev];
              updated[updated.length - 1] = { role: "assistant", content: assistantContent, toolCalls: [...tools] };
              return updated;
            });
            scrollToBottom();
            break;
          case "tool_start":
            tools.push({ name: event.tool_name || "unknown" });
            setCurrentTools([...tools]);
            break;
          case "tool_result":
            if (tools.length > 0) {
              tools[tools.length - 1].success = event.success;
            }
            setCurrentTools([...tools]);
            break;
          case "done":
            if (event.session_id) {
              sessionRef.current = event.session_id;
            }
            break;
          case "error":
            assistantContent += `\n\nError: ${event.content}`;
            setMessages((prev) => {
              const updated = [...prev];
              updated[updated.length - 1] = { role: "assistant", content: assistantContent, toolCalls: [...tools] };
              return updated;
            });
            break;
        }
      }
    } catch (e) {
      if ((e as Error).name !== "AbortError") {
        assistantContent += `\n\nConnection error: ${(e as Error).message}`;
      }
    } finally {
      setMessages((prev) => {
        const updated = [...prev];
        updated[updated.length - 1] = { role: "assistant", content: assistantContent, toolCalls: [...tools] };
        return updated;
      });
      setStreaming(false);
      setCurrentTools([]);
      abortRef.current = null;
      scrollToBottom();
    }
  };

  const handleStop = () => {
    abortRef.current?.abort();
  };

  return (
    <Stack sx={{ height: "calc(100vh - 120px)", display: "flex", flexDirection: "column" }}>
      <Typography variant="h4" sx={{ mb: 2 }}>Jenkins Investigation</Typography>

      <Box sx={{ flex: 1, overflow: "auto", mb: 2 }}>
        {messages.length === 0 && (
          <Box sx={{ display: "flex", alignItems: "center", justifyContent: "center", height: "100%" }}>
            <Stack spacing={2} alignItems="center">
              <Typography variant="h6" color="text.secondary">Ask anything about Jenkins agents</Typography>
              <Stack spacing={1}>
                {[
                  "Why are Jenkins agents going offline?",
                  "What pods are crashing in the jenkins namespace?",
                  "Is the build queue backed up?",
                  "Check agent memory and CPU usage",
                ].map((q) => (
                  <Chip
                    key={q}
                    label={q}
                    onClick={() => setInput(q)}
                    variant="outlined"
                    sx={{ cursor: "pointer" }}
                  />
                ))}
              </Stack>
            </Stack>
          </Box>
        )}

        <Stack spacing={2} sx={{ pb: 2 }}>
          {messages.map((msg, i) => (
            <Box key={i} sx={{ display: "flex", justifyContent: msg.role === "user" ? "flex-end" : "flex-start" }}>
              <Card
                sx={{
                  maxWidth: "80%",
                  bgcolor: msg.role === "user" ? "primary.main" : "background.paper",
                }}
              >
                <CardContent sx={{ py: 1.5, "&:last-child": { pb: 1.5 } }}>
                  {msg.toolCalls && msg.toolCalls.length > 0 && (
                    <Box sx={{ display: "flex", gap: 0.5, flexWrap: "wrap", mb: 1 }}>
                      {msg.toolCalls.map((tc, j) => (
                        <Chip
                          key={j}
                          label={tc.name}
                          size="small"
                          color={tc.success === false ? "error" : tc.success ? "success" : "default"}
                          variant="outlined"
                          sx={{ fontSize: "0.65rem", height: 18 }}
                        />
                      ))}
                    </Box>
                  )}
                  <Typography
                    variant="body2"
                    sx={{ whiteSpace: "pre-wrap", color: msg.role === "user" ? "white" : "text.primary" }}
                  >
                    {msg.content || (streaming && i === messages.length - 1 ? "Investigating..." : "")}
                  </Typography>
                </CardContent>
              </Card>
            </Box>
          ))}
          <div ref={messagesEndRef} />
        </Stack>

        {streaming && currentTools.length > 0 && (
          <Box sx={{ display: "flex", gap: 0.5, flexWrap: "wrap", px: 2 }}>
            <CircularProgress size={14} sx={{ mr: 1 }} />
            {currentTools.map((tc, i) => (
              <Chip
                key={i}
                label={tc.name}
                size="small"
                color={tc.success === false ? "error" : tc.success ? "success" : "info"}
                variant="outlined"
                sx={{ fontSize: "0.65rem", height: 18 }}
              />
            ))}
          </Box>
        )}
      </Box>

      <Box sx={{ display: "flex", gap: 1 }}>
        <TextField
          fullWidth
          placeholder="Ask about Jenkins agents, builds, or cluster state..."
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && !e.shiftKey && handleSend()}
          size="small"
          disabled={streaming}
          multiline
          maxRows={3}
        />
        {streaming ? (
          <IconButton onClick={handleStop} color="error">
            <StopIcon />
          </IconButton>
        ) : (
          <IconButton onClick={handleSend} color="primary" disabled={!input.trim()}>
            <SendIcon />
          </IconButton>
        )}
      </Box>
    </Stack>
  );
}
