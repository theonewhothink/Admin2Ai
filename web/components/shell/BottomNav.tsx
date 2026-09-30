"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Icon, type IconName } from "@/components/Icon";
import { NeedsBadge } from "./NeedsBadge";
import { isActive } from "./nav-items";
import styles from "./shell.module.css";

const left: { href: string; label: string; icon: IconName }[] = [
  { href: "/", label: "Home", icon: "home" },
  { href: "/needs-you", label: "Needs You", icon: "needs" },
];
const right: { href: string; label: string; icon: IconName }[] = [
  { href: "/activity", label: "Activity", icon: "activity" },
  { href: "/ask", label: "Ask", icon: "ask" },
];

export function BottomNav({ needsIds }: { needsIds: string[] }) {
  const pathname = usePathname();
  const item = (i: (typeof left)[number]) => {
    const active = isActive(pathname, i.href);
    return (
      <li key={i.href}>
        <Link href={i.href} className={styles.bottomLink} aria-current={active ? "page" : undefined}>
          <span className={styles.bottomIcon}>
            <Icon name={i.icon} size={24} strokeWidth={active ? 1.9 : 1.6} />
            {i.href === "/needs-you" ? <NeedsBadge ids={needsIds} variant="corner" /> : null}
          </span>
          <span>{i.label}</span>
        </Link>
      </li>
    );
  };
  const scanActive = isActive(pathname, "/scan");
  return (
    <nav aria-label="Main" className={styles.bottomNav}>
      <ul>
        {left.map(item)}
        <li>
          <Link href="/scan" className={styles.scanLink} aria-current={scanActive ? "page" : undefined}>
            <span className={styles.scanButton}>
              <Icon name="scan" size={26} strokeWidth={1.8} />
            </span>
            <span className="visually-hidden">Scan a receipt</span>
          </Link>
        </li>
        {right.map(item)}
      </ul>
    </nav>
  );
}
