"use client";

import Link from "next/link";
import { useEffect, useId, useRef, useState } from "react";
import { Icon, type IconName } from "@/components/Icon";
import { useSession } from "@/components/session/context";
import { resetEngine } from "@/lib/engine";
import { canOpenInternal, viewerRole } from "@/lib/internal-access";
import { BASE_PATH, browserEngine, production } from "@/lib/mode";
import { resetAnswered } from "@/lib/resolved-store";
import type { Owner } from "@/lib/types";
import styles from "./shell.module.css";

interface MenuLink {
  href: string;
  label: string;
  icon: IconName;
}

const main: MenuLink[] = [
  { href: "/documents", label: "Documents", icon: "document" },
  { href: "/sources", label: "Sources", icon: "link" },
  { href: "/diagram", label: "Diagram", icon: "flow" },
  { href: "/settings", label: "Settings", icon: "settings" },
  { href: "/scan", label: "Add documents", icon: "upload" },
  { href: "/accountant", label: "Accountant workspace", icon: "users" },
];

const demo: MenuLink[] = [
  { href: "/audit", label: "Free business audit", icon: "search" },
  { href: "/onboarding", label: "Onboarding", icon: "arrowRight" },
  { href: "/?demo=stale", label: "Show a connection problem", icon: "refresh" },
];

/** Production: end the session on the server, then sign-in. The cookie is gone either way. */
async function signOutAndLeave() {
  const { signOut } = await import("@/lib/account");
  await signOut();
  // A full page load on purpose: nothing of the signed-in session stays in memory.
  // eslint-disable-next-line @next/next/no-location-assign-relative-destination
  window.location.assign(`${BASE_PATH}/signin`);
}

export function ProfileMenu({ owner, sampleData }: { owner?: Owner; sampleData: boolean }) {
  const [open, setOpen] = useState(false);
  const [leaving, setLeaving] = useState(false);
  const session = useSession();
  const rootRef = useRef<HTMLDivElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const menuId = useId();
  const close = () => setOpen(false);
  // The accountant workspace is for accountants: owners in production don't see it.
  const links = production && session?.role === "owner" ? main.filter((l) => l.href !== "/accountant") : main;

  useEffect(() => {
    if (!open) return;
    const onPointer = (e: PointerEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setOpen(false);
        buttonRef.current?.focus();
      }
    };
    document.addEventListener("pointerdown", onPointer);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onPointer);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <div className={styles.profile} ref={rootRef}>
      <button
        ref={buttonRef}
        type="button"
        className={styles.avatar}
        aria-haspopup="true"
        aria-expanded={open}
        aria-controls={menuId}
        onClick={() => setOpen((v) => !v)}
      >
        {owner ? <span aria-hidden="true">{owner.initials}</span> : <Icon name="user" size={18} />}
        <span className="visually-hidden">Account and settings</span>
      </button>
      <div id={menuId} className={styles.menu} data-open={open} hidden={!open}>
        {owner ? (
          <div className={styles.menuHead}>
            <div className={styles.menuName}>{owner.fullName}</div>
            <div className="meta">{owner.email}</div>
          </div>
        ) : null}
        <ul className={styles.menuList}>
          {links.map((l) => (
            <li key={l.href}>
              <Link href={l.href} className={styles.menuItem} onClick={close}>
                <Icon name={l.icon} size={18} />
                {l.label}
              </Link>
            </li>
          ))}
          {canOpenInternal(viewerRole(session)) ? (
            <li>
              <Link href="/internal" className={styles.menuItem} onClick={close}>
                <Icon name="shield" size={18} />
                Internal dashboard
              </Link>
            </li>
          ) : null}
        </ul>
        {production ? (
          <ul className={styles.menuList}>
            <li>
              <button
                type="button"
                className={styles.menuItem}
                disabled={leaving}
                onClick={() => {
                  setLeaving(true);
                  void signOutAndLeave();
                }}
              >
                <Icon name="logout" size={18} />
                {leaving ? "Signing out…" : "Sign out"}
              </button>
            </li>
          </ul>
        ) : (
          <>
            <div className={styles.menuGroupLabel}>Preview</div>
            <ul className={styles.menuList}>
              {demo.map((l) => (
                <li key={l.href}>
                  <Link href={l.href} className={styles.menuItem} onClick={close}>
                    <Icon name={l.icon} size={18} />
                    {l.label}
                  </Link>
                </li>
              ))}
              <li>
                <button
                  type="button"
                  className={styles.menuItem}
                  onClick={() => {
                    resetAnswered();
                    close();
                    if (browserEngine) {
                      // The engine forgets this visit's changes and starts again from the demo.
                      resetEngine();
                      window.location.reload();
                    }
                  }}
                >
                  <Icon name="swap" size={18} />
                  Bring back answered items
                </button>
              </li>
            </ul>
            <ul className={styles.menuList}>
              <li>
                <Link href="/onboarding" className={styles.menuItem} onClick={close}>
                  <Icon name="logout" size={18} />
                  Sign out
                </Link>
              </li>
            </ul>
          </>
        )}
        {browserEngine ? (
          <div className={styles.menuFoot}>Demo business. Everything is worked out here in your browser.</div>
        ) : sampleData ? (
          <div className={styles.menuFoot}>Showing sample data</div>
        ) : null}
      </div>
    </div>
  );
}
