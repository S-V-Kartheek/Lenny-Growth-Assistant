import { useEffect, useRef, useState } from "react";
import { Header } from "./Header";
import { SessionSidebar } from "./SessionSidebar";
import { MessageList } from "./MessageList";
import { StreamingTurn } from "./StreamingTurn";
import { ChatInput } from "./ChatInput";
import { ArtifactPanel } from "./ArtifactPanel";
import { useProvider } from "../hooks/useProvider";
import { useSessions } from "../hooks/useSessions";
import { useChatStream } from "../hooks/useChatStream";
import { useArtifact } from "../hooks/useArtifact";
import { getSessionHistory, listSessionArtifacts } from "../api/client";

export function ChatWorkspace({ onBack }: { onBack?: () => void }) {
  const { status: providerStatus, switching: providerSwitching, switchProvider } = useProvider();
  const { sessions, activeId, setActiveId, newSession } = useSessions();
  const { messages, setHistory, stream, send, resetStream } = useChatStream(activeId);
  const [artifactId, setArtifactId] = useState<string | null>(null);
  const { artifact, loading: artifactLoading } = useArtifact(artifactId);
  const focusedSessions = useRef<Set<string>>(new Set());
  const lastMessageRef = useRef<string>("");

  const sendMessage = (content: string) => {
    lastMessageRef.current = content;
    send(content);
  };

  useEffect(() => {
    if (!activeId) return;
    let cancelled = false;
    resetStream();
    getSessionHistory(activeId).then((res) => {
      if (!cancelled) setHistory(res.messages);
    });
    listSessionArtifacts(activeId).then((res) => {
      if (cancelled) return;
      const latest = res.artifacts.at(-1) ?? null;
      setArtifactId(latest?.id ?? null);
    });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId]);

  useEffect(() => {
    if (stream.artifactSaved) {
      setArtifactId(stream.artifactSaved.id);
    }
  }, [stream.artifactSaved]);

  const isFirstArtifact = activeId != null && !focusedSessions.current.has(activeId);
  useEffect(() => {
    if (artifact && activeId) focusedSessions.current.add(activeId);
  }, [artifact, activeId]);

  return (
    <div className="app-shell">
      <Header
        provider={providerStatus}
        switching={providerSwitching}
        onSwitchProvider={switchProvider}
        onBack={onBack}
      />
      <div className="app-body">
        <SessionSidebar
          sessions={sessions}
          activeId={activeId}
          onSelect={setActiveId}
          onCreate={() => {
            newSession();
            setArtifactId(null);
          }}
        />
        <main className="chat-panel">
          <div className="chat-panel__scroll">
            <MessageList messages={messages} onExampleClick={sendMessage} />
            <StreamingTurn
              stream={stream}
              onRetry={() => lastMessageRef.current && sendMessage(lastMessageRef.current)}
            />
          </div>
          <ChatInput disabled={stream.active} onSend={sendMessage} />
        </main>
        <ArtifactPanel
          artifact={artifact}
          loading={artifactLoading}
          isFirstArtifact={isFirstArtifact}
        />
      </div>
    </div>
  );
}
