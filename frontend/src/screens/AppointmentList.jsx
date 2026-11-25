// Shared table for appointments. Cancel/complete buttons appear only when
// allowed — the backend is what actually refuses a bad call.
export default function AppointmentList({
  title,
  appointments,
  empty = 'Nothing here.',
  onCancel,
  onComplete,
  onOpen,
  canCancel = () => false,
  canComplete = () => false,
  canOpen = () => false,
  showStatus = false,
  showReason = false,
  showNote = false,
  showUrgent = false,
  showMoved = false,
  showVia = false,
  showIntake = false,
  error,
}) {
  return (
    <section className="card">
      <h2 className="section-title">{title}</h2>

      {appointments.length === 0 ? (
        <p className="muted">{empty}</p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Date</th>
                <th>Time</th>
                <th>Patient</th>
                <th>Doctor</th>
                <th>Speciality</th>
                <th>Visit</th>
                {showUrgent && <th></th>}
                {showStatus && <th>Status</th>}
                {showReason && <th>Reason</th>}
                {showNote && <th>Note</th>}
                {showMoved && <th>Moved</th>}
                {showVia && <th>Booked via</th>}
                {showIntake && <th>Intake notes</th>}
                <th></th>
              </tr>
            </thead>
            <tbody>
              {appointments.map((a) => (
                <tr key={a.id}>
                  <td className="mono">{a.date}</td>
                  <td className="mono">
                    {a.start_time}–{a.end_time}
                  </td>
                  <td>{a.patient_name}</td>
                  <td>{a.doctor_name}</td>
                  <td>{a.speciality}</td>
                  <td>
                    <span className={`tag tag-${a.visit_type}`}>
                      {a.visit_type === 'new' ? 'new' : 'follow-up'}
                    </span>
                  </td>
                  {showUrgent && (
                    <td>
                      {a.urgent && <span className="tag tag-urgent">urgent</span>}
                    </td>
                  )}
                  {showStatus && (
                    <td>
                      <span className={`tag tag-${a.status}`}>{a.status}</span>
                    </td>
                  )}
                  {showReason && (
                    <td className="reason-cell">{a.reason || '—'}</td>
                  )}
                  {showNote && (
                    <td className="reason-cell">{a.doctor_note || '—'}</td>
                  )}
                  {showMoved && (
                    <td className="reason-cell">{a.move_note || '—'}</td>
                  )}
                  {showVia && (
                    <td>
                      <span className="tag">{a.booked_via || 'Website'}</span>
                    </td>
                  )}
                  {showIntake && (
                    <td className="reason-cell" title={a.intake_notes || ''}>
                      {a.intake_notes ? (
                        a.intake_notes.length > 60
                          ? `${a.intake_notes.slice(0, 60)}…`
                          : a.intake_notes
                      ) : (
                        '—'
                      )}
                    </td>
                  )}
                  <td className="actions">
                    {a.approval_status === 'pending' && (
                      <span className="tag tag-pending">
                        waiting: {a.approval_action}
                      </span>
                    )}
                    {onOpen && canOpen(a) && (
                      <button className="btn btn-small" onClick={() => onOpen(a)}>
                        Summary
                      </button>
                    )}
                    {onCancel && canCancel(a) && (
                      <button className="btn btn-small" onClick={() => onCancel(a)}>
                        Cancel
                      </button>
                    )}
                    {onComplete && canComplete(a) && (
                      <button
                        className="btn btn-small btn-primary"
                        onClick={() => onComplete(a)}
                      >
                        Mark completed
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {error && <div className="alert">{error}</div>}
    </section>
  )
}
