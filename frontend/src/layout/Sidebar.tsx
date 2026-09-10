import { useCallback, useEffect, useRef, useState } from "react";
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
  Layers,
  Menu,
  X,
  Settings2,
  Workflow,
} from "lucide-react";
import { NavLink, useLocation } from "react-router-dom";
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
  { id: "strategies", to: "/strategies", labelKey: "nav.strategies", icon: Layers },
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


export function MobileNavigation() {
  const t = useT();
  const dialog = useRef<HTMLDialogElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);
  const location = useLocation();
  const close = useCallback(() => dialog.current?.close(), []);

  useEffect(close, [location.key, close]);
  useEffect(() => {
    const desktop = window.matchMedia("(min-width: 1101px)");
    const onResize = () => { if (desktop.matches) close(); };
    desktop.addEventListener("change", onResize);
    return () => desktop.removeEventListener("change", onResize);
  }, [close]);
  useEffect(() => {
    if (!open) return;
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => { document.body.style.overflow = previous; };
  }, [open]);

  return (
    <>
      <button
        ref={trigger}
        type="button"
        className="mobile-nav__trigger"
        aria-label={t("nav.openMenu")}
        aria-haspopup="dialog"
        aria-controls="mobile-navigation"
        aria-expanded={open}
        onClick={() => { dialog.current?.showModal(); setOpen(true); }}
      >
        <Menu size={22} strokeWidth={1.75} aria-hidden />
      </button>
      <dialog
        ref={dialog}
        id="mobile-navigation"
        className="mobile-nav"
        aria-labelledby="mobile-navigation-title"
        onClose={() => { setOpen(false); trigger.current?.focus(); }}
        onClick={(event) => {
          if (event.target !== event.currentTarget) return;
          const rect = event.currentTarget.getBoundingClientRect();
          if (event.clientX < rect.left || event.clientX > rect.right ||
              event.clientY < rect.top || event.clientY > rect.bottom) close();
        }}
      >
        <div className="mobile-nav__header">
          <strong id="mobile-navigation-title">{t("nav.menu")}</strong>
          <button type="button" className="mobile-nav__close" aria-label={t("nav.closeMenu")} onClick={close} autoFocus>
            <X size={22} strokeWidth={1.75} aria-hidden />
          </button>
        </div>
        <nav className="mobile-nav__links" aria-label={t("nav.aria")}
          onClick={(event) => { if ((event.target as Element).closest("a")) close(); }}>
          {PRIMARY.map((item) => <NavRow key={item.id} item={item} collapsed={false} />)}
          <div className="mobile-nav__settings"><NavRow item={SETTINGS} collapsed={false} /></div>
        </nav>
      </dialog>
    </>
  );
}
