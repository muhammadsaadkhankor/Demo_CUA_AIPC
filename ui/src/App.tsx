import React, { useState, useEffect, useRef } from "react";
import "./App.css";

const API = "http://localhost:5000";

type Status = "idle" | "running" | "success" | "done" | "error";

interface StatusResponse {
  running: boolean;
  status: Status;
  log: string[];
}

export default function App() {
  const [appName, setAppName] = useState("");
  const [status, setStatus] = useState<Status>("idle");
  const [log, setLog] = useState<string[]>([]);
  const [error, setError] = useState("");
  const logRef = useRef<HTMLDivElement>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Set window title so agent.py can find this browser window by title
  useEffect(() => {
    document.title = "CUA AIPC";
  }, []);

  // Auto-scroll log to bottom
  useEffect(() => {
    if (logRef.current) {
      logRef.current.scrollTop = logRef.current.scrollHeight;
    }
  }, [log]);

  // Poll /status while running
  useEffect(() => {
    if (status === "running") {
      pollRef.current = setInterval(async () => {
        try {
          const res = await fetch(`${API}/status`);
          const data: StatusResponse = await res.json();
          setLog(data.log);
          if (!data.running) {
            setStatus(data.status);
            clearInterval(pollRef.current!);
          }
        } catch {
          // backend not reachable yet, keep polling
        }
      }, 800);
    }
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [status]);

  const handleLaunch = async () => {
    if (!appName.trim() || status === "running") return;
    setError("");
    setLog([]);
    setStatus("running");
    try {
      const res = await fetch(`${API}/run`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ app: appName.trim() }),
      });
      if (!res.ok) {
        const data = await res.json();
        setError(data.error || "Failed to start agent.");
        setStatus("error");
      }
    } catch {
      setError("Cannot reach backend. Is agent.py running on port 5000?");
      setStatus("error");
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter") handleLaunch();
  };

  const isRunning = status === "running";

  const statusLabel: Record<Status, string> = {
    idle: "",
    running: "⏳ Agent running...",
    success: "✅ Successfully opened!",
    done: "🏁 Done",
    error: "❌ Error",
  };

  return (
    <div className="container">
      <div className="card">
        <h1 className="title">CUA AIPC</h1>
        <p className="subtitle">AI-powered Computer Use Agent</p>

        <div className="input-row">
          <input
            className="app-input"
            type="text"
            placeholder="App to open (e.g. Calculator, Notepad...)"
            value={appName}
            onChange={(e) => setAppName(e.target.value)}
            onKeyDown={handleKeyDown}
            disabled={isRunning}
            autoFocus
          />
          <button
            className={`launch-btn ${isRunning ? "disabled" : ""}`}
            onClick={handleLaunch}
            disabled={isRunning || !appName.trim()}
          >
            {isRunning ? "Running..." : "Launch"}
          </button>
        </div>

        {error && <p className="error-msg">{error}</p>}

        {status !== "idle" && !error && (
          <div className={`status-badge status-${status}`}>
            {statusLabel[status]}
          </div>
        )}

        {log.length > 0 && (
          <div className="log-box" ref={logRef}>
            {log.map((line, i) => (
              <div
                key={i}
                className={`log-line${line.startsWith("[error]") ? " log-error" : line.startsWith("Successfully") ? " log-success" : ""}`}
              >
                {line}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
