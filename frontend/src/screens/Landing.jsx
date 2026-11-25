import { useEffect, useState } from 'react'
import { api } from '../api.js'
import Logo from '../components/Logo.jsx'
import StarRating from '../components/StarRating.jsx'
import ReviewsModal from '../components/ReviewsModal.jsx'

// What a visitor sees before they ever log in: the poster version of the
// clinic, straight from the public GET /api/clinic (no token, no patient
// data). One job — show what NetraCare is and hand over to the login form.
export default function Landing({ onSignIn }) {
  const [clinic, setClinic] = useState(null)
  const [error, setError] = useState('')
  const [showReviews, setShowReviews] = useState(false)

  useEffect(() => {
    api
      .clinic()
      .then(setClinic)
      .catch((err) => setError(err.message))
  }, [])

  return (
    <div className="shell">
      <header className="topbar">
        <Logo />
        <div className="row">
          <button className="btn btn-ghost btn-small" onClick={onSignIn}>
            Staff &amp; doctor sign in
          </button>
        </div>
      </header>

      <main className="content landing">
        {error && <div className="alert">{error}</div>}

        <section className="hero">
          <p className="hero-kicker">Eye care, booked in minutes</p>
          <h1 className="hero-title">{clinic ? clinic.name : 'NetraCare'}</h1>
          <p className="hero-tagline">
            {clinic ? clinic.tagline : 'A calm, careful eye clinic — with every rule checked before you book.'}
          </p>
          <p className="hero-intro">{clinic && clinic.intro}</p>

          <button
            className="hero-rating"
            onClick={() => setShowReviews(true)}
            aria-label="Read patient reviews"
            title="Read patient reviews"
          >
            <StarRating score={4.5} />
            <span className="hero-rating-num">4.5</span>
            <span className="muted small">patient rating · see reviews</span>
          </button>

          <div className="hero-actions">
            <button className="btn btn-primary btn-big" onClick={onSignIn}>
              Book an appointment
            </button>
            <a className="btn btn-big" href="#hours">
              Opening hours
            </a>
          </div>

          {clinic && (
            <div className="hero-facts">
              <div className="hero-fact">
                <span className="hero-fact-num">{clinic.doctors.length}</span>
                <span className="hero-fact-label">Doctors</span>
              </div>
              <div className="hero-fact">
                <span className="hero-fact-num">{clinic.visit_length.new}′</span>
                <span className="hero-fact-label">New visit</span>
              </div>
              <div className="hero-fact">
                <span className="hero-fact-num">{clinic.visit_length.follow_up}′</span>
                <span className="hero-fact-label">Follow-up</span>
              </div>
            </div>
          )}
        </section>

        {!clinic && !error && (
          <p className="muted center">
            <span className="spinner" aria-hidden="true" /> Loading clinic information…
          </p>
        )}

        {clinic && (
          <>
            <section className="card landing-card">
              <h2 className="section-title">Our doctors</h2>
              <div className="doc-grid">
                {clinic.doctors.map((d) => (
                  <div className="doc-tile" key={d.doctor_id}>
                    <span className="doc-avatar" aria-hidden="true">
                      {d.name.replace(/^Dr\.?\s*/, '').charAt(0)}
                    </span>
                    <div>
                      <div className="doc-name">{d.name}</div>
                      <div className="muted small">{d.specialities.join(' · ')}</div>
                      <button
                        className="doc-rating"
                        onClick={() => setShowReviews(true)}
                        aria-label={`Read reviews, ${d.name}`}
                        title="Read patient reviews"
                      >
                        <StarRating score={4.5} />
                        <span className="small">4.5</span>
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            </section>

            <div className="landing-cols">
              <section className="card landing-card" id="hours">
                <h2 className="section-title">Opening hours</h2>
                <ul className="hours-list">
                  {clinic.hours.map((h) => (
                    <li key={h.day} className={h.closed ? 'closed' : ''}>
                      <span>{h.day}</span>
                      <span className="mono">{h.hours}</span>
                    </li>
                  ))}
                </ul>
              </section>

              <section className="card landing-card">
                <h2 className="section-title">Visit us</h2>
                <div className="notes">
                  <div className="note">{clinic.address}</div>
                  <div className="note">{clinic.email}</div>
                  <div className="note">Consultations: {clinic.sessions}</div>
                  <div className="note">
                    Visit length — new {clinic.visit_length.new} min · follow-up{' '}
                    {clinic.visit_length.follow_up} min
                  </div>
                </div>
                <div className="hero-actions">
                  <button className="btn btn-primary" onClick={onSignIn}>
                    Book an appointment
                  </button>
                </div>
              </section>
            </div>
          </>
        )}
      </main>

      <footer className="foot">
        NetraCare is fictional. All patients and appointments are synthetic.
      </footer>

      <ReviewsModal open={showReviews} onClose={() => setShowReviews(false)} />
    </div>
  )
}
