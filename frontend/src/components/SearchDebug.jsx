import { useState } from 'react'
import { api } from '../api.js'

// Iteration 3, R2: Staff type a question and see the top 4 chunks from
// keyword only, vector only and hybrid — the view to check when an answer
// is wrong (bad retrieval, not a bad prompt).
export default function SearchDebug() {
  const [query, setQuery] = useState('')
  const [result, setResult] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function run() {
    const q = query.trim()
    if (!q || busy) return
    setBusy(true)
    setError('')
    try {
      setResult(await api.searchDebug(q))
    } catch (err) {
      setResult(null)
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  const columns = result
    ? [
        ['Keyword (BM25)', result.keyword],
        ['Vector', result.vector],
        ['Hybrid (top 4)', result.hybrid],
      ]
    : []

  return (
    <section className="card">
      <h2 className="section-title">Search debug</h2>
      <p className="muted small">
        Which chunks does each search find? Keyword = words, Vector = meaning, Hybrid = both.
      </p>
      <div className="chat-input-row">
        <input
          className="chat-input"
          type="text"
          placeholder='e.g. Should I bring my HbA1c report?'
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && run()}
        />
        <div className="chat-side">
          <button className="btn" onClick={run} disabled={busy || !query.trim()}>
            {busy ? 'Searching…' : 'Search'}
          </button>
        </div>
      </div>

      {error && <div className="alert">{error}</div>}

      {result && (
        <div className="debug-cols">
          {columns.map(([label, hits]) => (
            <div key={label} className="debug-col">
              <h3 className="sub-title">{label}</h3>
              {hits.length === 0 && <p className="muted small">No chunks.</p>}
              {hits.map((h, i) => (
                <div key={h.chunk_id + i} className="debug-hit">
                  <div className="debug-meta">
                    <span className="mono">#{i + 1}</span>
                    <span className="debug-score">{h.score}</span>
                  </div>
                  <div className="debug-source">
                    {h.source} &gt; {h.section}
                  </div>
                  <p className="debug-text">{h.text}</p>
                </div>
              ))}
            </div>
          ))}
        </div>
      )}
    </section>
  )
}
