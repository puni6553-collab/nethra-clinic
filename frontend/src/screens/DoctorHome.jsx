import { useCallback, useEffect, useState } from 'react'
import { api } from '../api.js'
import AppointmentList from './AppointmentList.jsx'

// Doctor: schedule with reason/urgent tag, required note on complete,
// and an AI summary when opening a patient (R6).
export default function DoctorHome({ me }) {
  const today = new Date()
  const pad = (n) => String(n).padStart(2, '0')
  const [date, setDate] = useState(
    `${today.getFullYear()}-${pad(today.getMonth() + 1)}-${pad(today.getDate())}`,
  )
  const [appointments, setAppointments] = useState([])
  const [error, setError] = useState('')
  const [flash, setFlash] = useState('')
  const [noteFor, setNoteFor] = useState(null) // appointment waiting for a note
  const [noteText, setNoteText] = useState('')
  const [noteBusy, setNoteBusy] = useState(false)
  const [summary, setSummary] = useState(null) // {patient_id, ...} or null
  const [summaryBusy, setSummaryBusy] = useState(false)
  const [leaveDate, setLeaveDate] = useState('') // R7: the doctor marks leave
  const [leaveSession, setLeaveSession] = useState('FULL')
  const [leaveBusy, setLeaveBusy] = useState(false)
  const [myLeave, setMyLeave] = useState([])

  const refresh = useCallback(() => {
    api
      .appointments({ date })
      .then(setAppointments)
      .catch((err) => setError(err.message))
  }, [date])

  useEffect(refresh, [refresh])

  const refreshLeave = useCallback(() => {
    api.myLeave().then(setMyLeave).catch(() => {})
  }, [])

  useEffect(refreshLeave, [refreshLeave])

  const iso = (d) =>
    `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
  const leaveMin = iso(today)
  const leaveMax = iso(new Date(today.getTime() + 30 * 86400000))

  async function markLeave() {
    if (!leaveDate) {
      setError('Pick a date for your leave.')
      return
    }
    setLeaveBusy(true)
    setError('')
    try {
      const res = await api.doctorLeave({ date: leaveDate, session: leaveSession })
      setFlash(
        `Leave marked for ${leaveDate} — ${res.rebooked} appointment${res.rebooked === 1 ? '' : 's'} sent to Staff for rebooking.`,
      )
      setLeaveDate('')
      setLeaveSession('FULL')
      refreshLeave()
      refresh()
    } catch (err) {
      setError(err.message)
    } finally {
      setLeaveBusy(false)
    }
  }

  // "proposed" as well: the row a leave produced, still waiting for Staff.
  const active = appointments.filter(
    (a) => a.status === 'booked' || a.status === 'proposed',
  )
  const rest = appointments.filter(
    (a) => a.status !== 'booked' && a.status !== 'proposed',
  )

  async function submitNote() {
    if (!noteFor) return
    const note = noteText.trim()
    if (!note) {
      setError('A short note is required when completing a visit.')
      return
    }
    setNoteBusy(true)
    setError('')
    try {
      await api.complete(noteFor.id, note)
      setFlash(`Completed ${noteFor.patient_name}'s ${noteFor.speciality} visit — a follow-up with you is now allowed.`)
      setNoteFor(null)
      setNoteText('')
      refresh()
    } catch (err) {
      setError(err.message)
    } finally {
      setNoteBusy(false)
    }
  }

  async function openSummary(a) {
    setSummaryBusy(true)
    setError('')
    setSummary(null)
    try {
      // Pass the schedule row so "today's reason" is this booking's reason,
      // even when it is booked for a future date (handout §09).
      const data = await api.summary(a.patient_id, a.id)
      setSummary(data)
    } catch (err) {
      setError(err.message)
    } finally {
      setSummaryBusy(false)
    }
  }

  return (
    <div className="stack">
      <section className="card">
        <h2 className="section-title">My schedule</h2>
        <div className="filters">
          <label>
            Day
            <input type="date" value={date} onChange={(e) => setDate(e.target.value)} />
          </label>
          <span className="muted">
            Viewing as <b>{me.name}</b>
          </span>
        </div>
        {flash && <div className="alert alert-ok">{flash}</div>}
      </section>

      <section className="card">
        <h2 className="section-title">Mark leave</h2>
        <p className="muted small">
          Within the next 30 days. Anything you already have booked in that session is
          cancelled and sent to Staff with a new time to approve.
        </p>
        <div className="filters">
          <label>
            Date
            <input
              type="date"
              min={leaveMin}
              max={leaveMax}
              value={leaveDate}
              onChange={(e) => setLeaveDate(e.target.value)}
            />
          </label>
          <label>
            Session
            <select value={leaveSession} onChange={(e) => setLeaveSession(e.target.value)}>
              <option value="AM">Morning (AM)</option>
              <option value="PM">Afternoon (PM)</option>
              <option value="FULL">Full day</option>
            </select>
          </label>
          <button
            className="btn btn-primary"
            onClick={markLeave}
            disabled={leaveBusy || !leaveDate}
          >
            {leaveBusy ? 'Marking…' : 'Mark leave'}
          </button>
        </div>
        {myLeave.length > 0 && (
          <ul className="next-list" style={{ marginTop: 8 }}>
            {myLeave.map((l) => (
              <li key={l.id}>
                <span className="next-when mono">{l.date}</span>
                <span className="tag">{l.session}</span>
                <span className="muted">
                  {l.rebooked > 0
                    ? `${l.rebooked} appointment${l.rebooked === 1 ? '' : 's'} moved — waiting for Staff`
                    : 'nothing booked in that session'}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>

      {noteFor && (
        <section className="card">
          <h2 className="section-title">
            Complete visit — {noteFor.patient_name} · {noteFor.speciality}
          </h2>
          <label>
            Doctor note (required)
            <textarea
              rows={3}
              value={noteText}
              onChange={(e) => setNoteText(e.target.value)}
              placeholder="Findings, treatment, follow-up plan…"
            />
          </label>
          <div className="row" style={{ marginTop: 10 }}>
            <button
              className="btn btn-primary"
              onClick={submitNote}
              disabled={noteBusy || !noteText.trim()}
            >
              {noteBusy ? 'Saving…' : 'Save note & complete'}
            </button>
            <button
              className="btn btn-ghost"
              onClick={() => { setNoteFor(null); setNoteText(''); setError('') }}
            >
              Cancel
            </button>
          </div>
        </section>
      )}

      {summary && (
        <section className="card summary-card">
          <div className="fact-head">
            <h2 className="section-title" style={{ marginBottom: 0 }}>
              {summary.patient_name}
            </h2>
            <button className="btn btn-small" onClick={() => setSummary(null)}>
              Close
            </button>
          </div>
          <p className="summary-label">{summary.label}</p>
          <p className="summary-text">{summary.summary}</p>
          <h3 className="sub-title">Past notes</h3>
          {summary.notes.length === 0 ? (
            <p className="muted">No previous visits on record.</p>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Date</th>
                    <th>Doctor</th>
                    <th>Speciality</th>
                    <th>Reason</th>
                    <th>Note</th>
                  </tr>
                </thead>
                <tbody>
                  {summary.notes.map((n, i) => (
                    <tr key={i}>
                      <td className="mono">{n.date}</td>
                      <td>{n.doctor}</td>
                      <td>{n.speciality}</td>
                      <td>{n.reason || '—'}</td>
                      <td>{n.note || '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      )}

      <AppointmentList
        title="Today's patients"
        appointments={active}
        empty="No appointments booked for this day."
        canComplete={(a) => a.status === 'booked' && a.doctor_id === me.doctor_id}
        onComplete={(a) => { setNoteFor(a); setNoteText(''); setError(''); }}
        onOpen={(a) => openSummary(a)}
        canOpen={() => true}
        error={error}
        showReason
        showUrgent
        showMoved
        showIntake
      />

      <AppointmentList
        title="Completed & cancelled"
        appointments={rest}
        empty="Nothing yet."
        showStatus
        showReason
        showNote
        showMoved
        showIntake
      />
    </div>
  )
}
