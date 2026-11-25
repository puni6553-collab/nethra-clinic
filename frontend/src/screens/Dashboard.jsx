import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { BookingsPerDay, StatusDonut, DoctorUtilisation } from '../components/Charts.jsx'

function iso(d) {
  const pad = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

// Overview for every role: counts from the appointments the API already
// scopes to the signed-in user, plus what is coming up next.
export default function Dashboard({ me }) {
  const [appointments, setAppointments] = useState([])
  const [doctors, setDoctors] = useState([])
  const [error, setError] = useState('')

  useEffect(() => {
    api
      .appointments()
      .then(setAppointments)
      .catch((err) => setError(err.message))
    if (me.role === 'staff') {
      api.doctors().then(setDoctors).catch(() => {})
    }
  }, [me.role])

  const today = iso(new Date())
  const booked = appointments.filter((a) => a.status === 'booked')
  const completed = appointments.filter((a) => a.status === 'completed')
  const cancelled = appointments.filter((a) => a.status === 'cancelled')
  const todayCount = booked.filter((a) => a.date === today).length
  const upcoming = booked.filter((a) => a.date >= today).slice(0, 5)

  const label =
    me.role === 'staff' ? 'Clinic overview' : me.role === 'doctor' ? 'My day at a glance' : 'Your visits at a glance'

  const stats = [
    { num: todayCount, label: 'Booked today', tone: '' },
    { num: booked.length, label: 'Active bookings', tone: '' },
    { num: completed.length, label: 'Completed', tone: 'stat-ok' },
    { num: cancelled.length, label: 'Cancelled', tone: 'stat-danger' },
    ...(me.role === 'staff'
      ? [{ num: doctors.length, label: 'Doctors', tone: '' }]
      : []),
  ]

  return (
    <section className="card">
      <div className="dash-head">
        <div>
          <h2 className="section-title">Dashboard</h2>
          <p className="muted small">{label} · {today}</p>
        </div>
      </div>

      <div className="stats">
        {stats.map((s) => (
          <div className={`stat ${s.tone}`} key={s.label}>
            <span className="stat-num">{s.num}</span>
            <span className="stat-label">{s.label}</span>
          </div>
        ))}
      </div>

      <h3 className="sub-title">Next up</h3>
      {upcoming.length === 0 ? (
        <p className="muted">Nothing booked from today onwards.</p>
      ) : (
        <ul className="next-list">
          {upcoming.map((a) => (
            <li key={a.id}>
              <span className="mono next-when">
                {a.date === today ? 'Today' : a.date} · {a.start_time}
              </span>
              <span className="next-who">
                {me.role === 'patient' ? a.doctor_name : a.patient_name}
              </span>
              <span className="muted small">
                {a.speciality} · {a.visit_type === 'new' ? 'new' : 'follow-up'}
              </span>
            </li>
          ))}
        </ul>
      )}

      <div className={`chart-grid ${me.role === 'staff' ? 'chart-grid-3' : ''}`}>
        <div className="chart-cell">
          <BookingsPerDay appointments={appointments} />
        </div>
        <div className="chart-cell">
          <StatusDonut appointments={appointments} />
        </div>
        {me.role === 'staff' && (
          <div className="chart-cell">
            <DoctorUtilisation appointments={appointments} />
          </div>
        )}
      </div>

      {error && <div className="alert">{error}</div>}
    </section>
  )
}
