import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import { apiJson } from './api.js'

const SessionContext = createContext(null)

/**
 * One shared session provider with explicit loading/anonymous/authenticated
 * states. A single state owner updates after login/register/logout; headers,
 * navigation and pages consume this context; no page-local auth flags.
 *
 * A rejecting network request during refresh produces a visible recoverable
 * error state, never a fake signed-in user.
 */
export function SessionProvider({ children }) {
  const [state, setState] = useState({ status: 'loading', user: null, error: null })

  const refresh = useCallback(async () => {
    try {
      const payload = await apiJson('GET', '/api/session')
      const user = payload && payload.user ? payload.user : null
      setState({ status: user ? 'authenticated' : 'anonymous', user, error: null })
    } catch (error) {
      const message = error && error.message ? error.message : String(error)
      setState((previous) => ({
        status: 'error',
        user: null,
        error: previous.status === 'loading' ? `session refresh failed: ${message}` : previous.error,
      }))
    }
  }, [])

  const setSession = useCallback((user) => {
    setState({ status: 'authenticated', user, error: null })
  }, [])

  const clear = useCallback(() => {
    setState({ status: 'anonymous', user: null, error: null })
  }, [])

  useEffect(() => {
    refresh()
  }, [refresh])

  const value = useMemo(
    () => ({ ...state, refresh, setSession, clear }),
    [state, refresh, setSession, clear],
  )

  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>
}

export function useSession() {
  const context = useContext(SessionContext)
  if (!context) {
    throw new Error('useSession must be used inside a SessionProvider')
  }
  return context
}
