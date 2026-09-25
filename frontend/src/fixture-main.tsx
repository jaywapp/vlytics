import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { MatchBriefingPage } from "./features/matches/MatchBriefingPage";
import { createSyntheticFixtureClient } from "./features/matches/fixtures";
import "./styles.css";

const root = document.getElementById("root");

if (!root) {
  throw new Error("Root element is missing");
}

createRoot(root).render(
  <StrictMode>
    <MatchBriefingPage
      client={createSyntheticFixtureClient()}
      initialDate="2026-09-20"
    />
  </StrictMode>,
);
