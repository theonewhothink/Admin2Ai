import type { IconName } from "@/components/Icon";

export interface NavItem {
  href: string;
  label: string;
  icon: IconName;
}

/**
 * The top bar: at most five places (spec §34). Sources is one of them at the owner's request ("a tab that shows
 * all the sources the system is feeding from"); Ask is the Chat button on every page (and the profile menu).
 * Everything else (documents, deadlines, people, the diagram, the plan, settings) is in the profile menu, two
 * taps away.
 */
export const desktopNav: NavItem[] = [
  { href: "/", label: "Home", icon: "home" },
  { href: "/needs-you", label: "Needs You", icon: "needs" },
  { href: "/companies", label: "Companies", icon: "building" },
  { href: "/sources", label: "Sources", icon: "link" },
  { href: "/activity", label: "Activity", icon: "activity" },
];

export function isActive(pathname: string, href: string): boolean {
  if (href === "/") return pathname === "/";
  return pathname === href || pathname.startsWith(`${href}/`);
}
