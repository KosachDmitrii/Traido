import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { DeskProvider } from "@/context/DeskContext";
import { AppShell } from "@/layout/AppShell";
import { AgentsPage } from "@/pages/AgentsPage";
import { DeskPage } from "@/pages/DeskPage";
import { EvaluationPage } from "@/pages/EvaluationPage";
import { JournalPage } from "@/pages/JournalPage";
import { LogsPage } from "@/pages/LogsPage";
import { OpportunitiesPage } from "@/pages/OpportunitiesPage";
import { PositionsPage } from "@/pages/PositionsPage";
import { SettingsPage } from "@/pages/SettingsPage";

export function App() {
  return (
    <BrowserRouter>
      <DeskProvider>
        <Routes>
          <Route element={<AppShell />}>
            <Route index element={<DeskPage />} />
            <Route path="opportunities" element={<OpportunitiesPage />} />
            <Route path="positions" element={<PositionsPage />} />
            <Route path="agents" element={<AgentsPage />} />
            <Route path="journal" element={<JournalPage />} />
            <Route path="evaluation" element={<EvaluationPage />} />
            <Route path="logs" element={<LogsPage />} />
            <Route path="settings" element={<SettingsPage />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Route>
        </Routes>
      </DeskProvider>
    </BrowserRouter>
  );
}
