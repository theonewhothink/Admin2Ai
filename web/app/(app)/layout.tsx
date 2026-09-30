import { ChatDrawer } from "@/components/ask/ChatDrawer";
import { OnboardingReturn } from "@/components/flow/OnboardingReturn";
import { LiveBottomNav, LiveHeader } from "@/components/live/LiveShell";
import { SessionProvider } from "@/components/session/SessionProvider";
import { AppHeader } from "@/components/shell/AppHeader";
import { BottomNav } from "@/components/shell/BottomNav";
import styles from "@/components/shell/shell.module.css";
import { browserEngine, getNeedsYou, liveData, production } from "@/lib/api";
import { askExamples, owner } from "@/lib/data";

export default async function AppLayout({ children }: { children: React.ReactNode }) {
  if (browserEngine) {
    return (
      <>
        <LiveHeader owner={owner} />
        <main id="main" className={styles.main}>
          {children}
        </main>
        <LiveBottomNav />
        <ChatDrawer examples={askExamples} />
      </>
    );
  }
  if (production) {
    // Everything loads in the browser with the owner's session (see lib/mode.ts).
    return (
      <SessionProvider>
        <OnboardingReturn />
        <LiveHeader />
        <main id="main" className={styles.main}>
          {children}
        </main>
        <LiveBottomNav />
        <ChatDrawer examples={askExamples} />
      </SessionProvider>
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
      <ChatDrawer examples={askExamples} />
    </>
  );
}
