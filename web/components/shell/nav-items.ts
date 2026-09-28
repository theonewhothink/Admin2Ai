import type { IconName } from "@/components/Icon";

export interface NavItem {
  href: string;
  label: string;
  icon: IconName;
}

export const desktopNav: NavItem[] = [
  { href: "/", label: "Home", icon: "home" },
  { href: "/needs-you", label: "Needs You", icon: "needs" },
  { href: "/companies", label: "Companies", icon: "building" },
  { href: "/documents", label: "Documents", icon: "document" },
  { href: "/sources", label: "Sources", icon: "link" },
  { href: "/activity", label: "Activity", icon: "activity" },
  { href: "/ask", label: "Ask", icon: "ask" },
];

export function isActive(pathname: string, href: string): boolean {
  if (href === "/") return pathname === "/";
  return pathname === href || pathname.startsWith(`${href}/`);
}
