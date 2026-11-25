import { useCallback, useEffect, useState } from 'react'
import { api } from '../api.js'
import BookingForm from './BookingForm.jsx'
import AppointmentList from './AppointmentList.jsx'
import SearchDebug from '../components/SearchDebug.jsx'

// Staff: urgent cases on top, the approval queue for late changes, book for
// anyone (with reason), cancel anything.
export default function StaffHome({ me }) {
  const [appointments, setAppointments] = useState([])
  const [date, setDate] = useState('')
  const [doctorId, setDoctorId] = useState('')
  const [doctors, setDoctors] = useState([])
  const [error, setError] = useState('')
  const [urgent, setUrgent] = useState([])
  const [approvals, setApprovals] = useState([])
  const [rebookings, setRebookings] = useState([])

  useEffect(() => {
    api.doctors().then(setDoctors).catch(() => {})
  }, [])

  const refreshUrgent = useCallback(() => {
    api.urgentCases().then(setUrgent).catch(() => {})
  }, [])

  const refreshApprovals = useCallback(() => {
    api.staffApprovals().then(setApprovals).catch(() => {})
  }, [])

  const refreshRebookings = useCallback(() => {
    api.staffRebookings().then(setRebookings).catch(() => {})
  }, [])

  const refresh = useCallback(() => {
    setError('')
    api
      .appointments({ date, doctor_id: doctorId })
      .then(setAppointments)
      .catch((err) => setError(err.message))
    refreshUrgent()
    refreshApprovals()
    refreshRebookings()
  }, [date, doctorId, refreshUrgent, refreshApprovals, refreshRebookings])

  useEffect(refresh, [refresh])

  // "proposed" as well: a leave row waiting for Staff sits with its day.
  const active = appointments.filter(
    (a) => a.status === 'booked' || a.status === 'proposed',
  )
  const rest = appointments.filter(
    (a) => a.status !== 'booked' && a.status !== 'proposed',
  )
  // Marked-seen cases leave the list entirely (handout §09).
  const unseen = urgent.filter((u) => !u.seen)

  async function markSeen(u) {
    try {
      await api.markSeen(u.id)
      refreshUrgent()
    } catch (err) {
      setError(err.message)
    }
  }

  // Iteration 4 (R1): a patient's cancel/reschedule inside 2 hours waits here.
  async function decide(a, action) {
    setError('')
    try {
      if (action === 'approve') await api.approve(a.id)
      else await api.reject(a.id)
      refresh()
    } catch (err) {
      setError(err.message)
    }
  }

  // Iteration 4 (R7): a doctor went on leave — approve the new time, or
  // reject it and hand the slot back.
  async function decideRebooking(r, action) {
    setError('')
    try {
      if (action === 'approve') await api.approveRebooking(r.id)
      else await api.rejectRebooking(r.id)
      refresh()
    } catch (err) {
      setError(err.message)
    }
  }

  return (
    <div className="stack">
      {unseen.length > 0 && (
        <section className="card urgent-panel">
          <h2 className="section-title urgent-title">
            Urgent cases <span className="tag tag-urgent">{unseen.length} unseen</span>
          </h2>
          <ul className="next-list">
            {unseen.map((u) => (
              <li key={u.id} className="urgent-row">
                <span className="mono next-when">{u.created_at}</span>
                <span className="next-who">{u.patient_name}</span>
                <span className="urgent-reason">{u.reason}</span>
                {u.category && <span className="tag tag-urgent">{u.category}</span>}
                {u.source === 'chat' && <span className="tag">from chat</span>}
                {u.source === 'mcp' && <span className="tag">from MCP</span>}
                <button className="btn btn-small" onClick={() => markSeen(u)}>
                  Mark seen
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}

      {approvals.length > 0 && (
        <section className="card">
          <h2 className="section-title">
            Waiting for Staff <span className="tag tag-pending">{approvals.length}</span>
          </h2>
          <ul className="next-list">
            {approvals.map((a) => (
              <li key={a.id} className="urgent-row">
                <span className="next-who">{a.patient_name}</span>
                <span className="tag">{a.action === 'cancel' ? 'cancel' : 'reschedule'}</span>
                <span className="urgent-reason">
                  {a.appointment
                    ? `${a.appointment.date} ${a.appointment.start_time} with ${a.appointment.doctor_name}`
                    : `appointment #${a.appointment_id}`}
                </span>
                {a.action === 'reschedule' && a.payload && (
                  <span className="muted small">
                    → {a.payload.date} {a.payload.start_time}
                  </span>
                )}
                <span className="mono next-when">{a.created_at}</span>
                <button
                  className="btn btn-small btn-primary"
                  onClick={() => decide(a, 'approve')}
                >
                  Approve
                </button>
                <button className="btn btn-small" onClick={() => decide(a, 'reject')}>
                  Reject
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}

      {rebookings.length > 0 && (
        <section className="card">
          <h2 className="section-title">
            Moved because a doctor is on leave{' '}
            <span className="tag tag-pending">{rebookings.length}</span>
          </h2>
          <ul className="next-list">
            {rebookings.map((r) => (
              <li key={r.id} className="urgent-row">
                <span className={`tag tag-${r.status}`}>{r.status}</span>
                <span className="next-who">
                  {(r.original && r.original.patient_name) || 'appointment'}
                </span>
                <span className="urgent-reason">
                  {r.original
                    ? `${r.original.date} ${r.original.start_time} with ${r.original.doctor_name}`
                    : ''}
                </span>
                <span className="muted small">
                  Dr {r.doctor_name} on leave {r.leave_date} ({r.leave_session})
                </span>
                {r.proposed ? (
                  <span className="muted small">
                    → proposed {r.proposed.date} {r.proposed.start_time} with{' '}
                    {r.proposed.doctor_name}
                  </span>
                ) : (
                  <span className="muted small">{r.message || 'No slot: call the patient'}</span>
                )}
                {r.status === 'proposed' && (
                  <>
                    <button
                      className="btn btn-small btn-primary"
                      onClick={() => decideRebooking(r, 'approve')}
                    >
                      Approve
                    </button>
                    <button
                      className="btn btn-small"
                      onClick={() => decideRebooking(r, 'reject')}
                    >
                      Reject
                    </button>
                  </>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}

      <BookingForm me={me} onBooked={refresh} />

      <SearchDebug />

      <section className="card">
        <h2 className="section-title">Every appointment</h2>
        <div className="filters">
          <label>
            Day
            <input type="date" value={date} onChange={(e) => setDate(e.target.value)} />
          </label>
          <label>
            Doctor
            <select value={doctorId} onChange={(e) => setDoctorId(e.target.value)}>
              <option value="">All doctors</option>
              {doctors.map((d) => (
                <option key={d.doctor_id} value={d.doctor_id}>{d.name}</option>
              ))}
            </select>
          </label>
          {(date || doctorId) && (
            <button
              className="btn btn-ghost"
              onClick={() => { setDate(''); setDoctorId('') }}
            >
              Clear filters
            </button>
          )}
        </div>
      </section>

      <AppointmentList
        title="Booked"
        appointments={active}
        empty="No appointments match."
        canCancel={() => true} // staff may cancel anything
        onCancel={async (a) => {
          setError('')
          try {
            await api.cancel(a.id)
            refresh()
          } catch (err) {
            setError(err.message)
          }
        }}
        error={error}
        showReason
        showUrgent
        showMoved
        showVia
      />

      <AppointmentList
        title="Cancelled & completed"
        appointments={rest}
        empty="Nothing here."
        showStatus
        showReason
        showNote
        showVia
      />
    </div>
  )
}
