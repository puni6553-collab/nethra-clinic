// Left navigation: Home returns to the dashboard, About Us opens the clinic page.
export default function Sidebar({ view, onNavigate, me, onSignOut }) {
  const items = [
    { id: 'home', label: 'Home' },
    { id: 'about', label: 'About Us' },
  ]

  return (
    <aside className="sidebar">
      <div className="sidebar-brand">
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

      <nav className="side-nav" aria-label="Main">
        {items.map((item) => (
          <button
            key={item.id}
            className={`side-link ${view === item.id ? 'side-on' : ''}`}
            onClick={() => onNavigate(item.id)}
          >
            {item.id === 'home' && (
              <svg viewBox="0 0 24 24" aria-hidden="true" className="side-icon">
                <path
                  d="M4 11.5 12 4l8 7.5V20a1 1 0 0 1-1 1h-5v-6h-4v6H5a1 1 0 0 1-1-1v-8.5Z"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.8"
                  strokeLinejoin="round"
                />
              </svg>
            )}
            {item.id === 'about' && (
              <svg viewBox="0 0 24 24" aria-hidden="true" className="side-icon">
                <circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" strokeWidth="1.8" />
                <path d="M12 11v6" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
                <circle cx="12" cy="7.5" r="1.1" fill="currentColor" />
              </svg>
            )}
            {item.label}
          </button>
        ))}
      </nav>

      <div className="side-foot">
        <div className="who side-who">
          <span className={`pill pill-${me.role}`}>{me.role}</span>
          <span className="who-name">{me.name}</span>
        </div>
        <button className="btn btn-ghost side-out" onClick={onSignOut}>
          Log out
        </button>
      </div>
    </aside>
  )
}
