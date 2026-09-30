import type { Metadata } from "next";
import Link from "next/link";
import styles from "@/components/accountant/accountant.module.css";
import { Icon } from "@/components/Icon";
import { SessionBadge } from "@/components/session/SessionBadge";
import { SessionProvider } from "@/components/session/SessionProvider";
import { Logo } from "@/components/shell/Logo";
import { accountantFirm } from "@/lib/data";
import { production } from "@/lib/mode";

export const metadata: Metadata = {
  title: { default: "Clients", template: "%s · Admin2Ai for accountants" },
};

export default function AccountantLayout({ children }: { children: React.ReactNode }) {
  const shell = (
    <div className={styles.shell}>
      <header className={styles.header}>
        <div className={`container ${styles.headerInner}`}>
          <div className={styles.brand}>
            <Logo href="/accountant" suffix="Accountants" />
          </div>
          <nav aria-label="Accountant" className={styles.nav}>
            <Link href="/accountant" className={styles.navLink}>
              Clients
            </Link>
          </nav>
          <div className={styles.right}>
            <Link href="/" className={`link-quiet ${styles.ownerLink}`}>
              <Icon name="swap" size={16} />
              Owner view
            </Link>
            {production ? (
              <SessionBadge />
            ) : (
              <span className={styles.firm}>
                <span className={styles.firmAvatar} aria-hidden="true">
                  {accountantFirm.initials}
                </span>
                <span className={styles.firmName}>{accountantFirm.name}</span>
              </span>
            )}
          </div>
        </div>
      </header>
      <main id="main">{children}</main>
    </div>
  );
  // Production: the signed-in session (or a trip to sign-in) for the whole workspace.
  return production ? <SessionProvider>{shell}</SessionProvider> : shell;
}
