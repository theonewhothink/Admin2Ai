/** Line icons, same 24px paths as web/components/Icon.tsx so both apps look alike. */
import Svg, { Path } from "react-native-svg";
import { colors } from "../theme/tokens";

const paths = {
  home: "M4 10.5 12 4l8 6.5V19a1 1 0 0 1-1 1h-4.5v-5.5h-5V20H5a1 1 0 0 1-1-1v-8.5Z",
  needs: "M4 13.5 6.2 6.3A1.5 1.5 0 0 1 7.6 5.3h8.8a1.5 1.5 0 0 1 1.4 1l2.2 7.2V18a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1v-4.5Zm0 0h4.5l1 2h5l1-2H20",
  activity: "M4 12h3.5l2.5-6 4 12 2.5-6H20",
  ask: "M5 18.5V7a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2H9l-4 2.5Z",
  scan: "M4 8.5V6a2 2 0 0 1 2-2h2.5M15.5 4H18a2 2 0 0 1 2 2v2.5M20 15.5V18a2 2 0 0 1-2 2h-2.5M8.5 20H6a2 2 0 0 1-2-2v-2.5M4 12h16",
  check: "m5 12.5 4.5 4.5L19 7.5",
  chevronDown: "m6.5 9.5 5.5 5.5 5.5-5.5",
  chevronRight: "m9.5 6.5 5.5 5.5-5.5 5.5",
  arrowUp: "M12 19V6m-5.5 5.5L12 6l5.5 5.5",
  document: "M7 3.5h6.5L18 8v11.5a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1v-15a1 1 0 0 1 1-1Zm6 0V8.5h5M9 12.5h6M9 16h6",
  phone: "M8.4 4H6a1.5 1.5 0 0 0-1.5 1.6A15 15 0 0 0 18.4 19.5 1.5 1.5 0 0 0 20 18v-2.4a1 1 0 0 0-.8-1l-3-.7a1 1 0 0 0-1 .3l-1.4 1.5a11 11 0 0 1-5.5-5.5l1.5-1.4a1 1 0 0 0 .3-1l-.7-3a1 1 0 0 0-1-.8Z",
  shield: "M12 3.5 5 6.5v5c0 4.2 2.9 7.6 7 9 4.1-1.4 7-4.8 7-9v-5l-7-3Z",
  lock: "M6.5 10.5h11a1 1 0 0 1 1 1v7a1 1 0 0 1-1 1h-11a1 1 0 0 1-1-1v-7a1 1 0 0 1 1-1Zm2-.5V8a3.5 3.5 0 0 1 7 0v2",
  upload: "M12 15.5V4.5m-4.5 4.5L12 4.5 16.5 9M4.5 15v3a1.5 1.5 0 0 0 1.5 1.5h12a1.5 1.5 0 0 0 1.5-1.5v-3",
  refresh: "M19.5 12a7.5 7.5 0 1 1-2.2-5.3M19.5 4.5v3.5H16",
  clock: "M12 20.5a8.5 8.5 0 1 0 0-17 8.5 8.5 0 0 0 0 17ZM12 7.5V12l3 2",
  link: "M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1",
  inboxIn: "M12 4v8.5m-3.5-3.5 3.5 3.5 3.5-3.5M4 13.5h4.5l1 2h5l1-2H20V18a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1v-4.5Z",
  mail: "M4 6.5h16v11a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1v-11Zm0 0 8 6.5 8-6.5",
  search: "M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14Zm9 2-4-4",
  bookmark: "M7 4.5h10a.5.5 0 0 1 .5.5v15l-5.5-4-5.5 4V5a.5.5 0 0 1 .5-.5Z",
  x: "m6.5 6.5 11 11m0-11-11 11",
  chevronLeft: "m14.5 6.5-5.5 5.5 5.5 5.5",
  user: "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8Zm-7 8.5a7 7 0 0 1 14 0",
  bell: "M6.5 16.5V11a5.5 5.5 0 0 1 11 0v5.5l1.5 2h-14l1.5-2Zm3.5 2a2 2 0 0 0 4 0",
  logout: "M14.5 7.5V6a1.5 1.5 0 0 0-1.5-1.5H6A1.5 1.5 0 0 0 4.5 6v12A1.5 1.5 0 0 0 6 19.5h7a1.5 1.5 0 0 0 1.5-1.5v-1.5M10 12h10m-3-3 3 3-3 3",
  bank: "M4 9.5 12 5l8 4.5M5.5 10v7m4.5-7v7m4-7v7m4.5-7v7M4 19.5h16",
} as const;

export type IconName = keyof typeof paths;

export function Icon({
  name,
  size = 22,
  color = colors.text,
  strokeWidth = 1.6,
}: {
  name: IconName;
  size?: number;
  color?: string;
  strokeWidth?: number;
}) {
  return (
    <Svg width={size} height={size} viewBox="0 0 24 24" fill="none" accessible={false}>
      <Path d={paths[name]} stroke={color} strokeWidth={strokeWidth} strokeLinecap="round" strokeLinejoin="round" />
    </Svg>
  );
}
