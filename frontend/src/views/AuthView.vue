<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { loginAccount, registerAccount, sendRegistrationEmailCode } from '../api/accounts'
import type { ApiResponse } from '../api/accounts'

const activeTab = ref<'login' | 'register'>('login')
const router = useRouter()
const route = useRoute()
const loginEmail = ref('')
const loginPassword = ref('')
const registerEmail = ref('')
const registerPassword = ref('')
const registerPasswordConfirm = ref('')
const verificationCode = ref('')
const acceptedTerms = ref(false)
const isSubmitting = ref(false)
const formMessage = ref('')
const formErrors = ref<Record<string, string[]>>({})
const registerEmailInput = ref<HTMLInputElement | null>(null)
const isSendingCode = ref(false)
const codeMessage = ref('')
const codeMessageType = ref<'success' | 'error'>('success')
const resendSeconds = ref(0)
const lastCodeEmail = ref('')
let countdownTimer: number | undefined

const isCoolingDown = computed(() =>
  resendSeconds.value > 0 &&
  registerEmail.value.trim().toLowerCase() === lastCodeEmail.value,
)

watch(registerEmail, () => { codeMessage.value = '' })
watch(activeTab, () => { formMessage.value = ''; formErrors.value = {} })
onUnmounted(() => { if (countdownTimer !== undefined) window.clearInterval(countdownTimer) })

function startCountdown(seconds: number, email: string) {
  if (countdownTimer !== undefined) window.clearInterval(countdownTimer)
  lastCodeEmail.value = email.toLowerCase()
  resendSeconds.value = Math.max(0, Math.ceil(seconds))
  if (resendSeconds.value === 0) return

  countdownTimer = window.setInterval(() => {
    resendSeconds.value -= 1
    if (resendSeconds.value <= 0 && countdownTimer !== undefined) {
      window.clearInterval(countdownTimer)
      countdownTimer = undefined
    }
  }, 1000)
}

async function requestRegistrationCode() {
  if (!registerEmailInput.value?.reportValidity() || isSendingCode.value || isCoolingDown.value) return

  const email = registerEmail.value.trim()
  isSendingCode.value = true
  codeMessage.value = ''

  try {
    const result = await sendRegistrationEmailCode(email)
    if (result.code === 200 && result.data) {
      startCountdown(result.data.retry_after, email)
      codeMessageType.value = 'success'
      codeMessage.value = result.message
    } else {
      if (result.code === 429 && result.data?.retry_after) {
        startCountdown(result.data.retry_after, email)
      }
      codeMessageType.value = 'error'
      codeMessage.value = result.errors?.email?.[0] ?? result.message
    }
  } catch (error) {
    codeMessageType.value = 'error'
    codeMessage.value = error instanceof Error ? error.message : '发送失败，请稍后重试。'
  } finally {
    isSendingCode.value = false
  }
}

function showApiError(result: ApiResponse<unknown>) {
  formMessage.value = result.message
  formErrors.value = result.errors ?? {}
}

function enterWorkspace() {
  const redirect = route.query.redirect
  void router.replace(typeof redirect === 'string' && redirect.startsWith('/app')
    ? redirect
    : { name: 'app-generate' })
}

async function submitLogin() {
  if (isSubmitting.value) return
  isSubmitting.value = true
  formMessage.value = ''
  formErrors.value = {}
  try {
    const result = await loginAccount(loginEmail.value.trim(), loginPassword.value)
    if (result.code === 200) enterWorkspace()
    else showApiError(result)
  } catch (error) {
    formMessage.value = error instanceof Error ? error.message : '登录失败，请稍后重试。'
  } finally {
    isSubmitting.value = false
  }
}

async function submitRegistration() {
  if (isSubmitting.value) return
  isSubmitting.value = true
  formMessage.value = ''
  formErrors.value = {}
  try {
    const result = await registerAccount({
      email: registerEmail.value.trim(),
      password: registerPassword.value,
      password_confirm: registerPasswordConfirm.value,
      verification_code: verificationCode.value,
      accepted_terms: acceptedTerms.value,
    })
    if (result.code === 201) enterWorkspace()
    else showApiError(result)
  } catch (error) {
    formMessage.value = error instanceof Error ? error.message : '注册失败，请稍后重试。'
  } finally {
    isSubmitting.value = false
  }
}
</script>

<template>
  <div class="shell-simple">
    <section class="auth-side">
      <RouterLink class="brand" to="/">
        <span class="brand-mark" aria-hidden="true">
          <svg class="icon icon-lg" viewBox="0 0 24 24">
            <rect x="5.5" y="3.5" width="13" height="11" rx="4.5" />
            <path d="M9 8.5h.01M15 8.5h.01M9.5 17.5h5M12 14.5v3M4 18.5h16" />
          </svg>
        </span>
        <span class="brand-name">智塾<small>AI TEACHING PLATFORM</small></span>
      </RouterLink>

      <div class="auth-headline">
        <h1>回到你的课堂<br>专属学习团队已经就位</h1>
        <p>和智能教师对话，打造你的个性化课堂。完整的教学周期，配套习题，学情跟踪，错题归纳</p>
        <div class="auth-facts">
          <div class="auth-fact"><span class="k">T</span>教师 · 大纲与任务</div>
          <div class="auth-fact"><span class="k">A</span>助教 · 批改与学情报告</div>
          <div class="auth-fact"><span class="k">Q</span>答疑 · 错题库与同类型题</div>
          <div class="auth-fact"><span class="k">C</span>督学 · 复习计划与简报</div>
        </div>
      </div>

      <div class="auth-mini" aria-hidden="true">
        <img
          src="/prototype-assets/robot-auth.png"
          alt=""
          width="359"
          height="573"
          decoding="async"
        >
      </div>
      <div class="auth-foot">智塾 · MMXXVI</div>
    </section>

    <section class="auth-main">
      <div class="auth-card">
        <div class="tabs" role="tablist" aria-label="登录或注册">
          <button
            class="tab"
            type="button"
            role="tab"
            :aria-selected="activeTab === 'login'"
            @click="activeTab = 'login'"
          >登录</button>
          <button
            class="tab"
            type="button"
            role="tab"
            :aria-selected="activeTab === 'register'"
            @click="activeTab = 'register'"
          >注册</button>
        </div>

        <section v-if="activeTab === 'login'" role="tabpanel">
          <h2>欢迎回来</h2>
          <p class="sub">用邮箱继续你的学习计划</p>
          <form class="auth-form" @submit.prevent="submitLogin">
            <div class="field">
              <label for="loginEmail" style="font-weight: bold;">邮箱：</label>
              <input id="loginEmail" v-model="loginEmail" type="email" autocomplete="email" placeholder="you@example.com" required>
              <p v-if="formErrors.email?.[0]" class="auth-form-error">{{ formErrors.email[0] }}</p>
            </div>
            <div class="field">
              <label for="loginPassword" style="font-weight: bold;">密码：</label>
              <input id="loginPassword" v-model="loginPassword" type="password" autocomplete="current-password" placeholder="至少 8 位" required>
              <p v-if="formErrors.password?.[0]" class="auth-form-error">{{ formErrors.password[0] }}</p>
            </div>
            <div class="auth-actions">
              <button class="btn btn-primary btn-lg btn-block" type="submit" :disabled="isSubmitting">{{ isSubmitting ? '登录中…' : '登录' }}</button>
            </div>
            <p v-if="formMessage" class="auth-form-error" role="alert">{{ formMessage }}</p>
          </form>
        </section>

        <section v-else role="tabpanel">
          <h2>创建账号</h2>
          <p class="sub">快来加入我们一起学习吧</p>
          <form class="auth-form" @submit.prevent="submitRegistration">
            
            <div class="field">
              <label for="regEmail" style="font-weight: bold;">邮箱：</label>
              <input id="regEmail" ref="registerEmailInput" v-model="registerEmail" type="email" placeholder="you@example.com" autocomplete="email" :disabled="isSendingCode" required>
              <p v-if="formErrors.email?.[0]" class="auth-form-error">{{ formErrors.email[0] }}</p>
            </div>
            <div class="field">
              <label for="regPassword" style="font-weight: bold;">密码：</label>
              <input id="regPassword" v-model="registerPassword" type="password" placeholder="至少 8 位，含数字" autocomplete="new-password" minlength="8" required>
              <p v-if="formErrors.password?.[0]" class="auth-form-error">{{ formErrors.password[0] }}</p>
              <p class="field-hint">至少 8 位，并包含数字</p>
            </div>
            <div class="field">
              <label for="regPasswordConfirm" style="font-weight: bold;">确认密码：</label>
              <input id="regPasswordConfirm" v-model="registerPasswordConfirm" type="password" placeholder="再次输入密码" autocomplete="new-password" required>
              <p v-if="formErrors.password_confirm?.[0]" class="auth-form-error">{{ formErrors.password_confirm[0] }}</p>
            </div>
            <div class="field">
              <label for="regCode" style="font-weight: bold;">邮箱验证码：</label>
              <div class="auth-row">
                <input id="regCode" v-model="verificationCode" inputmode="numeric" pattern="[0-9]{6}" maxlength="6" placeholder="6 位数字" required>
                <button class="btn" type="button" :disabled="isSendingCode || isCoolingDown" @click="requestRegistrationCode">
                  {{ isSendingCode ? '发送中…' : isCoolingDown ? `${resendSeconds}s 后重发` : '发送验证码' }}
                </button>
              </div>
              <p v-if="codeMessage" class="auth-code-feedback" :data-kind="codeMessageType" :role="codeMessageType === 'error' ? 'alert' : 'status'">{{ codeMessage }}</p>
              <p v-if="formErrors.verification_code?.[0]" class="auth-form-error">{{ formErrors.verification_code[0] }}</p>
            </div>
            
            <label class="switch" style="margin-top: 20px">
              <input v-model="acceptedTerms" type="checkbox" required>
              <span class="track" aria-hidden="true"></span>
              <span>我已阅读并同意《服务条款》与《隐私政策》</span>
            </label>
            <p v-if="formErrors.accepted_terms?.[0]" class="auth-form-error">{{ formErrors.accepted_terms[0] }}</p>
            <div class="auth-actions">
              <button class="btn btn-primary btn-lg btn-block" type="submit" :disabled="isSubmitting">{{ isSubmitting ? '注册中…' : '注册' }}</button>
            </div>
            <p v-if="formMessage" class="auth-form-error" role="alert">{{ formMessage }}</p>
          </form>
        </section>
      </div>
    </section>
  </div>
</template>
