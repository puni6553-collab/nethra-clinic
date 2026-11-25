import { useCallback, useEffect, useState } from 'react'
import { api } from '../api.js'
import BookingForm from './BookingForm.jsx'
import AppointmentList from './AppointmentList.jsx'
import ChatPanel from '../components/ChatPanel.jsx'

// Patient: Fact of the Day, book (with reason), cancel — only their own.
export default function PatientHome({ me }) {
  const [appointments, setAppointments] = useState([])
  const [error, setError] = useState('')
  const [fact, setFact] = useState(null)

  const refresh = useCallback(() => {
    api
      .appointments()
      .then(setAppointments)
      .catch((err) => setError(err.message))
  }, [])

  useEffect(refresh, [refresh])

  // R5: fact on login; Refresh sends the current one so it changes.
  const loadFact = useCallback((previous) => {
    api
      .fact(previous)
      .then(setFact)
      .catch(() =>
        setFact({
          fact: 'Blinking spreads tears across the eye and helps keep the surface comfortable.',
          caveat: 'General information, not medical advice',
        }),
      )
  }, [])

  useEffect(() => {
    loadFact(null)
  }, [loadFact])

  // "proposed" too: a rebooking after leave is on its way and the patient
  // must see why the appointment moved (handout §05).
  const active = appointments.filter(
    (a) => a.status === 'booked' || a.status === 'proposed',
  )
  const history = appointments.filter(
    (a) => a.status !== 'booked' && a.status !== 'proposed',
  )

  return (
    <div className="stack">
      <section className="card fact-card">
        <div className="fact-head">
          <h2 className="section-title" style={{ marginBottom: 0 }}>Fact of the Day</h2>
          <button
            className="btn btn-small"
            onClick={() => loadFact(fact ? fact.fact : null)}
            disabled={!fact}
          >
            Refresh
          </button>
        </div>
        <p className="fact-text">{fact ? fact.fact : 'Loading…'}</p>
        <p className="fact-caveat">{fact ? fact.caveat : ''}</p>
      </section>

      <BookingForm me={me} onBooked={refresh} />

      <ChatPanel />

      <AppointmentList
        title="Your appointments"
        appointments={active}
        empty="Nothing booked yet."
        canCancel={(a) => a.patient_id === me.patient_id && a.status === 'booked'}
        onCancel={async (a) => {
          setError('')
          try {
            await api.cancel(a.id)
            refresh()
          } catch (err) {
            setError(err.message)
          }
        }}
        showStatus
        showReason
        showMoved
      />

      {error && <div className="alert">{error}</div>}

      <AppointmentList
        title="History"
        appointments={history}
        empty="No past appointments yet."
        showStatus
        showReason
        showNote
      />
    </div>
  )
}
