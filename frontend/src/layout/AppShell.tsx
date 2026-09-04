import { Outlet, useLocation } from "react-router-dom";
import { PageStrip } from "@/components/desk/AgentsStrip";
import { OpportunityRail } from "@/components/desk/OpportunityRail";
import { ReconciliationBanner } from "@/components/desk/ReconciliationBanner";
import { ToastStack } from "@/components/desk/ToastStack";
import { Sidebar, useNavCollapsed } from "@/layout/Sidebar";
import { Topbar } from "@/layout/Topbar";
import { useDesk } from "@/context/DeskContext";
import { TooltipProvider } from "@/ui";

export function AppShell() {
  const { desk, toasts, showFlash, dismissFlash, holdFlash, refreshAll } = useDesk();
  const isDesk = useLocation().pathname === "/";
  const { collapsed, onCollapsedChange } = useNavCollapsed();

  return (
    <TooltipProvider>
      <div className={`page root${collapsed ? " page--nav-collapsed" : ""}`}>
        <Topbar />
        <div className={`shell${isDesk ? "" : " shell--wide"}`}>
          <Sidebar collapsed={collapsed} onCollapsedChange={onCollapsedChange} />
          <main className="main">
            {!isDesk ? <PageStrip /> : null}
            <ReconciliationBanner desk={desk} />
            <Outlet />
          </main>
          {isDesk ? (
            <OpportunityRail desk={desk} onFlash={showFlash} onRefresh={refreshAll} />
          ) : null}
        </div>
        <ToastStack toasts={toasts} onDismiss={dismissFlash} onHold={holdFlash} />
      </div>
    </TooltipProvider>
  );
}
