<script setup lang="ts">
import { ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'

const activeTab = ref<'login' | 'register'>('login')
const router = useRouter()

function enterDemoWorkspace() {
  void router.push({ name: 'app-generate' })
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
          <form class="auth-form" @submit.prevent="enterDemoWorkspace">
            <div class="field">
              <label for="loginEmail" style="font-weight: bold;">邮箱：</label>
              <input id="loginEmail" type="email" autocomplete="email" placeholder="you@example.com">
            </div>
            <div class="field">
              <label for="loginPassword" style="font-weight: bold;">密码：</label>
              <input id="loginPassword" type="password" autocomplete="current-password" placeholder="至少 8 位">
            </div>
            <div class="auth-actions">
              <button class="btn btn-primary btn-lg btn-block" type="submit">登录</button>
              <div class="auth-alt">
                <button type="button">使用验证码登录</button>
                <a href="#" @click.prevent>忘记密码</a>
              </div>
            </div>
            <p class="auth-note">演示模式：提交后进入工作区，暂不校验账号。</p>
          </form>
        </section>

        <section v-else role="tabpanel">
          <h2>创建账号</h2>
          <p class="sub">快来加入我们一起学习吧</p>
          <form class="auth-form" @submit.prevent="enterDemoWorkspace">
            
            <div class="field">
              <label for="regEmail" style="font-weight: bold;">邮箱：</label>
              <input id="regEmail" type="email" placeholder="you@example.com" autocomplete="email">
            </div>
            <div class="field">
              <label for="regPassword" style="font-weight: bold;">密码：</label>
              <input id="regPassword" type="password" placeholder="至少 8 位，含数字" autocomplete="new-password">
              <p class="field-hint">强度：弱 · 中 · 强</p>
            </div>
            <div class="field">
              <label for="regCode" style="font-weight: bold;">邮箱验证码：</label>
              <div class="auth-row">
                <input id="regCode" inputmode="numeric" placeholder="6 位数字">
                <button class="btn" type="button">发送验证码</button>
              </div>
            </div>
            
            <label class="switch" style="margin-top: 20px">
              <input type="checkbox" checked>
              <span class="track" aria-hidden="true"></span>
              <span>我已阅读并同意《服务条款》与《隐私政策》</span>
            </label>
            <div class="auth-actions">
              <button class="btn btn-primary btn-lg btn-block" type="submit">注册</button>
            </div>
            <p class="auth-note">演示模式：提交后进入工作区，暂不创建真实账号。</p>
          </form>
        </section>
      </div>
    </section>
  </div>
</template>
