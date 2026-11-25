// Inline-SVG charts for the dashboard. No chart library — the data is
// small (appointments already on screen) and SVG keeps the bundle honest.

const BAR_W = 26
const BAR_GAP = 10
const CHART_H = 140

function iso(d) {
  const pad = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

function shortDay(dateStr) {
  const d = new Date(dateStr + 'T00:00:00')
  return d.toLocaleDateString(undefined, { weekday: 'short' }).slice(0, 2)
}

// Bookings per day over the last `days` days (today at the right edge).
export function BookingsPerDay({ appointments, days = 7 }) {
  const counts = []
  const labels = []
  const today = new Date()
  for (let i = days - 1; i >= 0; i--) {
    const d = new Date(today)
    d.setDate(today.getDate() - i)
    const key = iso(d)
    labels.push(shortDay(key))
    counts.push(appointments.filter((a) => a.date === key).length)
  }

  const max = Math.max(1, ...counts)
  const width = days * (BAR_W + BAR_GAP)
  const scale = CHART_H - 24

  return (
    <figure className="chart">
      <figcaption className="chart-cap">Bookings — last {days} days</figcaption>
      <svg
        viewBox={`0 0 ${width} ${CHART_H + 22}`}
        role="img"
        aria-label={`Bookings per day for the last ${days} days: ${counts.join(', ')}`}
        className="chart-svg"
      >
        {counts.map((n, i) => {
          const h = Math.round((n / max) * scale)
          const x = i * (BAR_W + BAR_GAP)
          const y = CHART_H - h
          return (
            <g key={i}>
              <rect
                x={x}
                y={y}
                width={BAR_W}
                height={h}
                rx="4"
                className={i === counts.length - 1 ? 'bar bar-now' : 'bar'}
              >
                <title>{n} booking{n === 1 ? '' : 's'}</title>
              </rect>
              {n > 0 && (
                <text x={x + BAR_W / 2} y={y - 5} className="bar-num" textAnchor="middle">
                  {n}
                </text>
              )}
              <text x={x + BAR_W / 2} y={CHART_H + 15} className="bar-label" textAnchor="middle">
                {labels[i]}
              </text>
            </g>
          )
        })}
      </svg>
    </figure>
  )
}

// Status split as a donut. Segments are drawn as circle arcs.
export function StatusDonut({ appointments }) {
  const segments = [
    { key: 'booked', label: 'Booked', color: 'var(--accent)' },
    { key: 'completed', label: 'Completed', color: 'var(--ok)' },
    { key: 'cancelled', label: 'Cancelled', color: 'var(--danger)' },
  ]
    .map((s) => ({ ...s, n: appointments.filter((a) => a.status === s.key).length }))
    .filter((s) => s.n > 0)

  const total = segments.reduce((sum, s) => sum + s.n, 0)
  const R = 44
  const C = 2 * Math.PI * R
  let offset = 0

  return (
    <figure className="chart">
      <figcaption className="chart-cap">Status split</figcaption>
      {total === 0 ? (
        <p className="muted small">Nothing to show yet.</p>
      ) : (
        <div className="donut-row">
          <svg viewBox="0 0 110 110" role="img" aria-label={segments.map((s) => `${s.label}: ${s.n}`).join(', ')} className="donut">
            <circle cx="55" cy="55" r={R} className="donut-track" />
            {segments.map((s) => {
              const frac = s.n / total
              const dash = frac * C
              const el = (
                <circle
                  key={s.key}
                  cx="55"
                  cy="55"
                  r={R}
                  className="donut-seg"
                  stroke={s.color}
                  strokeDasharray={`${dash} ${C - dash}`}
                  strokeDashoffset={-offset}
                >
                  <title>{`${s.label}: ${s.n}`}</title>
                </circle>
              )
              offset += dash
              return el
            })}
            <text x="55" y="52" className="donut-total" textAnchor="middle">
              {total}
            </text>
            <text x="55" y="68" className="donut-sub" textAnchor="middle">
              total
            </text>
          </svg>
          <ul className="legend">
            {segments.map((s) => (
              <li key={s.key}>
                <span className="dot" style={{ background: s.color }} />
                {s.label} <b className="mono">{s.n}</b>
              </li>
            ))}
          </ul>
        </div>
      )}
    </figure>
  )
}

// Horizontal utilisation bars: how many appointments each doctor has.
export function DoctorUtilisation({ appointments }) {
  const byDoctor = new Map()
  for (const a of appointments) {
    if (a.status === 'cancelled') continue
    const name = a.doctor_name || 'Unknown'
    byDoctor.set(name, (byDoctor.get(name) || 0) + 1)
  }
  const rows = [...byDoctor.entries()].sort((a, b) => b[1] - a[1]).slice(0, 8)

  if (rows.length === 0) return null
  const max = Math.max(...rows.map(([, n]) => n))

  return (
    <figure className="chart">
      <figcaption className="chart-cap">Doctor load (active + completed)</figcaption>
      <ul className="util-list">
        {rows.map(([name, n]) => (
          <li key={name}>
            <span className="util-name">{name}</span>
            <span className="util-track">
              <span className="util-fill" style={{ width: `${Math.round((n / max) * 100)}%` }} />
            </span>
            <span className="util-num mono">{n}</span>
          </li>
        ))}
      </ul>
    </figure>
  )
}
