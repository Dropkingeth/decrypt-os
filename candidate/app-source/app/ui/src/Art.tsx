// Code-native line art for Decrypt OS. Stroke-only, currentColor, no raster.
// Every drawing is decorative; text next to it carries the meaning.
import type { ReactNode } from 'react';

type IconProps = { size?: number; className?: string };

function Svg({ children, size = 24, box = 24, className }: IconProps & { children: ReactNode; box?: number }) {
  return <svg className={className} width={size} height={size} viewBox={`0 0 ${box} ${box}`} aria-hidden="true" focusable="false" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">{children}</svg>;
}

/* Navigation */
const navPaths: Record<string, ReactNode> = {
  home: <><path d="M3 10.5 12 3l9 7.5"/><path d="M5 9.5V21h14V9.5"/><path d="M10 21v-6h4v6"/></>,
  setup: <><path d="M4 6h9M4 12h9M4 18h9"/><path d="m16 6 2 2 3-4"/><path d="m16 12 2 2 3-4"/><circle cx="18.5" cy="18" r="1.5"/></>,
  engines: <><rect x="3.5" y="3.5" width="7" height="7" rx="1.5"/><rect x="13.5" y="3.5" width="7" height="7" rx="1.5"/><rect x="3.5" y="13.5" width="7" height="7" rx="1.5"/><path d="M13.5 17h7M17 13.5v7"/></>,
  connections: <><path d="M10 14a4 4 0 0 0 5.7 0l2.3-2.3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0L6 12.3a4 4 0 0 0 5.7 5.7l1-1"/></>,
  activity: <><circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/></>,
  trading: <><path d="M4 18 9 12l3.5 3.5L19 7"/><path d="m15 7 4 0 0 4"/><path d="M4 21h16"/></>,
  access: <><circle cx="8.5" cy="12" r="4.5"/><path d="M13 12h8M18 12v3M21 12v2.5"/></>,
  care: <><path d="M12 3 4.5 6v6c0 4.5 3.2 7.7 7.5 9 4.3-1.3 7.5-4.5 7.5-9V6z"/><path d="m9 12 2 2 4-4.5"/></>,
};
export function NavIcon({ name }: { name: keyof typeof navPaths | string }) {
  return <Svg size={19}>{navPaths[name] ?? navPaths.home}</Svg>;
}

/* Setup step icons, one per onboarding step id */
const stepPaths: Record<string, ReactNode> = {
  welcome: <><circle cx="16" cy="16" r="11"/><path d="m21 11-3 7-7 3 3-7z"/><circle cx="16" cy="16" r="1" fill="currentColor"/></>,
  assistant: <><path d="M16 5.5 18.3 11l5.5 2.3-5.5 2.3L16 21l-2.3-5.4L8.2 13.3l5.5-2.3z"/><path d="M7 22.5v4M5 24.5h4"/><path d="M25 21v3M23.5 22.5h3"/></>,
  bot: <><path d="M8 20V12M8 10v-1M8 22v1"/><rect x="6" y="12" width="4" height="8" rx=".8"/><path d="M16 17V9M16 7V6M16 19v1"/><rect x="14" y="9" width="4" height="8" rx=".8"/><path d="M24 24V14M24 12v-1M24 26v1"/><rect x="22" y="14" width="4" height="10" rx=".8"/></>,
  connections: <><path d="M13 19a4.5 4.5 0 0 0 6.4 0l2.6-2.6a4.5 4.5 0 0 0-6.4-6.4l-1.2 1.2"/><path d="M19 13a4.5 4.5 0 0 0-6.4 0L10 15.6a4.5 4.5 0 0 0 6.4 6.4l1.2-1.2"/></>,
  access: <><circle cx="11.5" cy="13.5" r="5"/><path d="m15 17 9.5 9.5"/><path d="m21 23 2-2M23.5 25.5l2-2"/><circle cx="11.5" cy="13.5" r="1.2" fill="currentColor"/></>,
  review: <><rect x="7" y="5" width="18" height="22" rx="2"/><path d="M11 11h10M11 16h10M11 21h6"/><path d="m20 20 1.6 1.6L25 18"/></>,
};
export function StepIcon({ step, size = 30 }: { step: string; size?: number }) {
  return <Svg size={size} box={32}>{stepPaths[step] ?? stepPaths.welcome}</Svg>;
}

/* Strategy glyphs, keyed by catalog id. Abstract price motifs, not charts with data. */
const strategyPaths: Record<string, ReactNode> = {
  soup: <><path d="M6 26c5 0 6-12 12-12s7 12 12 12"/><path d="M8 14h24" strokeDasharray="2 3"/><path d="m26 10 4 4-4 4"/><circle cx="18" cy="14" r="1.6" fill="currentColor"/></>,
  'fvg-ifvg': <><path d="M10 8v6M10 22v6M20 6v4M20 24v6M30 10v6M30 24v4"/><rect x="7.5" y="14" width="5" height="8" rx=".8"/><rect x="17.5" y="10" width="5" height="14" rx=".8" fill="currentColor" fillOpacity=".18"/><rect x="27.5" y="16" width="5" height="8" rx=".8"/><path d="M12.5 16h15" strokeDasharray="2 2"/></>,
  'ote-silver-bullet': <><circle cx="20" cy="20" r="11"/><circle cx="20" cy="20" r="5.5"/><path d="M4 20h9M27 20h9M20 4v9M20 27v9"/><circle cx="20" cy="20" r="1.4" fill="currentColor"/></>,
  'ote-refined-small': <><path d="M8 10h6M8 30h6M32 10h-6M32 30h-6"/><path d="M8 10v20M32 10V30"/><path d="M13 24c3 0 4-8 7-8s4 8 7 8"/><circle cx="20" cy="16" r="1.4" fill="currentColor"/></>,
  'ote-refined-large': <><path d="M5 8h8M5 32h8M35 8h-8M35 32h-8"/><path d="M5 8v24M35 8V32"/><path d="M11 26c3 0 5-14 9-14s6 14 9 14"/><path d="M11 20h18" strokeDasharray="2 3"/><circle cx="20" cy="12" r="1.4" fill="currentColor"/></>,
  // Lumi RTH Gap: the prior close level, the open level, and the shaded gap between them (the session break is dashed).
  'lumi-rth-gap': <><path d="M4 26h11M25 14h11"/><rect x="15" y="14" width="10" height="12" fill="currentColor" fillOpacity=".16" stroke="none"/><path d="M15 26V14M25 26V14" strokeDasharray="2 2"/><path d="M4 32h32" strokeDasharray="2 3"/><circle cx="20" cy="20" r="1.4" fill="currentColor"/></>,
  // Order Blocks Decoded: price revisits two shaded blocks, one below and one above.
  'order-blocks-decoded': <><rect x="7.5" y="23.5" width="8" height="7" rx=".8" fill="currentColor" fillOpacity=".16"/><rect x="25.5" y="7.5" width="8" height="7" rx=".8" fill="currentColor" fillOpacity=".16"/><path d="m4 18 7.5 9 6.5-14 6 12 5.5-16 6.5 5"/><circle cx="29.5" cy="9" r="1.4" fill="currentColor"/></>,
};
export function StrategyGlyph({ id, size = 40 }: { id: string; size?: number }) {
  return <Svg size={size} box={40} className="glyph">{strategyPaths[id] ?? strategyPaths['ote-refined-small']}</Svg>;
}

/* Connection and module icons */
const smallPaths: Record<string, ReactNode> = {
  module: <><path d="M8 3v5M16 3v5M6 8h12v4a6 6 0 0 1-12 0zM12 18v3"/></>,
  chat: <><path d="M4 6.5A2.5 2.5 0 0 1 6.5 4h11A2.5 2.5 0 0 1 20 6.5v7a2.5 2.5 0 0 1-2.5 2.5H10l-4.5 4v-4A2.5 2.5 0 0 1 4 13.5z"/><path d="m12 7 1 2.4 2.4 1-2.4 1L12 13.8l-1-2.4-2.4-1 2.4-1z"/></>,
  key: <><circle cx="8.5" cy="12" r="4.5"/><path d="M13 12h8M18 12v3M21 12v2.5"/></>,
  chart: <><path d="M4 18 9 12l3.5 3.5L19 7"/><path d="m15 7 4 0 0 4"/><path d="M4 21h16"/></>,
  vault: <><rect x="4" y="4" width="16" height="16" rx="2"/><circle cx="12" cy="12" r="3.5"/><path d="M12 8.5v2M12 13.5v2M8.5 12h2M13.5 12h2"/></>,
  runtime: <><rect x="3.5" y="5" width="17" height="13" rx="2"/><path d="m7 9 3 2.5L7 14M12 14h5"/></>,
  calendar: <><rect x="3.5" y="5" width="17" height="15" rx="2"/><path d="M3.5 10h17M8 3v4M16 3v4"/></>,
  broker: <><path d="M4 8h16v10H4z"/><path d="M4 12h16M9 8V6h6v2"/></>,
  data: <><ellipse cx="12" cy="6.5" rx="7" ry="2.5"/><path d="M5 6.5v11c0 1.4 3.1 2.5 7 2.5s7-1.1 7-2.5v-11"/><path d="M5 12c0 1.4 3.1 2.5 7 2.5s7-1.1 7-2.5"/></>,
  signal: <><path d="M4 18c0-7.7 6.3-14 14-14"/><path d="M4 12.5c4.7 0 8.5 3.8 8.5 8.5" opacity=".6"/><circle cx="5.5" cy="19.5" r="1.5" fill="currentColor"/></>,
  engine: <><circle cx="12" cy="12" r="3"/><path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.2 2.2M16.2 16.2l2.2 2.2M5.6 18.4l2.2-2.2M16.2 7.8l2.2-2.2"/></>,
  rollover: <><path d="M4 12a8 8 0 0 1 14-5.3"/><path d="M20 12a8 8 0 0 1-14 5.3"/><path d="M18 3v4h-4M6 21v-4h4"/></>,
  shield: <><path d="M12 3 4.5 6v6c0 4.5 3.2 7.7 7.5 9 4.3-1.3 7.5-4.5 7.5-9V6z"/><path d="m9 12 2 2 4-4.5"/></>,
  spark: <><path d="M12 3.5 14.4 9.6 20.5 12l-6.1 2.4L12 20.5l-2.4-6.1L3.5 12l6.1-2.4z"/></>,
  refresh: <><path d="M20 12a8 8 0 1 1-2.3-5.7"/><path d="M20 4v4.5h-4.5"/></>,
  empty: <><circle cx="12" cy="12" r="8.5" strokeDasharray="3 3"/><path d="M12 8v4l2.5 1.5"/></>,
};
export function Icon({ name, size = 20 }: { name: keyof typeof smallPaths | string; size?: number }) {
  return <Svg size={size}>{smallPaths[name] ?? smallPaths.spark}</Svg>;
}
export const moduleIcon: Record<string, string> = {
  'guardian-observer': 'shield',
};

/* Flask mark, exact decryptlabs.io paths */
export function Flask({ size = 30 }: { size?: number }) {
  return <svg width={size} height={Math.round(size * 36 / 32)} viewBox="0 0 32 36" aria-hidden="true" focusable="false" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"><path d="M11 3h10m-8 0v10L5 28q-2 5 3 5h16q5 0 3-5l-8-15V3M10 23h12"/><circle cx="14" cy="27" r="1.4"/><circle cx="19" cy="21" r="1"/></svg>;
}
