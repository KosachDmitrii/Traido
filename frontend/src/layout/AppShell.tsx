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
import { useT } from "@/i18n/I18nProvider";
import type { MessageKey } from "@/i18n";

type NavItem = {
  id: string;
  to: string;
  labelKey: MessageKey;
  end?: boolean;
  icon: LucideIcon;
};

const items: NavItem[] = [
  { id: "desk", to: "/", labelKey: "nav.dashboard", end: true, icon: LayoutDashboard },
  { id: "opportunities", to: "/opportunities", labelKey: "nav.opportunities", icon: Crosshair },
  { id: "positions", to: "/positions", labelKey: "nav.positions", icon: CandlestickChart },
  { id: "agents", to: "/agents", labelKey: "nav.agents", icon: Bot },
  { id: "journal", to: "/journal", labelKey: "nav.journal", icon: BookOpen },
  { id: "evaluation", to: "/evaluation", labelKey: "nav.evaluation", icon: FlaskConical },
  { id: "logs", to: "/logs", labelKey: "nav.logs", icon: ScrollText },
  { id: "settings", to: "/settings", labelKey: "nav.settings", icon: Settings },
];

export function AppShell() {
  const { desk, scannerLine, toasts, showFlash, dismissFlash, holdFlash, refreshAll } = useDesk();
  const loc = useLocation();
  const isDesk = loc.pathname === "/";
  const t = useT();

  return (
    <div className="page">
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
                {t(item.labelKey)}
              </NavLink>
            );
          })}
        </aside>

        <main className="main">
          {!isDesk ? <PageStrip /> : null}
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

      {isDesk ? (
        <ToastStack toasts={toasts} onDismiss={dismissFlash} onHold={holdFlash} />
      ) : null}
    </div>
  );
}
