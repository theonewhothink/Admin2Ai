import Link from "next/link";
import styles from "./flow.module.css";
import { Icon } from "@/components/Icon";
import { Bullets, Disclosure, Dot } from "@/components/ui";
import type { AuditResult } from "@/lib/types";

export function AuditView({ audit }: { audit: AuditResult }) {
  return (
    <div className="container">
      <div className={styles.audit}>
        <header className="stack-2">
          <p className="meta">Free business audit · {audit.companyName}</p>
          <h1 className="h1">Here is your business, in one page.</h1>
          <p className="lead">
            I read {audit.periodLabel} of email and bank activity. Nothing was changed. We found:
          </p>
        </header>

        <ul className={styles.findings}>
          {audit.findings.map((f) => (
            <li key={f.id} className={`card ${styles.finding}`}>
              <span className={`${styles.findingValue} num`}>
                {f.value}
                {f.tone === "attention" ? <Dot tone="attention" /> : null}
              </span>
              <span className={styles.findingLabel}>{f.label}</span>
              {f.examples.length > 0 ? (
                <Disclosure summary="Examples" className={styles.findingExamples}>
                  <Bullets items={f.examples} />
                </Disclosure>
              ) : null}
            </li>
          ))}
        </ul>

        <section className={`card ${styles.auditCta}`} aria-labelledby="cta-h">
          <h2 id="cta-h" className="h2">
            I can keep all of this in order for you, every month.
          </h2>
          <p className="muted">
            I collect the documents, chase what is missing, answer your accountant, and only ask you when I truly need to.
          </p>
          <Link href="/onboarding" className="btn btn-primary btn-lg">
            Let me manage this automatically.
            <Icon name="arrowRight" size={18} />
          </Link>
          <p className="meta">Nothing changes until you say so.</p>
        </section>
      </div>
    </div>
  );
}
