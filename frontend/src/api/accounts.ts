import { shallowRef } from 'vue'

export interface ApiResponse<T> {
  code: number
  message: string
  data: T | null
  errors: Record<string, string[]> | null
}

export interface User {
  id: number
  email: string
}

interface TokenPair {
  access: string
  refresh: string
  user: User
}

export interface EmailCodeResult {
  code_valid_for: number
  retry_after: number
}

export interface RegistrationDetails {
  email: string
  password: string
  password_confirm: string
  verification_code: string
  accepted_terms: boolean
}

const ACCESS_KEY = 'zhishu_access'
const REFRESH_KEY = 'zhishu_refresh'
export const currentUser = shallowRef<User | null>(null)
let refreshInFlight: Promise<boolean> | null = null

function accessToken() { return sessionStorage.getItem(ACCESS_KEY) }
function refreshToken() { return sessionStorage.getItem(REFRESH_KEY) }

export function hasSession() {
  return Boolean(accessToken() && refreshToken())
}

export function clearSession() {
  sessionStorage.removeItem(ACCESS_KEY)
  sessionStorage.removeItem(REFRESH_KEY)
  currentUser.value = null
}

function saveSession(data: TokenPair) {
  sessionStorage.setItem(ACCESS_KEY, data.access)
  sessionStorage.setItem(REFRESH_KEY, data.refresh)
  currentUser.value = data.user
}

async function request<T>(path: string, method: 'GET' | 'POST', body?: unknown, access?: string): Promise<ApiResponse<T>> {
  let response: Response
  try {
    response = await fetch(`/api/accounts/${path}`, {
      method,
      headers: {
        ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
        ...(access ? { Authorization: `Bearer ${access}` } : {}),
      },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    })
  } catch {
    throw new Error('无法连接后端服务，请检查 Django 是否已启动。')
  }

  if (!response.headers.get('content-type')?.includes('application/json')) {
    throw new Error('无法连接后端服务，请稍后重试。')
  }

  const result = await response.json() as ApiResponse<T>
  if (result.code !== response.status || typeof result.message !== 'string') {
    throw new Error('服务器响应格式异常，请稍后重试。')
  }
  return result
}

export function sendRegistrationEmailCode(email: string) {
  return request<EmailCodeResult>('email-codes/', 'POST', { email, purpose: 'register' })
}

export async function registerAccount(details: RegistrationDetails) {
  const result = await request<TokenPair>('register/', 'POST', details)
  if (result.code === 201 && result.data) saveSession(result.data)
  return result
}

export async function loginAccount(email: string, password: string) {
  const result = await request<TokenPair>('login/', 'POST', { email, password })
  if (result.code === 200 && result.data) saveSession(result.data)
  return result
}

async function refreshSession() {
  const token = refreshToken()
  if (!token) return false
  const result = await request<{ access: string }>('token/refresh/', 'POST', { refresh: token })
  if (result.code !== 200 || !result.data?.access) {
    clearSession()
    return false
  }
  sessionStorage.setItem(ACCESS_KEY, result.data.access)
  return true
}

export function refreshAccessToken() {
  if (!refreshInFlight) {
    refreshInFlight = refreshSession().finally(() => { refreshInFlight = null })
  }
  return refreshInFlight
}

export async function loadCurrentUser(): Promise<User | null> {
  if (!hasSession()) {
    clearSession()
    return null
  }
  if (currentUser.value) return currentUser.value

  let result = await request<User>('me/', 'GET', undefined, accessToken() ?? undefined)
  if (result.code === 401 && await refreshAccessToken()) {
    result = await request<User>('me/', 'GET', undefined, accessToken() ?? undefined)
  }
  if (result.code !== 200 || !result.data) {
    clearSession()
    return null
  }
  currentUser.value = result.data
  return result.data
}

export async function logoutAccount() {
  const token = refreshToken()
  try {
    if (token) await request<null>('logout/', 'POST', { refresh: token })
  } finally {
    clearSession()
  }
}
