import Link from "next/link";
import styles from "./shell.module.css";

export function Logo({ href = "/", suffix }: { href?: string; suffix?: string }) {
  return (
    <Link href={href} className={styles.logo} aria-label="Admin2Ai home">
      <svg width="26" height="26" viewBox="0 0 26 26" aria-hidden="true">
        <rect width="26" height="26" rx="7.5" fill="#111318" />
        <path d="M7.5 9.5h11M7.5 13h7.5M7.5 16.5h4" stroke="#fff" strokeWidth="1.8" strokeLinecap="round" />
        <circle cx="18" cy="16.5" r="1.9" fill="#3fb68b" />
      </svg>
      <span className={styles.wordmark}>Admin2Ai</span>
      {suffix ? <span className={styles.logoSuffix}>{suffix}</span> : null}
    </Link>
  );
}
