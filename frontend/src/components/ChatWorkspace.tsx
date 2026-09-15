import { useEffect, useLayoutEffect, useRef, useState } from "react";
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
  const { sessions, activeId, setActiveId, newSession, rename, remove } = useSessions();
  const { messages, setHistory, stream, send, resetStream } = useChatStream(activeId);
  const [artifactId, setArtifactId] = useState<string | null>(null);
  const { artifact, loading: artifactLoading } = useArtifact(artifactId);
  const focusedSessions = useRef<Set<string>>(new Set());
  const lastMessageRef = useRef<string>("");
  const scrollRef = useRef<HTMLDivElement>(null);
  // Tracks whether the viewport was already at the bottom before this render's
  // content changed, so a live answer streaming in keeps the view pinned to
  // it, but a user who has scrolled up to re-read an earlier source card
  // isn't yanked back down mid-read.
  const stickToBottomRef = useRef(true);

  const sendMessage = (content: string) => {
    lastMessageRef.current = content;
    stickToBottomRef.current = true;
    send(content);
  };

  const handleScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    stickToBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  };

  useEffect(() => {
    if (!activeId) return;
    let cancelled = false;
    resetStream();
    getSessionHistory(activeId).then((res) => {
      if (!cancelled) {
        setHistory(res.messages);
        // Opening a session should land on its most recent turn, not the top.
        stickToBottomRef.current = true;
      }
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

  // Runs after the DOM reflects new messages/streamed text, so the height
  // used to compute "already scrolled to bottom" is the post-update height.
  useLayoutEffect(() => {
    if (!stickToBottomRef.current) return;
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [messages, stream.text, stream.sources, stream.phase]);

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
          onRename={rename}
          onDelete={remove}
        />
        <main className="chat-panel">
          <div className="chat-panel__scroll" ref={scrollRef} onScroll={handleScroll}>
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
