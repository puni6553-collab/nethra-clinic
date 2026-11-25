import { useEffect, useState } from 'react'
import { api, getToken, setToken } from './api.js'
import Login from './screens/Login.jsx'
import Landing from './screens/Landing.jsx'
import PatientHome from './screens/PatientHome.jsx'
import StaffHome from './screens/StaffHome.jsx'
import DoctorHome from './screens/DoctorHome.jsx'
import Dashboard from './screens/Dashboard.jsx'
import About from './screens/About.jsx'
import Sidebar from './components/Sidebar.jsx'

const THEME_KEY = 'netracare.theme'

// Dark only: light mode was removed, so the theme is always applied as dark
// (any saved 'light' preference in localStorage is overwritten).
function applyTheme() {
  document.documentElement.setAttribute('data-theme', 'dark')
}

export default function App() {
  const [me, setMe] = useState(null)
  const [booting, setBooting] = useState(true)
  const [view, setView] = useState('home') // 'home' | 'about'
  // Signed out: 'landing' (the poster page) or 'login' (the form).
  const [signedOutView, setSignedOutView] = useState('landing')

  useEffect(() => {
    applyTheme()
    localStorage.setItem(THEME_KEY, 'dark')
  }, [])

  // If we still hold a token, restore the session on reload.
  useEffect(() => {
    if (!getToken()) {
      setBooting(false)
      return
    }
    api
      .me()
      .then(setMe)
      .catch(() => setToken(null))
      .finally(() => setBooting(false))
  }, [])

  function signIn(session) {
    setToken(session.token)
    setMe(session)
    setView('home')
  }

  function signOut() {
    setToken(null)
    setMe(null)
    setView('home')
    setSignedOutView('landing')
  }

  if (booting) return <div className="center muted"><span className="spinner" aria-hidden="true" /> Loading…</div>

  if (!me) {
    return signedOutView === 'login' ? (
      <Login onSignIn={signIn} onBack={() => setSignedOutView('landing')} />
    ) : (
      <Landing onSignIn={() => setSignedOutView('login')} />
    )
  }

  return (
    <div className="shell">
      <a className="skip-link" href="#main-content">Skip to content</a>
      <div className="layout">
        <Sidebar view={view} onNavigate={setView} me={me} onSignOut={signOut} />

        <div className="main-col">
          <main className="content" id="main-content">
            {view === 'about' ? (
              <About />
            ) : (
              <div className="stack">
                <Dashboard me={me} />
                {me.role === 'patient' && <PatientHome me={me} />}
                {me.role === 'staff' && <StaffHome me={me} />}
                {me.role === 'doctor' && <DoctorHome me={me} />}
              </div>
            )}
          </main>

          <footer className="foot">
            NetraCare is fictional. All patients and appointments are synthetic.
          </footer>
        </div>
      </div>
    </div>
  )
}
