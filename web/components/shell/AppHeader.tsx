"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { Owner } from "@/lib/types";
import { Logo } from "./Logo";
import { NeedsBadge } from "./NeedsBadge";
import { ProfileMenu } from "./ProfileMenu";
import { desktopNav, isActive } from "./nav-items";
import styles from "./shell.module.css";

export function AppHeader({ needsIds, owner, sampleData }: { needsIds: string[]; owner?: Owner; sampleData: boolean }) {
  const pathname = usePathname();
  return (
    <header className={styles.header}>
      <div className={`container ${styles.headerInner}`}>
        <Logo />
        <nav aria-label="Main" className={styles.topNav}>
          <ul>
            {desktopNav.map((item) => {
              const active = isActive(pathname, item.href);
              return (
                <li key={item.href}>
                  <Link href={item.href} className={styles.topLink} aria-current={active ? "page" : undefined}>
                    {item.label}
                    {item.href === "/needs-you" ? <NeedsBadge ids={needsIds} /> : null}
                  </Link>
                </li>
              );
            })}
          </ul>
        </nav>
        <ProfileMenu owner={owner} sampleData={sampleData} />
      </div>
    </header>
  );
}
