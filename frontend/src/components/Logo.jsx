// Eye logo + clinic name, used in the top bar on every screen.
export default function Logo() {
  return (
    <div className="brand">
      <svg className="brand-eye" viewBox="0 0 48 48" aria-hidden="true">
        <path
          d="M2 24C10 12 18 8 24 8s14 4 22 16c-8 12-16 16-22 16S10 36 2 24Z"
          fill="none"
          stroke="currentColor"
          strokeWidth="3"
          strokeLinejoin="round"
        />
        <circle cx="24" cy="24" r="8" fill="currentColor" />
        <circle cx="27" cy="21" r="2.5" fill="#fff" />
      </svg>
      <span className="brand-name">
        NetraCare <span className="brand-sub">Eye Clinic</span>
      </span>
    </div>
  )
}
