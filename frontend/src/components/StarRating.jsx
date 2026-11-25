// 4.5-star rating mark: five stars with the fill clipped to 90% (4.5/5).
// Pure CSS overlay, so no SVG gradient ids to collide across instances.
export default function StarRating({ score = 4.5, className = '' }) {
  const pct = Math.max(0, Math.min(100, (score / 5) * 100))
  return (
    <span
      className={`star-rating ${className}`}
      role="img"
      aria-label={`${score} out of 5 stars`}
    >
      <span className="star-rating-bg" aria-hidden="true">★★★★★</span>
      <span className="star-rating-fill" style={{ width: `${pct}%` }} aria-hidden="true">
        ★★★★★
      </span>
    </span>
  )
}
