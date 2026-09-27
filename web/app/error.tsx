"use client";

export default function ErrorPage({ reset }: { error: Error & { digest?: string }; reset: () => void }) {
  return (
    <main id="main" className="container-narrow page" style={{ paddingTop: "var(--s-12)" }}>
      <div className="stack-2">
        <h1 className="h1">This page didn’t load.</h1>
        <p className="lead">Nothing is lost. Try again in a moment.</p>
        <div>
          <button type="button" className="btn btn-primary" onClick={reset}>
            Try again
          </button>
        </div>
      </div>
    </main>
  );
}
