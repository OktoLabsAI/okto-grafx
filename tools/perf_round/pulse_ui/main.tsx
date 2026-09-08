// Actual Pulse API client + actual WebGL canvas; only the surrounding controls are a fixture.
import { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { GraphCanvas } from '@/components/knowledge/GraphCanvas';
import { getSubgraph } from '@/services/kg-api';
import type { KGNode, KGEdge } from '@/types/knowledge-graph';
import './style.css';

function App() {
  const [nodes, setNodes] = useState<KGNode[]>([]);
  const [edges, setEdges] = useState<KGEdge[]>([]);
  const [cursor, setCursor] = useState<string | null>('');
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState('Ready — synthetic data only');
  async function load(reset: boolean) {
    setBusy(true);
    const started = performance.now();
    try {
      const page = await getSubgraph('benchmark', { limit: 500, cursor: reset ? '' : cursor ?? '' });
      const received = performance.now();
      if (page.metadata.edge_read_status !== 'ok') throw Error(JSON.stringify(page.metadata));
      const allNodes = reset ? page.nodes : [...nodes, ...page.nodes];
      if (new Set(allNodes.map(n => n.id)).size !== allNodes.length) throw Error('Duplicate page identities');
      const allEdges = [...new Map([...(reset ? [] : edges), ...page.edges].map(e => [e.id, e])).values()];
      setNodes(allNodes); setEdges(allEdges); setCursor(page.next_cursor);
      requestAnimationFrame(() => requestAnimationFrame(() => {
        setStatus(`Loaded ${allNodes.length} nodes; ${allEdges.length} edges retained; ` +
          `HTTP + JSON ${(received-started).toFixed(1)} ms; update to second frame ${(performance.now()-received).toFixed(1)} ms. ` +
          'Frame timing is not ForceAtlas2 convergence.');
        setBusy(false);
      }));
    } catch (error) { setStatus(String(error)); setBusy(false); }
  }
  return <main>
    <header><h1>Grafx 0.0.5 / Pulse GraphCanvas — isolated fixture</h1>
      <p>Real Pulse GET route, API client and renderer. Fixture authorization; no production data or workers.</p>
      <button disabled={busy} onClick={() => load(true)}>Load first 500</button>{' '}
      <button disabled={busy || !nodes.length || cursor === null} onClick={() => load(false)}>Load next 500</button>
      <p role="status">{busy ? 'Loading…' : status}</p>
    </header>
    <section style={{ height: '75vh', position: 'relative' }}>
      <GraphCanvas nodes={nodes} edges={edges} filters={{ types: [], edgeTypes: [], minRelevance: 0, searchQuery: '' }} />
    </section>
  </main>;
}
createRoot(document.getElementById('root')!).render(<App />);
