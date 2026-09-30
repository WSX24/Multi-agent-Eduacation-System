<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref } from 'vue'
import { useRouter } from 'vue-router'
import { saveDemoCourse, type DemoCourse, type DemoUnit } from '../data/demoCourses'

type Message = {
  id: number
  role: 'assistant' | 'user'
  note?: string
  text: string
  outline?: DemoUnit[]
  files?: string[]
}

const router = useRouter()
const chatLog = ref<HTMLElement | null>(null)
const draft = ref('')
const isBusy = ref(false)
const showSettings = ref(false)
const generatedCourse = ref<DemoCourse | null>(null)
const isSaved = ref(false)
const selectedModel = ref('智塾 3 Pro')
const selectedCycle = ref<'short' | 'long'>('long')
const selectedLevel = ref<'beginner' | 'intermediate'>('beginner')
const messages = ref<Message[]>([])

const models = [
  { name: '智塾 3 Pro', description: '适合课程规划' },
  { name: '通用推理模型', description: '适合复杂主题' },
  { name: '轻量模型', description: '响应更快' },
]

let nextMessageId = 1
let timers: number[] = []
let disposed = false

function setTimer(callback: () => void, delay: number) {
  const timer = window.setTimeout(() => {
    timers = timers.filter((pending) => pending !== timer)
    if (!disposed) callback()
  }, delay)
  timers.push(timer)
}

function scrollChatToBottom() {
  void nextTick(() => {
    if (chatLog.value) chatLog.value.scrollTop = chatLog.value.scrollHeight
  })
}

function makeCourse(goal: string): DemoCourse {
  const firstClause = goal.split(/[，。；;、]/)[0]?.trim() || goal.trim()
  const title = firstClause.length > 26 ? `${firstClause.slice(0, 26)}…` : firstClause
  const topic = title.replace(/^(我想(要)?|我希望|请帮我|帮我|想要|想学)/, '').trim() || title

  const unitTitles = selectedCycle.value === 'short'
    ? [
        `${topic}：基础概念与核心方法`,
        `${topic}：典型案例与专项练习`,
        `${topic}：综合复习与阶段测验`,
      ]
    : [
        `${topic}：基础概念与学习准备`,
        `${topic}：核心方法与关键知识`,
        `${topic}：典型案例与专项练习`,
        `${topic}：综合复习与阶段测验`,
      ]

  return {
    id: `demo-${Date.now()}`,
    title: topic,
    goal,
    overview: `围绕「${topic}」的核心知识循序规划，从基础概念逐步过渡到应用练习，帮助你建立系统的知识框架。`,
    units: unitTitles.map((unitTitle): DemoUnit => ({
      title: unitTitle,
      lessons: ['核心概念与基本原理', '典型例题与解题方法', '知识应用与巩固练习'],
    })),
    completedLessons: [],
    createdAt: new Date().toLocaleDateString('zh-CN'),
    model: selectedModel.value,
    cycle: selectedCycle.value,
    level: selectedLevel.value,
  }
}

function submitGoal(value = draft.value) {
  const goal = value.trim()
  if (!goal || isBusy.value) return

  draft.value = ''
  isBusy.value = true
  isSaved.value = false
  generatedCourse.value = null
  messages.value.push({ id: nextMessageId++, role: 'user', text: goal })
  scrollChatToBottom()

  setTimer(() => {
    const course = makeCourse(goal)
    generatedCourse.value = course
    messages.value.push({
      id: nextMessageId++,
      role: 'assistant',
      note: '教师',
      text: `我先根据你的目标整理了「${course.title}」的课程大纲，接下来继续准备配套材料。`,
      outline: course.units,
    })
    scrollChatToBottom()

    setTimer(() => {
      isBusy.value = false
      messages.value.push({
        id: nextMessageId++,
        role: 'assistant',
        note: '课程材料已准备好',
        text: '课程大纲和配套材料已经整理完成。你可以先保存课程，后续再继续完善讲义和课堂脚本。',
        files: ['课程大纲.md', '课程课件.pptx', '课件说明.md'],
      })
      scrollChatToBottom()
    }, 1250)
  }, 650)
}

function clearConversation() {
  if (isBusy.value) return
  messages.value = []
  generatedCourse.value = null
  isSaved.value = false
  showSettings.value = false
  draft.value = ''
  nextMessageId = 1
}

function saveCourse() {
  if (!generatedCourse.value || isSaved.value || isBusy.value) return
  saveDemoCourse(generatedCourse.value)
  isSaved.value = true
  void router.push({ name: 'app-courses' })
}

onBeforeUnmount(() => {
  disposed = true
  timers.forEach((timer) => window.clearTimeout(timer))
})
</script>

<template>
  <section class="generate-workspace" aria-label="生成课堂">
    <header class="conversation-header">
      <button class="new-chat-button" type="button" :disabled="isBusy" @click="clearConversation">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14" /></svg>
        <span>新对话</span>
      </button>
    </header>

    <div ref="chatLog" class="conversation-scroll" :class="{ 'conversation-scroll--empty': !messages.length }" aria-live="polite">
      <div v-if="!messages.length" class="welcome-state">
        <span class="welcome-mark" aria-hidden="true">
          <svg viewBox="0 0 24 24"><path d="M12 3.5 13.8 10l6.7 2-6.7 2-1.8 6.5L10.2 14l-6.7-2 6.7-2L12 3.5Z" /></svg>
        </span>
        <h1>今天想学点什么？</h1>
        <p>告诉我学习主题、目标和时间安排，我会为你规划课程，并准备配套大纲与课件。</p>
      </div>

      <div v-else class="message-list">
        <article v-for="message in messages" :key="message.id" class="chat-message" :class="`chat-message--${message.role}`">
          <span v-if="message.role === 'assistant'" class="message-mark" aria-hidden="true">师</span>
          <div class="message-content">
            <div v-if="message.role === 'assistant' && message.note" class="message-label">{{ message.note }}</div>
            <p class="message-text">{{ message.text }}</p>

            <section v-if="message.outline" class="outline-card" aria-label="课程大纲预览">
              <div class="outline-heading">
                <strong>课程大纲</strong>
                <span>{{ message.outline.length }} 个单元 · {{ message.outline.reduce((total, unit) => total + unit.lessons.length, 0) }} 个小节</span>
              </div>
              <ol>
                <li v-for="(unit, index) in message.outline" :key="unit.title">
                  <div class="outline-unit-heading">
                    <span class="unit-number">{{ String(index + 1).padStart(2, '0') }}</span>
                    <strong>{{ unit.title }}</strong>
                  </div>
                  <ol class="outline-lesson-list">
                    <li v-for="(lesson, lessonIndex) in unit.lessons" :key="`${unit.title}-${lesson}`">
                      <span class="lesson-number">{{ index + 1 }}.{{ lessonIndex + 1 }}</span>
                      <span>{{ lesson }}</span>
                    </li>
                  </ol>
                </li>
              </ol>
            </section>

            <div v-if="message.files" class="result-card">
              <span class="result-heading">本次生成</span>
              <div class="file-list">
                <span v-for="file in message.files" :key="file" class="file-chip">
                  <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 3.75h8l4 4v12.5H6zM14 3.75v4h4M9 13h6M9 16.5h6" /></svg>
                  {{ file }}
                </span>
              </div>
              <button v-if="generatedCourse && !isSaved" class="save-course-button" type="button" @click="saveCourse">保存到我的课程</button>
              <span v-else-if="isSaved" class="saved-message">已保存到我的课程</span>
            </div>
          </div>
        </article>

        <div v-if="isBusy" class="thinking-indicator" role="status">
          <span class="message-mark" aria-hidden="true">师</span>
          <span class="thinking-dots"><i /><i /><i /></span>
          <span>{{ generatedCourse ? '正在准备配套课件…' : '正在整理课程大纲…' }}</span>
        </div>
      </div>
    </div>

    <footer class="composer-area">
      <section v-if="showSettings" class="settings-panel" aria-label="课程设置">
        <div class="settings-panel-header">
          <strong>课程偏好</strong>
          <span>可随时调整</span>
        </div>
        <div class="setting-row">
          <span class="setting-name">模型</span>
          <div class="setting-choices">
            <button v-for="model in models" :key="model.name" class="setting-choice" type="button" :disabled="isBusy" :aria-pressed="selectedModel === model.name" @click="selectedModel = model.name">
              {{ model.name }}
            </button>
          </div>
        </div>
        <div class="setting-row">
          <span class="setting-name">学习周期</span>
          <div class="setting-choices">
            <button class="setting-choice" type="button" :disabled="isBusy" :aria-pressed="selectedCycle === 'short'" @click="selectedCycle = 'short'">短期计划</button>
            <button class="setting-choice" type="button" :disabled="isBusy" :aria-pressed="selectedCycle === 'long'" @click="selectedCycle = 'long'">长期计划</button>
          </div>
        </div>
        <div class="setting-row">
          <span class="setting-name">起点</span>
          <div class="setting-choices">
            <button class="setting-choice" type="button" :disabled="isBusy" :aria-pressed="selectedLevel === 'beginner'" @click="selectedLevel = 'beginner'">零基础</button>
            <button class="setting-choice" type="button" :disabled="isBusy" :aria-pressed="selectedLevel === 'intermediate'" @click="selectedLevel = 'intermediate'">有基础</button>
          </div>
        </div>
      </section>

      <div class="composer-box">
        <textarea
          v-model="draft"
          rows="2"
          :disabled="isBusy"
          placeholder="描述你想学习的主题、目标和时间安排…"
          aria-label="描述课程学习目标"
          @keydown.enter.exact.prevent="submitGoal()"
        />
        <div class="composer-toolbar">
          <button class="settings-toggle" type="button" :aria-expanded="showSettings" :disabled="isBusy" @click="showSettings = !showSettings">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h9M17 7h3M4 17h3m4 0h9M13 4v6M7 14v6" /></svg>
            <span>课程偏好</span>
            <span class="selected-model">{{ selectedModel }}</span>
          </button>
          <div class="composer-submit-group">
            <span class="composer-hint">Enter 发送 · Shift + Enter 换行</span>
            <button class="send-button" type="button" :disabled="isBusy || !draft.trim()" :aria-label="isBusy ? '正在生成课程' : '发送消息'" @click="submitGoal()">
              <svg v-if="!isBusy" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 19V5m-7 7 7-7 7 7" /></svg>
              <span v-else class="send-spinner" aria-hidden="true" />
            </button>
          </div>
        </div>
      </div>
      <p class="composer-disclaimer">当前为前端演示，暂未连接模型服务。</p>
    </footer>
  </section>
</template>

<style scoped>
.generate-workspace {
  display: flex;
  width: min(100%, 900px);
  height: calc(100dvh - 142px);
  min-height: 500px;
  flex-direction: column;
  margin-inline: auto;
  color: var(--fg);
}

.conversation-header {
  display: flex;
  flex: none;
  align-items: center;
  justify-content: flex-end;
  gap: 16px;
  padding: 0 0 10px;
}

.message-mark {
  display: grid;
  width: 34px;
  height: 34px;
  flex: none;
  place-items: center;
  border: 1px solid color-mix(in oklch, var(--accent) 25%, var(--border));
  border-radius: 12px;
  background: color-mix(in oklch, var(--accent) 9%, var(--surface));
  color: var(--accent);
  font-size: 14px;
  font-weight: 650;
}
.new-chat-button, .settings-toggle {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--surface);
  color: var(--muted);
  cursor: pointer;
  transition: border-color 140ms ease, color 140ms ease, background 140ms ease;
}
.new-chat-button { padding: 8px 11px; font: inherit; font-size: 12px; }
.new-chat-button:hover, .settings-toggle:hover { border-color: var(--accent); color: var(--fg); }
.new-chat-button:disabled, .settings-toggle:disabled { cursor: not-allowed; opacity: .5; }
.new-chat-button svg, .settings-toggle svg { width: 16px; height: 16px; fill: none; stroke: currentColor; stroke-linecap: round; stroke-linejoin: round; stroke-width: 1.7; }

.conversation-scroll { min-height: 0; flex: 1; overflow-y: auto; overscroll-behavior: contain; scrollbar-color: var(--border) transparent; scrollbar-width: thin; }
.conversation-scroll--empty { display: grid; place-items: center; }
.welcome-state { width: min(100%, 590px); padding: 36px 12px 44px; text-align: center; }
.welcome-mark {
  display: inline-grid;
  width: 54px;
  height: 54px;
  place-items: center;
  border: 1px solid color-mix(in oklch, var(--accent) 22%, var(--border));
  border-radius: 19px;
  background: color-mix(in oklch, var(--accent) 8%, var(--surface));
  color: var(--accent);
}
.welcome-mark svg { width: 25px; height: 25px; fill: none; stroke: currentColor; stroke-linecap: round; stroke-linejoin: round; stroke-width: 1.5; }
.welcome-state h1 { margin: 17px 0 8px; font-size: clamp(25px, 4vw, 32px); font-weight: 650; letter-spacing: -.035em; }
.welcome-state > p { max-width: 48ch; margin: 0 auto; color: var(--muted); font-size: 14px; line-height: 1.75; }
.message-list { display: flex; flex-direction: column; gap: 28px; padding: 28px 0 36px; }
.chat-message { display: flex; align-items: flex-start; gap: 11px; }
.chat-message--user { justify-content: flex-end; }
.message-content { min-width: 0; max-width: min(100%, 720px); }
.chat-message--assistant .message-content { flex: 1; }
.chat-message--user .message-content { max-width: min(82%, 650px); padding: 12px 16px; border: 1px solid var(--border-soft); border-radius: 18px 18px 5px 18px; background: color-mix(in oklch, var(--surface) 78%, var(--border-soft)); }
.message-mark { width: 29px; height: 29px; border-radius: 10px; font-size: 12px; }
.message-label { margin: 1px 0 8px; color: var(--muted); font-size: 12px; font-weight: 600; }
.message-text { margin: 0; color: var(--fg); font-size: 14px; line-height: 1.8; white-space: pre-wrap; overflow-wrap: anywhere; }
.outline-card, .result-card { margin-top: 15px; border: 1px solid var(--border); border-radius: 14px; background: var(--surface); }
.outline-card { padding: 15px 17px; }
.outline-heading { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding-bottom: 12px; border-bottom: 1px solid var(--border-soft); }
.outline-heading strong, .result-heading { font-size: 13px; font-weight: 650; }
.outline-heading > span { color: var(--muted); font-size: 11px; }
.outline-card > ol { display: flex; flex-direction: column; gap: 2px; margin: 9px 0 0; padding: 0; list-style: none; }
.outline-card > ol > li { padding: 10px 0; color: var(--muted); font-size: 13px; line-height: 1.55; }
.outline-unit-heading { display: flex; align-items: baseline; gap: 12px; }
.outline-unit-heading strong { color: var(--fg); font-size: 12px; font-weight: 600; }
.outline-lesson-list { display: flex; flex-direction: column; gap: 3px; margin: 6px 0 0 28px; padding: 0; list-style: none; }
.outline-lesson-list li { display: flex; align-items: baseline; gap: 9px; padding: 3px 0; color: var(--muted); font-size: 11px; line-height: 1.5; }
.lesson-number { flex: none; color: var(--meta); font-family: var(--font-mono); font-size: 9px; }
.unit-number { flex: none; color: var(--accent); font-family: var(--font-mono); font-size: 10px; }
.result-card { padding: 14px 15px; }
.file-list { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 11px; }
.file-chip { display: inline-flex; align-items: center; gap: 6px; max-width: 100%; padding: 7px 9px; border: 1px solid var(--border-soft); border-radius: 8px; background: var(--bg); color: var(--muted); font-family: var(--font-mono); font-size: 10px; overflow-wrap: anywhere; }
.file-chip svg { width: 14px; height: 14px; flex: none; fill: none; stroke: var(--accent); stroke-linecap: round; stroke-linejoin: round; stroke-width: 1.5; }
.save-course-button { margin-top: 14px; padding: 8px 12px; border: 0; border-radius: 9px; background: var(--accent); color: white; cursor: pointer; font: inherit; font-size: 12px; font-weight: 600; }
.save-course-button:hover { filter: brightness(.96); }
.saved-message { display: inline-block; margin-top: 13px; color: var(--success); font-size: 12px; }
.thinking-indicator { display: flex; align-items: center; gap: 10px; color: var(--muted); font-size: 12px; }
.thinking-dots { display: flex; gap: 3px; }
.thinking-dots i { width: 5px; height: 5px; border-radius: 50%; background: var(--accent); animation: thinking 1s infinite ease-in-out; }
.thinking-dots i:nth-child(2) { animation-delay: .14s; }
.thinking-dots i:nth-child(3) { animation-delay: .28s; }
@keyframes thinking { 0%, 60%, 100% { opacity: .32; transform: translateY(0); } 30% { opacity: 1; transform: translateY(-3px); } }

.composer-area { position: relative; z-index: 2; flex: none; padding-top: 12px; background: linear-gradient(180deg, color-mix(in oklch, var(--bg) 0%, transparent), var(--bg) 18%); }
.composer-box { padding: 12px 13px 10px; border: 1px solid var(--border); border-radius: 17px; background: var(--surface); box-shadow: 0 8px 28px color-mix(in oklch, var(--fg) 5%, transparent); }
.composer-box:focus-within { border-color: color-mix(in oklch, var(--accent) 48%, var(--border)); box-shadow: 0 0 0 3px color-mix(in oklch, var(--accent) 9%, transparent); }
.composer-box textarea { display: block; width: 100%; min-height: 52px; max-height: 160px; resize: vertical; border: 0; outline: 0; background: transparent; color: var(--fg); font: inherit; font-size: 14px; line-height: 1.6; }
.composer-box textarea::placeholder { color: var(--meta); }
.composer-box textarea:disabled { opacity: .65; }
.composer-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding-top: 8px; }
.settings-toggle { min-width: 0; padding: 7px 9px; font: inherit; font-size: 11px; }
.settings-toggle[aria-expanded="true"] { border-color: color-mix(in oklch, var(--accent) 42%, var(--border)); color: var(--accent); }
.selected-model { overflow: hidden; max-width: 120px; padding-left: 7px; border-left: 1px solid var(--border); color: var(--meta); text-overflow: ellipsis; white-space: nowrap; }
.composer-submit-group { display: flex; align-items: center; gap: 12px; }
.composer-hint, .composer-disclaimer { color: var(--meta); font-size: 10px; }
.send-button { display: grid; width: 34px; height: 34px; flex: none; place-items: center; border: 0; border-radius: 11px; background: var(--accent); color: #fff; cursor: pointer; transition: opacity 120ms ease, transform 120ms ease; }
.send-button:hover:not(:disabled) { transform: translateY(-1px); }
.send-button:disabled { cursor: not-allowed; opacity: .38; }
.send-button svg { width: 17px; height: 17px; fill: none; stroke: currentColor; stroke-linecap: round; stroke-linejoin: round; stroke-width: 1.8; }
.send-spinner { width: 15px; height: 15px; border: 2px solid rgb(255 255 255 / 38%); border-top-color: #fff; border-radius: 50%; animation: spin .75s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
.composer-disclaimer { margin: 8px 0 0; text-align: center; }

.settings-panel { margin-bottom: 10px; padding: 15px 16px; border: 1px solid var(--border); border-radius: 14px; background: var(--surface); box-shadow: 0 8px 28px color-mix(in oklch, var(--fg) 5%, transparent); }
.settings-panel-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
.settings-panel-header strong { font-size: 12px; }
.settings-panel-header span { color: var(--meta); font-size: 10px; }
.setting-row { display: grid; grid-template-columns: 78px minmax(0, 1fr); gap: 12px; align-items: center; padding: 8px 0; }
.setting-row + .setting-row { border-top: 1px solid var(--border-soft); }
.setting-name { color: var(--muted); font-size: 11px; }
.setting-choices { display: flex; flex-wrap: wrap; gap: 7px; }
.setting-choice { padding: 6px 9px; border: 1px solid var(--border); border-radius: 8px; background: var(--bg); color: var(--muted); cursor: pointer; font: inherit; font-size: 10px; transition: border-color 140ms ease, color 140ms ease; }
.setting-choice:hover:not(:disabled) { border-color: var(--accent); color: var(--fg); }
.setting-choice[aria-pressed="true"] { border-color: color-mix(in oklch, var(--accent) 55%, var(--border)); background: color-mix(in oklch, var(--accent) 7%, var(--surface)); color: var(--accent); }
.setting-choice:disabled { cursor: not-allowed; opacity: .55; }

@media (max-width: 760px) {
  .generate-workspace { height: calc(100dvh - 128px); min-height: 470px; }
  .conversation-header { padding-bottom: 12px; }
  .conversation-identity > div > span { max-width: 48vw; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .welcome-state { padding-inline: 2px; }
  .message-list { gap: 22px; padding-top: 20px; }
  .chat-message--user .message-content { max-width: 90%; }
  .composer-hint { display: none; }
  .setting-row { grid-template-columns: 65px minmax(0, 1fr); gap: 8px; }
}

@media (max-height: 720px) and (min-width: 761px) {
  .generate-workspace { height: calc(100dvh - 120px); min-height: 420px; }
  .welcome-state { padding-block: 16px 20px; }
}
</style>
