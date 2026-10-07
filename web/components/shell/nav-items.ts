import type { IconName } from "@/components/Icon";

export interface NavItem {
  href: string;
  label: string;
  icon: IconName;
}

/**
 * The top bar: at most five places (spec §34). Everything else (documents, deadlines, people, sources,
 * the diagram, the plan, settings) is in the profile menu, two taps away.
 */
export const desktopNav: NavItem[] = [
  { href: "/", label: "Home", icon: "home" },
  { href: "/needs-you", label: "Needs You", icon: "needs" },
  { href: "/companies", label: "Companies", icon: "building" },
  { href: "/activity", label: "Activity", icon: "activity" },
  { href: "/ask", label: "Ask", icon: "ask" },
];

export function isActive(pathname: string, href: string): boolean {
  if (href === "/") return pathname === "/";
  return pathname === href || pathname.startsWith(`${href}/`);
}
