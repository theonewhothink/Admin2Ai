"use client";

import styles from "@/components/accountant/accountant.module.css";
import { ownerFrom } from "@/lib/owner";
import { useSession } from "./context";

/** The signed-in accountant's practice in the workspace header (production). */
export function SessionBadge() {
  const session = useSession();
  if (!session) return null;
  const who = ownerFrom(session);
  return (
    <span className={styles.firm}>
      <span className={styles.firmAvatar} aria-hidden="true">
        {who.initials}
      </span>
      <span className={styles.firmName}>{session.tenant.name || who.fullName}</span>
    </span>
  );
}
