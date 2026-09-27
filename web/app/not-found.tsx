import Link from "next/link";

export default function NotFound() {
  return (
    <main id="main" className="container-narrow page" style={{ paddingTop: "var(--s-12)" }}>
      <div className="stack-2">
        <h1 className="h1">I couldn’t find that page.</h1>
        <p className="lead">It may have moved. Everything important is on Home.</p>
        <div>
          <Link href="/" className="btn btn-primary">
            Go to Home
          </Link>
        </div>
      </div>
    </main>
  );
}
