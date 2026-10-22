import catalog from '../../strategy-catalog.json';

// Display metadata only. IDs and technical revisions are not renamed.
export const strategies: Array<(typeof catalog.strategies)[number] & {publicationAuthor?: string; engineAvailability?: string}> = catalog.strategies;
export const soup = strategies.find(strategy => strategy.id === 'soup')!;

// Engines that ship in this release but cannot be installed from the app yet: every engine but Soup.
// Their decrypt-bridge-v2 adapters ship off and are set up with the bridge's own setup. The UI enforces
// this even if a catalog omits the flag.
export const comingSoonEngines = new Set<string>(strategies.filter(strategy => strategy.id !== 'soup').map(strategy => strategy.id));
export function engineComingSoon(engine: {id: string; customerAvailability?: string}) {
  return engine.customerAvailability === 'coming_soon' || comingSoonEngines.has(engine.id);
}

// Engine status from the catalog, as the user reads it. One label set for the strategy
// picker, the engine rows and the engine dialog. An unknown status shows no label.
export const engineStatusLabels: Record<string, {text: string; tone: 'teal' | 'amber' | 'violet' | 'muted'}> = {
  live: {text: 'Live', tone: 'teal'},
  hold: {text: 'On hold', tone: 'amber'},
  in_development: {text: 'In development', tone: 'violet'},
  parked: {text: 'Parked', tone: 'muted'},
};
const statusOrder = ['live', 'hold', 'in_development', 'parked'] as const;
// Listed strategies grouped by engine status, in reading order. Empty groups are left out.
export function engineStatusGroups() {
  return statusOrder
    .map(status => ({status, label: engineStatusLabels[status], items: strategies.filter(s => s.listed !== false && s.engineStatus === status)}))
    .filter(group => group.items.length > 0);
}
// "A", "A and B", "A, B and C".
export const listNames = (names: string[]) => names.length <= 1 ? names.join('') : names.slice(0, -1).join(', ') + ' and ' + names[names.length - 1];
// The bridge adapter, chart and TradingView alert setup notes of a strategy, from the catalog.
export function bridgeOf(id: string) {
  return strategyOf(id)?.bridge;
}
export function strategyOf(id: string) {
  return strategies.find(strategy => strategy.id === id);
}
// A strategy is shown in the catalog unless the catalog says listed: false.
export function strategyListed(id: string) {
  return strategyOf(id)?.listed !== false;
}

// Strategies the user can pick in Strategy access: catalog-listed, requestable, and
// with a public TradingView page to send people to. A strategy with no tradingviewUrl
// (e.g. one still in development) has nothing to request yet, so it is left out here
// rather than shown as a disabled option.
export function requestableStrategies() {
  return strategies.filter(s => s.listed !== false && s.accessRequestAvailable && s.tradingviewUrl);
}
