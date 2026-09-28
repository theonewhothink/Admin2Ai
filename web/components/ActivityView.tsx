import styles from "@/components/activity.module.css";
import { Icon, type IconName } from "@/components/Icon";
import { formatMoney, formatTime, localDay, relativeDayLabel } from "@/lib/format";
import type { ActivityFeed, ActivityItem, ActivityKind } from "@/lib/types";

const kindIcon: Record<ActivityKind, IconName> = {
  collected: "inboxIn",
  recovered: "search",
  chased: "send",
  answered: "ask",
  checked: "check",
  closed: "checkCircle",
  protected: "shield",
  learned: "bookmark",
};

const kindLabel: Record<ActivityKind, string> = {
  collected: "Collected",
  recovered: "Recovered",
  chased: "Chased",
  answered: "Answered",
  checked: "Checked",
  closed: "Closed",
  protected: "Protected",
  learned: "Learned",
};

export function ActivityView({ feed }: { feed: ActivityFeed }) {
  const today = feed.today ?? localDay(new Date().toISOString());

  const groups = new Map<string, ActivityItem[]>();
  for (const item of [...feed.items].sort((a, b) => b.at.localeCompare(a.at))) {
    const day = localDay(item.at);
    const list = groups.get(day) ?? [];
    list.push(item);
    groups.set(day, list);
  }

  return (
    <div className="container-narrow page">
      <header className="page-head">
        <h1 className="h1">Activity</h1>
        <p className="lead">What I handled for you. Nothing here needs you.</p>
      </header>

      {groups.size === 0 ? (
        <div className="card card-pad">
          <p className="muted">Nothing yet. As soon as I start, everything I do shows up here.</p>
        </div>
      ) : (
        <div className="stack-4">
          {[...groups.entries()].map(([day, items]) => (
            <section key={day} aria-labelledby={`day-${day}`}>
              <h2 id={`day-${day}`} className={styles.day}>
                {relativeDayLabel(day, today)}
              </h2>
              <ol className={`card ${styles.timeline}`}>
                {items.map((item) => (
                  <li key={item.id} className={styles.item}>
                    <time className={`${styles.time} num`} dateTime={item.at}>
                      {formatTime(item.at)}
                    </time>
                    <span className={styles.icon} data-kind={item.kind} title={kindLabel[item.kind]}>
                      <Icon name={kindIcon[item.kind]} size={16} strokeWidth={1.8} />
                    </span>
                    <div className={styles.content}>
                      <p className={styles.text}>{item.text}</p>
                      {item.companyName || item.amount !== undefined ? (
                        <p className="meta">
                          {[item.companyName, item.amount !== undefined ? formatMoney(item.amount, item.currency) : null]
                            .filter(Boolean)
                            .join(" · ")}
                        </p>
                      ) : null}
                    </div>
                  </li>
                ))}
              </ol>
            </section>
          ))}
        </div>
      )}
    </div>
  );
}
