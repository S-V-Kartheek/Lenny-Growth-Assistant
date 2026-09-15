import { useCallback, useEffect, useState } from "react";
import "./App.css";
import { LandingPage } from "./components/LandingPage";
import { ChatWorkspace } from "./components/ChatWorkspace";

type View = "landing" | "app";

function readViewFromHash(): View {
  return window.location.hash === "#app" ? "app" : "landing";
}

function App() {
  const [view, setView] = useState<View>(() =>
    typeof window === "undefined" ? "landing" : readViewFromHash(),
  );

  useEffect(() => {
    const onHashChange = () => setView(readViewFromHash());
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  const enterApp = useCallback(() => {
    window.location.hash = "app";
    setView("app");
  }, []);

  const backToLanding = useCallback(() => {
    window.location.hash = "";
    setView("landing");
  }, []);

  if (view === "landing") {
    return <LandingPage onEnter={enterApp} />;
  }

  return <ChatWorkspace onBack={backToLanding} />;
}

export default App;
