"use client";

import Link from "next/link";
import { useEffect, useId, useRef, useState } from "react";
import { Icon, type IconName } from "@/components/Icon";
import { browserEngine, resetEngine } from "@/lib/engine";
import { resetAnswered } from "@/lib/resolved-store";
import type { Owner } from "@/lib/types";
import styles from "./shell.module.css";

interface MenuLink {
  href: string;
  label: string;
  icon: IconName;
}

const main: MenuLink[] = [
  { href: "/settings", label: "Settings", icon: "settings" },
  { href: "/scan", label: "Add documents", icon: "upload" },
  { href: "/accountant", label: "Accountant workspace", icon: "users" },
];

const demo: MenuLink[] = [
  { href: "/audit", label: "Free business audit", icon: "search" },
  { href: "/onboarding", label: "Onboarding", icon: "arrowRight" },
  { href: "/?demo=stale", label: "Show a connection problem", icon: "refresh" },
];

export function ProfileMenu({ owner, sampleData }: { owner: Owner; sampleData: boolean }) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const menuId = useId();
  const close = () => setOpen(false);

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
        <span aria-hidden="true">{owner.initials}</span>
        <span className="visually-hidden">Account and settings</span>
      </button>
      <div id={menuId} className={styles.menu} data-open={open} hidden={!open}>
        <div className={styles.menuHead}>
          <div className={styles.menuName}>{owner.fullName}</div>
          <div className="meta">{owner.email}</div>
        </div>
        <ul className={styles.menuList}>
          {main.map((l) => (
            <li key={l.href}>
              <Link href={l.href} className={styles.menuItem} onClick={close}>
                <Icon name={l.icon} size={18} />
                {l.label}
              </Link>
            </li>
          ))}
        </ul>
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
        {browserEngine ? (
          <div className={styles.menuFoot}>Demo business. Everything is worked out here in your browser.</div>
        ) : sampleData ? (
          <div className={styles.menuFoot}>Showing sample data</div>
        ) : null}
      </div>
    </div>
  );
}
