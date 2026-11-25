import { useEffect, useMemo, useState } from 'react'
import { api } from '../api.js'

// The booking form shared by Patient (books for self) and Staff (books for
// any patient, including walk-ins). It never decides what is allowed — it
// asks the backend for free slots and shows whatever refusal comes back.
// Iteration 2: requires a reason; the backend runs the red-flag check first.
export default function BookingForm({ me, onBooked }) {
  const [doctors, setDoctors] = useState([])
  const [patients, setPatients] = useState([]) // staff only
  const [form, setForm] = useState({
    doctor_id: '',
    speciality: '',
    visit_type: 'new',
    date: '',
    patient_id: '',
    mobile: '',
    patient_name: '',
    reason: '',
  })
  const [slotData, setSlotData] = useState({ slots: [], notes: [] })
  const [notice, setNotice] = useState(null) // {kind, text, emergency}
  const [busy, setBusy] = useState(false)

  const isStaff = me.role === 'staff'

  useEffect(() => {
    api.doctors().then(setDoctors).catch(() => {})
    if (isStaff) api.patients().then(setPatients).catch(() => {})
  }, [isStaff])

  const doctor = useMemo(
    () => doctors.find((d) => d.doctor_id === form.doctor_id) || null,
    [doctors, form.doctor_id],
  )

  const specialities = doctor ? doctor.specialities : []

  // Earliest date the API will accept (today) and the 30-day horizon.
  const today = new Date()
  const pad = (n) => String(n).padStart(2, '0')
  const iso = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
  const minDate = iso(today)
  const horizon = new Date(today)
  horizon.setDate(horizon.getDate() + 30)
  const maxDate = iso(horizon)

  function set(field) {
    return (e) => setForm({ ...form, [field]: e.target.value })
  }

  function onPickDoctor(id) {
    const picked = doctors.find((d) => d.doctor_id === id)
    setForm({
      ...form,
      doctor_id: id,
      speciality: picked ? picked.specialities[0] : '',
    })
    setSlotData({ slots: [], notes: [] })
  }

  // Fetch free slots whenever the doctor/date/visit type lines up.
  useEffect(() => {
    const abort = { cancelled: false }
    if (!form.doctor_id || !form.date) {
      setSlotData({ slots: [], notes: [] })
      return
    }
    api
      .slots({ doctor_id: form.doctor_id, date: form.date, visit_type: form.visit_type })
      .then((data) => {
        if (!abort.cancelled) setSlotData({ slots: data.slots, notes: data.notes })
      })
      .catch((err) => {
        if (!abort.cancelled) setSlotData({ slots: [], notes: [err.message] })
      })
    return () => {
      abort.cancelled = true
    }
  }, [form.doctor_id, form.date, form.visit_type])

  async function book(start_time) {
    const reason = form.reason.trim()
    if (!reason) {
      setNotice({ kind: 'err', text: 'Please give a reason for the visit.' })
      return
    }
    setBusy(true)
    setNotice(null)
    try {
      const payload = {
        doctor_id: form.doctor_id,
        speciality: form.speciality,
        visit_type: form.visit_type,
        date: form.date,
        start_time,
        reason,
      }
      if (isStaff) {
        if (form.patient_id) payload.patient_id = form.patient_id
        else {
          payload.mobile = form.mobile
          payload.patient_name = form.patient_name
        }
      }
      const appt = await api.book(payload)
      // Emergency response first, in red (handout R4).
      if (appt.emergency_message) {
        setNotice({
          kind: 'emergency',
          text: appt.emergency_message,
          after: `Booking still confirmed: ${appt.date} at ${appt.start_time} with ${appt.doctor_name}.`,
        })
      } else {
        setNotice({
          kind: 'ok',
          text: `Booked ${appt.date} at ${appt.start_time} with ${appt.doctor_name}.`,
        })
      }
      setForm((f) => ({ ...f, reason: '' }))
      // Refresh the slot list: the one we just took must disappear.
      const data = await api.slots({
        doctor_id: form.doctor_id,
        date: form.date,
        visit_type: form.visit_type,
      })
      setSlotData({ slots: data.slots, notes: data.notes })
      if (onBooked) onBooked(appt)
    } catch (err) {
      // A red-flag reason that a clinic rule then refused: show the
      // emergency response first (in red), the refusal underneath (R4).
      const detail = err.data && err.data.detail
      const emergency = detail && detail.emergency_message
      if (emergency) {
        setNotice({ kind: 'emergency', text: emergency, after: err.message })
      } else {
        setNotice({ kind: 'err', text: err.message })
      }
    } finally {
      setBusy(false)
    }
  }

  const reasonOver = form.reason.length > 300

  return (
    <section className="card">
      <h2 className="section-title">Book an appointment</h2>

      <div className="form-grid">
        {isStaff && (
          <label className="span-2">
            Patient
            <div className="row">
              <select value={form.patient_id} onChange={set('patient_id')}>
                <option value="">— walk-in (new patient) —</option>
                {patients.map((p) => (
                  <option key={p.patient_id} value={p.patient_id}>
                    {p.patient_id} · {p.name} · {p.mobile}
                  </option>
                ))}
              </select>
            </div>
            {!form.patient_id && (
              <div className="row row-tight">
                <input
                  inputMode="numeric"
                  placeholder="mobile number"
                  value={form.mobile}
                  onChange={set('mobile')}
                />
                <input
                  placeholder="patient name"
                  value={form.patient_name}
                  onChange={set('patient_name')}
                />
              </div>
            )}
          </label>
        )}

        <label className="span-2">
          What is the reason for your visit?
          <textarea
            rows={2}
            maxLength={300}
            placeholder="e.g. Blurry vision when reading"
            value={form.reason}
            onChange={set('reason')}
            required
          />
          <span className={`hint ${reasonOver ? 'hint-bad' : ''}`}>
            {form.reason.length}/300 — required
          </span>
        </label>

        <label>
          Doctor
          <select value={form.doctor_id} onChange={(e) => onPickDoctor(e.target.value)}>
            <option value="">— select —</option>
            {doctors.map((d) => (
              <option key={d.doctor_id} value={d.doctor_id}>
                {d.name}
              </option>
            ))}
          </select>
        </label>

        <label>
          Speciality
          <select value={form.speciality} onChange={set('speciality')} disabled={!doctor}>
            {specialities.map((s) => (
              <option key={s} value={s}>{s}</option>
            ))}
            {!doctor && <option value="">—</option>}
          </select>
        </label>

        <label>
          Visit type
          <select value={form.visit_type} onChange={set('visit_type')}>
            <option value="new">New consultation · 20 min</option>
            <option value="follow_up">Follow-up · 10 min</option>
          </select>
        </label>

        <label>
          Date
          <input
            type="date"
            value={form.date}
            min={minDate}
            max={maxDate}
            onChange={set('date')}
          />
        </label>
      </div>

      {form.date && slotData.notes.length > 0 && (
        <div className="notes">
          {slotData.notes.map((n, i) => (
            <div className="note" key={i}>{n}</div>
          ))}
        </div>
      )}

      {form.doctor_id && form.date && slotData.slots.length > 0 && (
        <>
          <h3 className="sub-title">
            Free slots <span className="muted small">({slotData.slots.length})</span>
          </h3>
          <div className="slots">
            {slotData.slots.map((s) => (
              <button
                key={s}
                className="slot"
                disabled={busy}
                onClick={() => book(s)}
                title={`Book ${s}`}
              >
                {s}
              </button>
            ))}
          </div>
        </>
      )}

      {notice && notice.kind === 'emergency' && (
        <div className="alert alert-emergency" role="alert">
          <b>{notice.text}</b>
          {notice.after && <div className="small" style={{ marginTop: 6 }}>{notice.after}</div>}
        </div>
      )}
      {notice && notice.kind === 'ok' && (
        <div className="alert alert-ok" role="status">{notice.text}</div>
      )}
      {notice && notice.kind === 'err' && (
        <div className="alert" role="alert">{notice.text}</div>
      )}
    </section>
  )
}
