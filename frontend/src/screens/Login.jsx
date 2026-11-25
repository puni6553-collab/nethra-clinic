import { useEffect, useState } from 'react'
import { api } from '../api.js'
import Logo from '../components/Logo.jsx'

// Password-less login for the three roles.
// Patient: mobile + name (a new mobile creates a new patient), or register
//          a new account first if they have never been here.
// Doctor:  pick your name.  Staff: any name.
export default function Login({ onSignIn, onBack }) {
  const [role, setRole] = useState('patient')
  // 'login' for everyone; 'register' is only offered to patients.
  const [mode, setMode] = useState('login')
  const [doctors, setDoctors] = useState([])
  const [form, setForm] = useState({ mobile: '', name: '', year_of_birth: '', doctor_id: '' })
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    api.doctors().then(setDoctors).catch(() => {})
  }, [])

  function set(field) {
    return (e) => setForm({ ...form, [field]: e.target.value })
  }

  function pickRole(r) {
    setRole(r)
    setMode('login')  // never leave someone stranded on the register form
    setError('')
  }

  async function submit(e) {
    e.preventDefault()
    setError('')
    setBusy(true)
    try {
      let session
      if (role === 'patient' && mode === 'register') {
        session = await api.register({
          mobile: form.mobile,
          name: form.name,
          // Leave it out entirely rather than sending an empty string.
          year_of_birth: form.year_of_birth.trim() ? Number(form.year_of_birth) : null,
        })
      } else {
        const payload =
          role === 'patient'
            ? { role, mobile: form.mobile, name: form.name }
            : role === 'doctor'
              ? { role, doctor_id: form.doctor_id }
              : { role, name: form.name }
        session = await api.login(payload)
      }
      onSignIn(session)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="shell">
      <header className="topbar">
        <Logo />
        <div className="row">
          {onBack && (
            <button className="btn btn-ghost btn-small" onClick={onBack}>
              ← Back
            </button>
          )}
          <span className="muted small">Eye clinic appointments</span>
        </div>
      </header>

      <main className="content narrow">
        <div className="card">
          <h1 className="title">{mode === 'register' ? 'Create your account' : 'Log in'}</h1>
          <p className="muted">
            {mode === 'register'
              ? 'Tell us who you are and we will set you up — you can book straight after.'
              : 'No passwords here — this is a classroom project, not a real clinic system.'}
          </p>

          <div className="tabs" role="tablist">
            {['patient', 'staff', 'doctor'].map((r) => (
              <button
                key={r}
                role="tab"
                aria-selected={role === r}
                className={`tab ${role === r ? 'tab-on' : ''}`}
                onClick={() => pickRole(r)}
              >
                {r}
              </button>
            ))}
          </div>

          <form onSubmit={submit} className="form">
            {role === 'patient' && (
              <>
                <label>
                  Mobile number
                  <input
                    inputMode="numeric"
                    placeholder="9000000001"
                    value={form.mobile}
                    onChange={set('mobile')}
                  />
                </label>
                <label>
                  Full name
                  <input placeholder="Asha Verma" value={form.name} onChange={set('name')} />
                </label>

                {mode === 'register' && (
                  <label>
                    Year of birth <span className="muted small">(optional)</span>
                    <input
                      inputMode="numeric"
                      placeholder="1985"
                      value={form.year_of_birth}
                      onChange={set('year_of_birth')}
                    />
                  </label>
                )}

                {mode === 'login' ? (
                  <p className="hint">
                    Try <b>9000000001</b> with the name <b>Asha Verma</b>.
                  </p>
                ) : (
                  <p className="hint">
                    A mobile number can only be registered once. If you have been here
                    before, just log in instead.
                  </p>
                )}

                <button
                  type="button"
                  className="linkbtn"
                  onClick={() => { setMode(mode === 'register' ? 'login' : 'register'); setError('') }}
                >
                  {mode === 'register'
                    ? 'Already registered? Log in'
                    : 'New patient? Create an account'}
                </button>
              </>
            )}

            {role === 'staff' && (
              <label>
                Your name
                <input placeholder="Reception" value={form.name} onChange={set('name')} />
              </label>
            )}

            {role === 'doctor' && (
              <label>
                Choose your name
                <select value={form.doctor_id} onChange={set('doctor_id')}>
                  <option value="">— select a doctor —</option>
                  {doctors.map((d) => (
                    <option key={d.doctor_id} value={d.doctor_id}>
                      {d.name} · {d.specialities.join(', ')}
                    </option>
                  ))}
                </select>
              </label>
            )}

            {error && <div className="alert">{error}</div>}

            <button className="btn btn-primary" disabled={busy}>
              {busy && <span className="spinner" aria-hidden="true" />}{' '}
              {busy
                ? (mode === 'register' ? 'Creating your account…' : 'Signing in…')
                : (mode === 'register' ? 'Create account and continue' : 'Log in')}
            </button>
          </form>
        </div>
      </main>

      <footer className="foot">
        NetraCare is fictional. All patients and appointments are synthetic.
      </footer>
    </div>
  )
}
