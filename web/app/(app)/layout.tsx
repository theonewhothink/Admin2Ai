import { LiveBottomNav, LiveHeader } from "@/components/live/LiveShell";
import { AppHeader } from "@/components/shell/AppHeader";
import { BottomNav } from "@/components/shell/BottomNav";
import styles from "@/components/shell/shell.module.css";
import { browserEngine, getNeedsYou, liveData } from "@/lib/api";
import { owner } from "@/lib/data";

export default async function AppLayout({ children }: { children: React.ReactNode }) {
  if (browserEngine) {
    return (
      <>
        <LiveHeader owner={owner} />
        <main id="main" className={styles.main}>
          {children}
        </main>
        <LiveBottomNav />
      </>
    );
  }
  const items = await getNeedsYou();
  const needsIds = items.map((i) => i.id);
  return (
    <>
      <AppHeader needsIds={needsIds} owner={owner} sampleData={!liveData} />
      <main id="main" className={styles.main}>
        {children}
      </main>
      <BottomNav needsIds={needsIds} />
    </>
  );
}
