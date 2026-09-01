import { useCallback, useEffect, useState } from "react";
import type { LucideIcon } from "lucide-react";
import {
  Activity,
  BarChart3,
  ChevronLeft,
  ChevronRight,
  ClipboardList,
  FileText,
  Home,
  Inbox,
  Settings2,
  Workflow,
} from "lucide-react";
import { NavLink } from "react-router-dom";
import { useT } from "@/i18n/I18nProvider";
import type { MessageKey } from "@/i18n";
import { Button, HintTooltip } from "@/ui";

type NavItem = {
  id: string;
  to: string;
  labelKey: MessageKey;
  end?: boolean;
  icon: LucideIcon;
};

const PRIMARY: NavItem[] = [
  { id: "desk", to: "/", labelKey: "nav.dashboard", end: true, icon: Home },
  { id: "opportunities", to: "/opportunities", labelKey: "nav.opportunities", icon: Inbox },
  { id: "positions", to: "/positions", labelKey: "nav.positions", icon: BarChart3 },
  { id: "agents", to: "/agents", labelKey: "nav.agents", icon: Workflow },
  { id: "journal", to: "/journal", labelKey: "nav.journal", icon: ClipboardList },
  { id: "evaluation", to: "/evaluation", labelKey: "nav.evaluation", icon: Activity },
  { id: "logs", to: "/logs", labelKey: "nav.logs", icon: FileText },
];

const SETTINGS: NavItem = {
  id: "settings",
  to: "/settings",
  labelKey: "nav.settings",
  icon: Settings2,
};

const STORAGE_KEY = "traido.nav.collapsed";

type Props = {
  collapsed: boolean;
  onCollapsedChange: (next: boolean) => void;
};

function NavRow({ item, collapsed }: { item: NavItem; collapsed: boolean }) {
  const t = useT();
  const Icon = item.icon;
  const label = t(item.labelKey);
  const link = (
    <NavLink
      to={item.to}
      end={item.end}
      className={({ isActive }) =>
        `nav-item${isActive ? " active" : ""}${collapsed ? " nav-item--icon" : ""}`
      }
      aria-label={collapsed ? label : undefined}
    >
      <Icon size={18} strokeWidth={1.5} absoluteStrokeWidth aria-hidden />
      {!collapsed ? <span className="nav-item__label">{label}</span> : null}
    </NavLink>
  );
  if (!collapsed) return link;
  return (
    <HintTooltip content={label} side="right">
      {link}
    </HintTooltip>
  );
}

export function Sidebar({ collapsed, onCollapsedChange }: Props) {
  const t = useT();
  return (
    <aside className={`sidebar${collapsed ? " sidebar--collapsed" : ""}`} aria-label={t("nav.aria")}>
      <nav className="sidebar__nav">
        {PRIMARY.map((item) => (
          <NavRow key={item.id} item={item} collapsed={collapsed} />
        ))}
      </nav>
      <div className="sidebar__footer">
        <NavRow item={SETTINGS} collapsed={collapsed} />
        <Button
          variant="ghost"
          className="sidebar__collapse"
          aria-label={collapsed ? t("nav.expand") : t("nav.collapse")}
          aria-expanded={!collapsed}
          onClick={() => onCollapsedChange(!collapsed)}
        >
          {collapsed ? (
            <ChevronRight size={18} strokeWidth={1.75} absoluteStrokeWidth aria-hidden />
          ) : (
            <ChevronLeft size={18} strokeWidth={1.75} absoluteStrokeWidth aria-hidden />
          )}
        </Button>
      </div>
    </aside>
  );
}

export function useNavCollapsed() {
  const [collapsed, setCollapsed] = useState(() => {
    if (typeof window === "undefined") return false;
    return window.localStorage.getItem(STORAGE_KEY) === "1";
  });

  useEffect(() => {
    window.localStorage.setItem(STORAGE_KEY, collapsed ? "1" : "0");
  }, [collapsed]);

  return {
    collapsed,
    onCollapsedChange: useCallback((next: boolean) => setCollapsed(next), []),
  };
}
