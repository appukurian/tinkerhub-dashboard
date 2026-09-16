const fs = require('fs');
const baseline = require('./parsed.json');

const TODAY = '2026-09-16';
const GENERATED_AT = '2026-09-16T02:36:42Z';

function daysBetween(a, b) {
  // a, b are 'YYYY-MM-DD' strings, treat as UTC dates
  const da = new Date(a + 'T00:00:00Z');
  const db = new Date(b + 'T00:00:00Z');
  return Math.round((db - da) / 86400000);
}

const threads = baseline.threads.map(t => {
  const daysSinceReceived = daysBetween(t.received, TODAY);
  let daysOpen;
  if (t.status === 'Resolved' || t.status === 'Informational') {
    daysOpen = daysBetween(t.received, t.last);
  } else {
    daysOpen = daysSinceReceived;
  }
  return { ...t, daysOpen, daysSinceReceived };
});

// summary: counts per status
const summary = {};
for (const t of threads) {
  summary[t.status] = (summary[t.status] || 0) + 1;
}

// analytics per group
const groups = [...new Set(threads.map(t => t.group))];
const analytics = {};
for (const g of groups) {
  const gt = threads.filter(t => t.group === g);
  const statusCounts = {};
  for (const t of gt) statusCounts[t.status] = (statusCounts[t.status] || 0) + 1;
  const openThreads = gt.filter(t => t.status !== 'Resolved' && t.status !== 'Informational');
  const resolvedThreads = gt.filter(t => t.status === 'Resolved');
  const avgOpenDays = openThreads.length
    ? Math.round((openThreads.reduce((s, t) => s + t.daysOpen, 0) / openThreads.length) * 10) / 10
    : 0;
  const avgResolvedDays = resolvedThreads.length
    ? Math.round((resolvedThreads.reduce((s, t) => s + t.daysOpen, 0) / resolvedThreads.length) * 10) / 10
    : 0;
  analytics[g] = {
    total: gt.length,
    ...statusCounts,
    avgOpenDays,
    avgResolvedDays,
  };
}

const output = {
  generatedAt: GENERATED_AT,
  threads,
  summary,
  analytics,
};

const js = `window.DASHBOARD_DATA = ${JSON.stringify(output, null, 2)};\n`;
fs.writeFileSync('/tmp/thd-fetch/dashboard-data-new.js', js);
console.log('Written. Threads:', threads.length);
console.log('Summary:', JSON.stringify(summary));
console.log('Analytics:', JSON.stringify(analytics, null, 2));
