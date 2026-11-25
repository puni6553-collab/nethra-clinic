// One small place that talks to the FastAPI backend.
//
// Every response that is a refusal comes back as
//   { detail: { code: "...", message: "..." } }
// and we normalise that into an Error so screens can just show
// `err.message` in plain words.

const TOKEN_KEY = 'netracare.token'

export function getToken() {
  return localStorage.getItem(TOKEN_KEY)
}

export function setToken(token) {
  if (token) localStorage.setItem(TOKEN_KEY, token)
  else localStorage.removeItem(TOKEN_KEY)
}

async function request(path, { method = 'GET', body, token = getToken() } = {}) {
  const headers = { 'Content-Type': 'application/json' }
  if (token) headers.Authorization = `Bearer ${token}`

  const res = await fetch(path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  })

  let data = null
  try {
    data = await res.json()
  } catch {
    data = null
  }

  if (!res.ok) {
    const detail = data && data.detail
    const err = new Error(
      (detail && detail.message) || `Request failed (${res.status})`,
    )
    err.code = detail && detail.code
    err.status = res.status
    err.data = data
    throw err
  }
  return data
}

export const api = {
  clinic: () => request('/api/clinic', { token: null }),
  login: (payload) => request('/api/login', { method: 'POST', body: payload, token: null }),
  register: (payload) => request('/api/register', { method: 'POST', body: payload, token: null }),
  me: () => request('/api/me'),
  doctors: () => request('/api/doctors', { token: null }),
  patients: () => request('/api/patients'),
  slots: ({ doctor_id, date, visit_type }) =>
    request(
      `/api/slots?doctor_id=${encodeURIComponent(doctor_id)}` +
        `&date=${encodeURIComponent(date)}&visit_type=${encodeURIComponent(visit_type)}`,
    ),
  appointments: (filters = {}) => {
    const qs = new URLSearchParams()
    for (const [k, v] of Object.entries(filters)) if (v) qs.set(k, v)
    const suffix = qs.toString() ? `?${qs}` : ''
    return request(`/api/appointments${suffix}`)
  },
  book: (payload) => request('/api/appointments', { method: 'POST', body: payload }),
  cancel: (id) => request(`/api/appointments/${id}/cancel`, { method: 'POST', body: {} }),
  complete: (id, note) =>
    request(`/api/appointments/${id}/complete`, { method: 'POST', body: { note } }),

  // Iteration 2
  fact: (previous) =>
    request(
      '/api/fact-of-the-day' +
        (previous ? `?previous=${encodeURIComponent(previous)}` : ''),
    ),
  urgentCases: () => request('/api/urgent-cases'),
  markSeen: (id) => request(`/api/urgent-cases/${id}/seen`, { method: 'POST', body: {} }),
  summary: (patientId, appointmentId) =>
    request(
      `/api/doctors/patients/${patientId}/summary` +
        (appointmentId ? `?appointment_id=${appointmentId}` : ''),
    ),

  // Iteration 3 + 4 (the agent): {thread_id, message} or {thread_id, confirm}.
  chat: (payload) => request('/api/chat', { method: 'POST', body: payload }),
  searchDebug: (q) => request(`/api/staff/search-debug?q=${encodeURIComponent(q)}`),

  // Iteration 4
  reschedule: (id, payload) =>
    request(`/api/appointments/${id}/reschedule`, { method: 'POST', body: payload }),
  staffApprovals: () => request('/api/staff/approvals'),
  approve: (id) =>
    request(`/api/staff/approvals/${id}/approve`, { method: 'POST', body: {} }),
  reject: (id) =>
    request(`/api/staff/approvals/${id}/reject`, { method: 'POST', body: {} }),

  // Iteration 4 (R7): the doctor marks leave, Staff decide the rebookings.
  doctorLeave: (payload) => request('/api/doctor/leave', { method: 'POST', body: payload }),
  myLeave: () => request('/api/doctor/leave'),
  staffRebookings: () => request('/api/staff/rebookings'),
  approveRebooking: (id) =>
    request(`/api/staff/rebookings/${id}/approve`, { method: 'POST', body: {} }),
  rejectRebooking: (id) =>
    request(`/api/staff/rebookings/${id}/reject`, { method: 'POST', body: {} }),
}
