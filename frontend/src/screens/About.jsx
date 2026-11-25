import { useEffect, useState } from 'react'
import { api } from '../api.js'

// Public clinic story: hours, doctors, specialities — straight from /api/clinic.
export default function About() {
  const [clinic, setClinic] = useState(null)
  const [error, setError] = useState('')

  useEffect(() => {
    api
      .clinic()
      .then(setClinic)
      .catch((err) => setError(err.message))
  }, [])

  if (error) return <div className="alert">{error}</div>
  if (!clinic) return <p className="muted">Loading clinic information…</p>

  return (
    <div className="stack">
      <section className="card">
        <h1 className="title">About {clinic.name}</h1>
        <p className="muted">{clinic.tagline}</p>
        <p style={{ marginTop: 14 }}>{clinic.intro}</p>
        <div className="notes">
          <div className="note">{clinic.address}</div>
          <div className="note">{clinic.email}</div>
          <div className="note">Consultations: {clinic.sessions}</div>
          <div className="note">
            Visit length — new {clinic.visit_length.new} min · follow-up{' '}
            {clinic.visit_length.follow_up} min
          </div>
        </div>
      </section>

      <section className="card">
        <h2 className="section-title">Our doctors</h2>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Doctor</th>
                <th>Specialities</th>
              </tr>
            </thead>
            <tbody>
              {clinic.doctors.map((d) => (
                <tr key={d.doctor_id}>
                  <td>{d.name}</td>
                  <td>{d.specialities.join(', ')}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="card">
        <h2 className="section-title">Opening hours</h2>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Day</th>
                <th>Hours</th>
              </tr>
            </thead>
            <tbody>
              {clinic.hours.map((h) => (
                <tr key={h.day}>
                  <td>{h.day}</td>
                  <td className={h.closed ? 'muted' : ''}>{h.hours}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  )
}
