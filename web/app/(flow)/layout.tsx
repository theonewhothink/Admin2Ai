import styles from "@/components/flow/flow.module.css";
import { Logo } from "@/components/shell/Logo";

export default function FlowLayout({ children }: { children: React.ReactNode }) {
  return (
    <>
      <header className={styles.header}>
        <div className={`container ${styles.headerInner}`}>
          <Logo />
        </div>
      </header>
      <main id="main" className={styles.main}>
        {children}
      </main>
    </>
  );
}
