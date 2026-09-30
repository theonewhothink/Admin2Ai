/**
 * Design tokens (§30-33), identical to web/app/globals.css.
 *
 * Calm, not accounting software. Emerald means "all good / closed" and little
 * else; amber is attention; red is real risk only. Actions are ink, not colour.
 * 8px spacing, 12-16px radius, body 15-16px, tabular numerals, 150-220ms
 * animations that communicate state only.
 */
import type { TextStyle, ViewStyle } from "react-native";
import type { Tone } from "../api/types";

export const colors = {
  bg: "#F7F8F6",
  surface: "#FFFFFF",
  surfaceMuted: "#F2F4F1",
  surfaceHover: "#EEF0EC",

  text: "#111318",
  text2: "#667085",
  text3: "#8A93A3",

  line: "rgba(17, 19, 24, 0.07)",
  line2: "rgba(17, 19, 24, 0.12)",

  good: "#0F6B4F",
  goodDot: "#15835F",
  goodSoft: "#E8F3EE",
  attention: "#92570A",
  attentionDot: "#E0951F",
  attentionSoft: "#FCF3E3",
  risk: "#B42318",
  riskDot: "#D0382A",
  riskSoft: "#FCEDEB",

  ink: "#111318",
  inkPressed: "#2A2D35",
  onInk: "#FFFFFF",
} as const;

/** 8px system (web --s-0 … --s-12). */
export const space = {
  s0: 4,
  s1: 8,
  s2: 16,
  s3: 24,
  s4: 32,
  s5: 40,
  s6: 48,
  s8: 64,
} as const;

export const radius = {
  card: 14,
  control: 10,
  pill: 999,
} as const;

/** Motion communicates state only. */
export const motion = {
  fast: 150,
  base: 180,
  slow: 220,
  /** cubic-bezier(0.2, 0, 0, 1), as on the web. */
  easing: [0.2, 0, 0, 1] as const,
} as const;

export const fonts = {
  regular: "Geist_400Regular",
  medium: "Geist_500Medium",
  semibold: "Geist_600SemiBold",
} as const;

const tabular: TextStyle = { fontVariant: ["tabular-nums"] };

export const type = {
  display: { fontFamily: fonts.semibold, fontSize: 28, lineHeight: 34, letterSpacing: -0.4, color: colors.text, ...tabular },
  title: { fontFamily: fonts.semibold, fontSize: 22, lineHeight: 28, letterSpacing: -0.2, color: colors.text, ...tabular },
  heading: { fontFamily: fonts.semibold, fontSize: 17, lineHeight: 24, color: colors.text, ...tabular },
  body: { fontFamily: fonts.regular, fontSize: 16, lineHeight: 24, color: colors.text, ...tabular },
  bodyStrong: { fontFamily: fonts.medium, fontSize: 16, lineHeight: 24, color: colors.text, ...tabular },
  small: { fontFamily: fonts.regular, fontSize: 15, lineHeight: 22, color: colors.text2, ...tabular },
  meta: { fontFamily: fonts.medium, fontSize: 13, lineHeight: 18, color: colors.text2, ...tabular },
  number: { fontFamily: fonts.semibold, fontSize: 26, lineHeight: 32, color: colors.text, ...tabular },
} satisfies Record<string, TextStyle>;

/** Barely-there elevation, like the web's --shadow. */
export const shadow: ViewStyle = {
  shadowColor: "#101828",
  shadowOpacity: 0.05,
  shadowRadius: 3,
  shadowOffset: { width: 0, height: 1 },
  elevation: 1,
};

export interface ToneColors {
  fg: string;
  dot: string;
  soft: string;
}

export function toneColors(tone: Tone): ToneColors {
  switch (tone) {
    case "good":
      return { fg: colors.good, dot: colors.goodDot, soft: colors.goodSoft };
    case "attention":
      return { fg: colors.attention, dot: colors.attentionDot, soft: colors.attentionSoft };
    case "risk":
      return { fg: colors.risk, dot: colors.riskDot, soft: colors.riskSoft };
    default:
      return { fg: colors.text2, dot: colors.text3, soft: colors.surfaceMuted };
  }
}
