"use client";

/**
 * The internal dashboard's frame ("Admin OS"): a dark, full-height app with a
 * collapsible left sidebar (240px, or 64px of icons) of grouped sections, and
 * on phones a top bar whose menu button slides the sidebar in over the page.
 */
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useId, useRef, useState } from "react";
import { useSession } from "@/components/session/context";
import { canOpenInternal, viewerRole } from "@/lib/internal-access";
import { AdminIcon, type AdminIconName } from "./icons";
import { useOverview } from "./store";
import styles from "./admin.module.css";

interface NavLink {
  href: string;
  label: string;
  icon: AdminIconName;
  /** Leaves the dashboard for another part of the app. */
  away?: boolean;
  badge?: number;
}

interface NavGroup {
  id: string;
  label: string;
  icon: AdminIconName;
  color: string;
  items: NavLink[];
}

function normalise(path: string): string {
  return path.replace(/\/+$/, "") || "/";
}

function isCurrent(pathname: string, href: string): boolean {
  return normalise(pathname) === normalise(href);
}

function Badge({ count, label }: { count: number | undefined; label: string }) {
  if (!count) return null;
  return (
    <span className={styles.badge} aria-label={`${count} ${label}`}>
      {count}
    </span>
  );
}

function Brand() {
  return (
    <span className={styles.brandMark} aria-hidden="true">
      <AdminIcon name="command" size={16} strokeWidth={2} />
    </span>
  );
}

function Sidebar({
  collapsed,
  onToggle,
  onNavigate,
  mobile,
}: {
  collapsed: boolean;
  onToggle?: () => void;
  onNavigate?: () => void;
  mobile?: boolean;
}) {
  const pathname = usePathname();
  const { data } = useOverview();
  const [closedGroups, setClosedGroups] = useState<Record<string, boolean>>({});
  const baseId = useId();

  const attention = data?.fixes.filter((f) => f.severity !== "blue").length;
  const stale = data?.connections.filter((c) => c.status !== "healthy").length;

  const groups: NavGroup[] = [
    {
      id: "production",
      label: "Production",
      icon: "rocket",
      color: "var(--a-amber-400)",
      items: [{ href: "/internal/readiness", label: "Readiness", icon: "listChecks" }],
    },
    {
      id: "maintenance",
      label: "Maintenance & QA",
      icon: "wrench",
      color: "var(--a-blue-400)",
      items: [
        { href: "/internal/operations", label: "Operations", icon: "activity" },
        { href: "/diagram", label: "Pipeline", icon: "workflow", away: true },
        { href: "/internal/connections", label: "Connections", icon: "plug", badge: stale },
      ],
    },
    {
      id: "strategy",
      label: "Strategy",
      icon: "compass",
      color: "var(--a-purple-400)",
      items: [{ href: "/internal/targets", label: "Targets", icon: "target" }],
    },
    {
      id: "system",
      label: "System",
      icon: "cog",
      color: "var(--a-400)",
      items: [
        { href: "/", label: "Owner app", icon: "building", away: true },
        { href: "/accountant", label: "Accountant view", icon: "users", away: true },
      ],
    },
  ];

  const commandActive = isCurrent(pathname, "/internal");

  return (
    <div className={styles.sidebarInner}>
      <div className={styles.brand}>
        <Link href="/internal" className={styles.brandLink} onClick={onNavigate}>
          <Brand />
          <span className={styles.brandName}>Admin OS</span>
        </Link>
        {onToggle ? (
          <button
            type="button"
            className={styles.iconButton}
            onClick={onToggle}
            aria-label={mobile ? "Close menu" : collapsed ? "Expand sidebar" : "Collapse sidebar"}
            aria-expanded={mobile ? undefined : !collapsed}
          >
            <AdminIcon name={mobile ? "x" : "menu"} size={18} />
          </button>
        ) : null}
      </div>

      <nav className={styles.nav} aria-label="Admin">
        <Link
          href="/internal"
          className={styles.primary}
          aria-current={commandActive ? "page" : undefined}
          title={collapsed ? "Command Center" : undefined}
          onClick={onNavigate}
        >
          <AdminIcon name="command" size={18} />
          <span className={styles.label}>Command Center</span>
          <Badge count={attention} label="critical fixes" />
        </Link>

        {groups.map((g) => {
          const open = !closedGroups[g.id];
          const listId = `${baseId}-${g.id}`;
          return (
            <div key={g.id} className={styles.group} data-open={open}>
              <button
                type="button"
                className={styles.groupHead}
                aria-expanded={open}
                aria-controls={listId}
                title={collapsed ? g.label : undefined}
                onClick={() => setClosedGroups((s) => ({ ...s, [g.id]: open }))}
              >
                <AdminIcon name={g.icon} size={16} style={{ color: g.color }} />
                <span className={styles.label}>{g.label}</span>
                <AdminIcon name="chevronDown" size={14} className={styles.chevron} />
              </button>
              <ul id={listId} className={styles.items} hidden={!open}>
                {g.items.map((item) => {
                  const current = !item.away && isCurrent(pathname, item.href);
                  return (
                    <li key={item.href}>
                      <Link
                        href={item.href}
                        prefetch={item.away ? false : undefined}
                        className={styles.item}
                        aria-current={current ? "page" : undefined}
                        title={collapsed ? item.label : undefined}
                        onClick={onNavigate}
                      >
                        <AdminIcon name={item.icon} size={16} />
                        <span className={styles.label}>{item.label}</span>
                        {item.away ? <AdminIcon name="externalLink" size={12} className={styles.away} /> : null}
                        <Badge count={item.badge} label="need attention" />
                      </Link>
                    </li>
                  );
                })}
              </ul>
            </div>
          );
        })}
      </nav>

      <div className={styles.sideFoot}>
        <Link href="/" prefetch={false} className={styles.back} title={collapsed ? "Back to app" : undefined} onClick={onNavigate}>
          <AdminIcon name="arrowLeft" size={16} />
          <span className={styles.label}>Back to app</span>
        </Link>
      </div>
    </div>
  );
}

export function AdminShell({ children }: { children: React.ReactNode }) {
  const [collapsed, setCollapsed] = useState(false);
  const [drawer, setDrawer] = useState(false);
  const menuButton = useRef<HTMLButtonElement>(null);
  const drawerRef = useRef<HTMLDivElement>(null);
  const pathname = usePathname();
  const session = useSession();
  const allowed = canOpenInternal(viewerRole(session));

  // Close the phone menu when the page changes.
  const [lastPath, setLastPath] = useState(pathname);
  if (lastPath !== pathname) {
    setLastPath(pathname);
    setDrawer(false);
  }

  useEffect(() => {
    if (!drawer) return;
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    drawerRef.current?.querySelector<HTMLElement>("button, a")?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setDrawer(false);
        menuButton.current?.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.body.style.overflow = previous;
      document.removeEventListener("keydown", onKey);
    };
  }, [drawer]);

  return (
    <div className={styles.shell} data-admin-os="">
      {allowed ? (
        <aside className={styles.sidebar} data-collapsed={collapsed} aria-label="Admin navigation">
          <Sidebar collapsed={collapsed} onToggle={() => setCollapsed((c) => !c)} />
        </aside>
      ) : null}

      <div className={styles.column}>
        <header className={styles.topBar}>
          {allowed ? (
            <button
              ref={menuButton}
              type="button"
              className={styles.iconButton}
              aria-label="Open menu"
              aria-expanded={drawer}
              onClick={() => setDrawer(true)}
            >
              <AdminIcon name="menu" size={20} />
            </button>
          ) : null}
          <Link href="/internal" className={styles.brandLink}>
            <Brand />
            <span className={styles.brandName}>Admin OS</span>
          </Link>
        </header>

        <main id="main" className={styles.main}>
          {allowed ? children : <Locked />}
        </main>
      </div>

      {allowed ? (
        <div className={styles.drawerRoot} data-open={drawer} aria-hidden={!drawer}>
          <div className={styles.backdrop} onClick={() => setDrawer(false)} />
          <div
            ref={drawerRef}
            className={styles.drawer}
            role="dialog"
            aria-modal="true"
            aria-label="Admin navigation"
            inert={!drawer}
          >
            <Sidebar collapsed={false} mobile onToggle={() => setDrawer(false)} onNavigate={() => setDrawer(false)} />
          </div>
        </div>
      ) : null}
    </div>
  );
}

function Locked() {
  return (
    <div className={styles.page}>
      <div className={`${styles.card} ${styles.locked}`}>
        <span className={styles.chip} style={{ "--chip": "var(--a-amber)" } as React.CSSProperties}>
          <AdminIcon name="lock" size={18} />
        </span>
        <h1 className={styles.title}>For the Admin2Ai team</h1>
        <p className={styles.subtitle}>
          This dashboard is for admins. Sign in with an admin account to open it.
        </p>
        <Link href="/" prefetch={false} className={styles.ghostButton}>
          <AdminIcon name="arrowLeft" size={16} />
          Back to app
        </Link>
      </div>
    </div>
  );
}
