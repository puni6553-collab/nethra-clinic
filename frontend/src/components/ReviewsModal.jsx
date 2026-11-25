import { useEffect } from 'react'
import StarRating from './StarRating.jsx'

// Synthetic patient reviews — the app ships no real patient data.
const REVIEWS = [
  {
    name: 'Ananya R.',
    score: 5,
    date: 'Nov 2026',
    text: 'Booked online in under a minute and the slot was exactly as shown. Dr. Mehta explained every step of the dilated exam without rushing.',
  },
  {
    name: 'Vikram S.',
    score: 4.5,
    date: 'Oct 2026',
    text: 'Front desk was polite and I was seen within ten minutes of my appointment time. The follow-up reminder by message was a nice touch.',
  },
  {
    name: 'Priya K.',
    score: 5,
    date: 'Oct 2026',
    text: 'I asked the assistant to move my cataract follow-up and it found a new slot right away — I only had to press Confirm. Very smooth.',
  },
  {
    name: 'Rohit M.',
    score: 4,
    date: 'Sep 2026',
    text: 'Good consultation and clear prescription notes. Parking was a little tight in the morning, but the clinic itself ran on time.',
  },
  {
    name: 'Sneha D.',
    score: 5,
    date: 'Sep 2026',
    text: 'The team caught a red-flag symptom in my message and called me the same day. Grateful for how seriously they took it.',
  },
]

// Clicking the rating opens this overlay: the overall score plus the review
// messages behind it.
export default function ReviewsModal({ open, onClose }) {
  useEffect(() => {
    if (!open) return undefined
    function onKey(e) {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [open, onClose])

  if (!open) return null

  const list = REVIEWS

  return (
    <div className="modal-backdrop" onClick={onClose} role="presentation">
      <div
        className="modal-card reviews-modal"
        role="dialog"
        aria-modal="true"
        aria-label="Patient reviews"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="fact-head">
          <h2 className="section-title" style={{ marginBottom: 0 }}>
            Patient reviews
          </h2>
          <button className="btn btn-small" onClick={onClose} aria-label="Close reviews">
            ✕
          </button>
        </div>

        <div className="reviews-summary">
          <StarRating score={4.5} />
          <span className="hero-rating-num">4.5</span>
          <span className="muted small">{list.length} reviews</span>
        </div>

        <div className="reviews-list" role="log" aria-label="Review messages">
          {list.map((r, i) => (
            <article className="review-item" key={i}>
              <div className="review-head">
                <span className="review-name">{r.name}</span>
                <StarRating score={r.score} className="review-stars" />
                <span className="muted small">{r.date}</span>
              </div>
              <p className="review-text">{r.text}</p>
            </article>
          ))}
        </div>
      </div>
    </div>
  )
}
