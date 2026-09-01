import type { LucideIcon } from "lucide-react";
import {
  Bot,
  BookOpen,
  CandlestickChart,
  Crosshair,
  FlaskConical,
  LayoutDashboard,
  ScrollText,
  Settings,
} from "lucide-react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import { PageStrip } from "@/components/desk/AgentsStrip";
import { OpportunityRail } from "@/components/desk/OpportunityRail";
import { ReconciliationBanner } from "@/components/desk/ReconciliationBanner";
import { ToastStack } from "@/components/desk/ToastStack";
import { Topbar } from "@/layout/Topbar";
import { useDesk } from "@/context/DeskContext";

type NavItem = {
  id: string;
  to: string;
  label: string;
  end?: boolean;
  icon: LucideIcon;
};

const items: NavItem[] = [
  { id: "desk", to: "/", label: "Dashboard", end: true, icon: LayoutDashboard },
  { id: "opportunities", to: "/opportunities", label: "Opportunities", icon: Crosshair },
  { id: "positions", to: "/positions", label: "Positions", icon: CandlestickChart },
  { id: "agents", to: "/agents", label: "Agents", icon: Bot },
  { id: "journal", to: "/journal", label: "Journal", icon: BookOpen },
  { id: "evaluation", to: "/evaluation", label: "Evaluation", icon: FlaskConical },
  { id: "logs", to: "/logs", label: "Logs", icon: ScrollText },
  { id: "settings", to: "/settings", label: "Settings", icon: Settings },
];

export function AppShell() {
  const { desk, scannerLine, toasts, showFlash, dismissFlash, holdFlash, refreshAll } = useDesk();
  const loc = useLocation();
  const isDesk = loc.pathname === "/";

  return (
    <div className="page">
      {/* Outside the grid, so it spans the sidebar and the rail rather than only
          the content column. */}
      <Topbar />

      <div className={`shell${isDesk ? "" : " shell--wide"}`}>
        <aside className="sidebar">
          {items.map((item) => {
            const Icon = item.icon;
            return (
              <NavLink
                key={item.id}
                to={item.to}
                end={item.end}
                className={({ isActive }) => `nav-item${isActive ? " active" : ""}`}
              >
                <Icon size={18} strokeWidth={1.75} absoluteStrokeWidth aria-hidden />
                {item.label}
              </NavLink>
            );
          })}
        </aside>

        <main className="main">
          {!isDesk ? <PageStrip /> : null}
          {/* Shown on every page, desk included, and deliberately not a flash: a
              book that cannot be checked against the broker is a standing
              condition, not an event, and it must not time out on its own. */}
          <ReconciliationBanner desk={desk} />
          <Outlet />
        </main>

        {isDesk ? (
          <OpportunityRail
            desk={desk}
            scannerLine={scannerLine}
            onFlash={showFlash}
            onRefresh={refreshAll}
          />
        ) : null}
      </div>

      {/* The one place the dashboard-only rule for messages is stated. */}
      {isDesk ? (
        <ToastStack toasts={toasts} onDismiss={dismissFlash} onHold={holdFlash} />
      ) : null}
    </div>
  );
}
