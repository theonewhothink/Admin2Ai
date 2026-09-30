import type { Metadata } from "next";
import { Icon } from "@/components/Icon";
import { ScanDropzone } from "@/components/ScanDropzone";
import { owner } from "@/lib/data";
import { production } from "@/lib/mode";

export const metadata: Metadata = { title: "Add a receipt" };

export default function ScanPage() {
  const inbox = `receipts+${owner.email.split("@")[0]}@in.admin2ai.app`;
  return (
    <div className="container-narrow page">
      <header className="page-head">
        <h1 className="h1">Add a receipt</h1>
        <p className="lead">
          Scanning works best on your phone. Tap Scan, point the camera at the receipt, and I will match it to the right
          payment.
        </p>
      </header>

      <ScanDropzone />

      {/* The forwarding address below is the sample owner's; production has no per-owner inbox address yet. */}
      {production ? null : (
        <div className="notice" style={{ marginTop: "var(--s-3)" }}>
          <Icon name="mail" size={20} style={{ color: "var(--text-2)", marginTop: 2 }} />
          <p>
            You can also forward receipts by email to <span className="mono">{inbox}</span>. Most of the time you won’t
            need to: I already collect them from your inbox.
          </p>
        </div>
      )}
    </div>
  );
}
