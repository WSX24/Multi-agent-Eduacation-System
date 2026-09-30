<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import { loadDemoCourses, physicsDemoCourse, type DemoCourse } from '../data/demoCourses'

const route = useRoute()
const courses = ref<DemoCourse[]>([])

onMounted(() => {
  courses.value = loadDemoCourses()
})

const course = computed(() => {
  const courseId = String(route.params.id ?? '')
  return courses.value.find((item) => item.id === courseId)
    ?? (courseId === physicsDemoCourse.id ? physicsDemoCourse : undefined)
})

const cycleLabel = computed(() => course.value?.cycle === 'short' ? '短期计划' : '长期计划')
const levelLabel = computed(() => course.value?.level === 'beginner' ? '零基础起步' : '已有学习基础')
const lessonCount = computed(() => course.value?.units.reduce((total, unit) => total + unit.lessons.length, 0) ?? 0)
const overviewText = computed(() => course.value?.overview
  ?? `围绕「${course.value?.title ?? ''}」的核心内容逐步展开，帮助你建立系统的知识框架。`)

function isLessonComplete(unitIndex: number, lessonIndex: number) {
  return course.value?.completedLessons?.includes(`${unitIndex + 1}.${lessonIndex + 1}`) ?? false
}

</script>

<template>
  <div class="course-detail-page">
    <RouterLink class="detail-back" :to="{ name: 'app-courses' }">
      <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m15 18-6-6 6-6M9 12h11" /></svg>
      返回我的课堂
    </RouterLink>

    <template v-if="course">
      <header class="detail-hero">
        <div class="detail-hero-copy">
          <span class="eyebrow">个性化课堂</span>
          <h1>{{ course.title }}</h1>
          <div class="detail-tags">
            <span>{{ cycleLabel }}</span>
            <span>{{ levelLabel }}</span>
          </div>
        </div>
        <div class="detail-hero-art" aria-hidden="true">
          <svg viewBox="0 0 160 112">
            <path class="cover-book-shadow" d="M80 34c-15-12-34-14-54-8v54c19-6 38-4 54 8 16-12 35-14 54-8V26c-20-6-39-4-54 8Z" />
            <path class="cover-book-page" d="M80 37c-14-11-31-12-48-7v43c17-5 34-3 48 8V37Z" />
            <path class="cover-book-page cover-book-page--right" d="M80 37c14-11 31-12 48-7v43c-17-5-34-3-48 8V37Z" />
            <path class="cover-book-line" d="M43 43c10-2 19-1 27 3M43 53c10-2 19-1 27 3M90 46c8-4 17-5 27-3M90 56c8-4 17-5 27-3" />
            <path class="cover-book-spine" d="M80 37v44" />
          </svg>
        </div>
      </header>

      <div class="detail-layout">
        <main class="detail-main">
          <section class="detail-section">
            <div class="detail-section-heading">
              <div>
                <span class="section-kicker">COURSE OVERVIEW</span>
                <h2>课程概述</h2>
              </div>
            </div>
            <p class="overview-copy">{{ overviewText }}</p>
          </section>

          <section class="detail-section goal-section">
            <div class="detail-section-heading">
              <div>
                <span class="section-kicker">YOUR LEARNING GOAL</span>
                <h2>学习目标</h2>
              </div>
            </div>
            <p>{{ course.goal }}</p>
          </section>

          <section class="detail-section">
            <div class="detail-section-heading">
              <div>
                <span class="section-kicker">COURSE OUTLINE</span>
                <h2>课程大纲</h2>
              </div>
              <span class="outline-count">{{ course.units.length }} 个单元 · {{ lessonCount }} 个小节</span>
            </div>
            <ol class="unit-list">
              <li v-for="(unit, index) in course.units" :key="unit.title" class="unit-item">
                <div class="unit-heading">
                  <span class="unit-copy">
                    <span class="unit-label">第 {{ index + 1 }} 单元 · {{ unit.lessons.length }} 个小节</span>
                    <strong>{{ unit.title }}</strong>
                  </span>
                </div>
                <ol class="lesson-list" :aria-label="`${unit.title}的小节`">
                  <li v-for="(lesson, lessonIndex) in unit.lessons" :key="`${unit.title}-${lesson}`">
                    <RouterLink
                      class="lesson-entry"
                      :to="{ name: 'app-lesson', params: { id: course.id, unitNumber: index + 1, lessonNumber: lessonIndex + 1 } }"
                      :aria-label="`${lesson}，第 ${index + 1} 单元第 ${lessonIndex + 1} 小节，${isLessonComplete(index, lessonIndex) ? '已完成' : '未完成'}，进入学习`"
                    >
                      <span
                        class="lesson-status"
                        :class="isLessonComplete(index, lessonIndex) ? 'lesson-status--done' : 'lesson-status--todo'"
                        aria-hidden="true"
                      >{{ isLessonComplete(index, lessonIndex) ? '✓' : '未' }}</span>
                      <span class="lesson-index" aria-hidden="true">{{ index + 1 }}.{{ lessonIndex + 1 }}</span>
                      <span class="lesson-title">{{ lesson }}</span>
                      <span class="lesson-action">进入学习<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 18 6-6-6-6" /></svg></span>
                    </RouterLink>
                  </li>
                </ol>
              </li>
            </ol>
          </section>
        </main>

        <aside class="detail-sidebar">
          <section class="course-info-card">
            <h2>课程信息</h2>
            <dl>
              <div><dt>学习周期</dt><dd>{{ cycleLabel }}</dd></div>
              <div><dt>学习起点</dt><dd>{{ levelLabel }}</dd></div>
              <div><dt>课程模型</dt><dd>{{ course.model }}</dd></div>
              <div><dt>单元 / 小节</dt><dd>{{ course.units.length }} / {{ lessonCount }}</dd></div>
              <div><dt>创建时间</dt><dd>{{ course.createdAt }}</dd></div>
            </dl>
          </section>
          <RouterLink class="back-to-library" :to="{ name: 'app-courses' }">
            返回我的课堂
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14m-6-6 6 6-6 6" /></svg>
          </RouterLink>
        </aside>
      </div>
    </template>

    <section v-else class="detail-not-found">
      <span class="not-found-mark" aria-hidden="true">?</span>
      <h1>没有找到这门课堂</h1>
      <p>课堂可能已被移除，或者当前浏览器没有保存这门课堂。</p>
      <RouterLink class="btn btn-primary" :to="{ name: 'app-courses' }">返回我的课堂</RouterLink>
    </section>
  </div>
</template>

<style scoped>
.course-detail-page { max-width: 1160px; margin-inline: auto; }
.detail-back { display: inline-flex; align-items: center; gap: 7px; margin: 0 0 18px; color: var(--muted); font-size: 12px; transition: color 140ms ease; }
.detail-back:hover { color: var(--accent); }
.detail-back svg, .back-to-library svg { width: 16px; height: 16px; fill: none; stroke: currentColor; stroke-linecap: round; stroke-linejoin: round; stroke-width: 1.7; }
.detail-hero { position: relative; display: flex; min-height: 220px; align-items: center; justify-content: space-between; gap: 28px; overflow: hidden; padding: 28px 34px; border: 1px solid color-mix(in oklch, var(--accent) 17%, var(--border)); border-radius: 18px; background: linear-gradient(112deg, color-mix(in oklch, var(--accent) 10%, var(--surface)), var(--surface) 78%); }
.detail-hero::after { position: absolute; right: 8%; bottom: -128px; width: 300px; height: 300px; border: 1px solid color-mix(in oklch, var(--accent) 12%, transparent); border-radius: 50%; content: ''; }
.detail-hero-copy { position: relative; z-index: 1; max-width: 690px; }
.detail-hero .eyebrow { color: var(--accent); }
.detail-hero h1 { margin-top: 9px; font-size: clamp(26px, 4vw, 38px); font-weight: 680; letter-spacing: -.035em; line-height: 1.25; }
.detail-tags { display: flex; flex-wrap: wrap; gap: 7px; margin-top: 14px; }
.detail-tags span { padding: 6px 9px; border: 1px solid color-mix(in oklch, var(--accent) 18%, var(--border)); border-radius: 7px; background: color-mix(in oklch, var(--surface) 80%, transparent); color: var(--muted); font-size: 10px; }
.detail-hero-art { position: relative; z-index: 1; display: grid; width: 190px; height: 150px; flex: none; place-items: center; }
.detail-hero-art::before { position: absolute; width: 156px; height: 156px; border: 1px solid color-mix(in oklch, var(--accent) 20%, transparent); border-radius: 50%; content: ''; }
.detail-hero-art svg { width: 160px; filter: drop-shadow(0 10px 12px rgb(35 23 54 / 13%)); }
.cover-book-shadow { fill: color-mix(in oklch, var(--accent) 32%, #b8a8d1); }
.cover-book-page { fill: #fffdfd; stroke: color-mix(in oklch, var(--accent) 18%, var(--border)); stroke-width: 1.2; }
.cover-book-page--right { fill: color-mix(in oklch, var(--accent) 4%, #fff); }
.cover-book-line, .cover-book-spine { fill: none; stroke: color-mix(in oklch, var(--accent) 36%, #9583ae); stroke-linecap: round; stroke-width: 1.5; }

.detail-layout { display: grid; grid-template-columns: minmax(0, 1fr) 280px; gap: 22px; align-items: start; margin-top: 22px; }
.detail-main { display: flex; flex-direction: column; gap: 18px; min-width: 0; }
.detail-section, .course-info-card { border: 1px solid var(--border); border-radius: 15px; background: var(--surface); }
.detail-section { padding: 22px 24px; }
.detail-section-heading { display: flex; align-items: flex-end; justify-content: space-between; gap: 12px; margin-bottom: 16px; }
.section-kicker { color: var(--meta); font-family: var(--font-mono); font-size: 9px; letter-spacing: .08em; }
.detail-section-heading h2 { margin-top: 5px; font-size: 18px; font-weight: 650; }
.outline-count { padding: 6px 9px; border: 1px solid var(--border-soft); border-radius: 7px; color: var(--muted); font-size: 10px; }
.overview-copy { margin: 0; color: var(--muted); font-size: 13px; line-height: 1.85; white-space: pre-wrap; overflow-wrap: anywhere; }
.unit-list { display: flex; flex-direction: column; gap: 18px; margin: 0; padding: 0; list-style: none; }
.unit-item { min-width: 0; }
.unit-heading { display: grid; width: 100%; min-height: 72px; grid-template-columns: minmax(0, 1fr); align-items: center; padding: 12px 15px; border: 1px solid var(--border); border-radius: 12px; background: color-mix(in oklch, var(--surface) 88%, var(--border-soft)); color: var(--fg); text-align: left; }
.unit-copy { display: flex; min-width: 0; flex-direction: column; gap: 5px; }
.unit-label { color: var(--meta); font-size: 10px; }
.unit-copy strong { font-size: 13px; font-weight: 600; line-height: 1.5; }
.lesson-list { display: flex; flex-direction: column; gap: 2px; margin: 8px 0 0 18px; padding: 1px 0 1px 29px; border-left: 1px solid var(--border-soft); list-style: none; }
.lesson-list li { display: flex; align-items: baseline; gap: 10px; padding: 5px 0; color: var(--muted); font-size: 11px; line-height: 1.55; }
.lesson-index { flex: none; color: var(--meta); font-family: var(--font-mono); font-size: 9px; }
.lesson-entry { display: flex; min-height: 38px; align-items: center; gap: 10px; padding: 6px 10px; border-radius: 8px; color: var(--muted); font-size: 11px; line-height: 1.55; transition: background 140ms ease, color 140ms ease; }
.lesson-entry:hover { background: color-mix(in oklch, var(--accent) 7%, var(--surface)); color: var(--fg); }
.lesson-entry:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; background: color-mix(in oklch, var(--accent) 7%, var(--surface)); color: var(--fg); }
.lesson-title { flex: 1; }
.lesson-status { display: grid; width: 21px; height: 21px; flex: none; place-items: center; border: 1px solid transparent; border-radius: 50%; font-size: 8px; font-weight: 700; }
.lesson-status--todo { border-color: #f5d7a9; background: #fff5e5; color: #d98216; }
.lesson-status--done { border-color: #b9e5ca; background: #eaf7ef; color: #16864a; font-size: 12px; }
.lesson-action { display: inline-flex; align-items: center; gap: 3px; color: var(--accent); font-size: 10px; white-space: nowrap; opacity: .82; }
.lesson-action svg { width: 14px; height: 14px; fill: none; stroke: currentColor; stroke-linecap: round; stroke-linejoin: round; stroke-width: 1.7; }
.goal-section > p { color: var(--muted); font-size: 13px; line-height: 1.8; white-space: pre-wrap; overflow-wrap: anywhere; }

.detail-sidebar { position: sticky; top: 80px; }
.course-info-card { padding: 18px; }
.course-info-card h2 { font-size: 14px; font-weight: 650; }
.course-info-card dl { margin: 10px 0 0; }
.course-info-card dl > div { display: flex; justify-content: space-between; gap: 12px; padding: 11px 0; border-bottom: 1px solid var(--border-soft); font-size: 11px; }
.course-info-card dl > div:last-child { border-bottom: 0; }
.course-info-card dt { color: var(--muted); }
.course-info-card dd { max-width: 58%; margin: 0; color: var(--fg); text-align: right; overflow-wrap: anywhere; }
.back-to-library { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-top: 12px; padding: 12px 14px; border: 1px solid var(--border); border-radius: 11px; background: var(--surface); color: var(--muted); font-size: 11px; transition: border-color 140ms ease, color 140ms ease; }
.back-to-library:hover { border-color: var(--accent); color: var(--accent); }
.detail-not-found { display: flex; min-height: 50vh; flex-direction: column; align-items: center; justify-content: center; text-align: center; }
.not-found-mark { display: grid; width: 46px; height: 46px; place-items: center; border-radius: 15px; background: color-mix(in oklch, var(--accent) 9%, var(--surface)); color: var(--accent); font-size: 20px; }
.detail-not-found h1 { margin-top: 15px; font-size: 21px; }
.detail-not-found p { max-width: 48ch; margin-top: 8px; color: var(--muted); font-size: 12px; line-height: 1.7; }
.detail-not-found .btn { margin-top: 17px; }

@media (max-width: 900px) {
  .detail-layout { grid-template-columns: minmax(0, 1fr) 245px; gap: 15px; }
  .detail-hero { padding-inline: 24px; }
  .detail-hero-art { width: 145px; }
}

@media (max-width: 700px) {
  .detail-hero { min-height: unset; padding: 22px; }
  .detail-hero-art { display: none; }
  .detail-layout { grid-template-columns: minmax(0, 1fr); }
  .detail-sidebar { position: static; grid-row: 1; }
  .detail-section { padding: 18px 16px; }
  .unit-heading { grid-template-columns: minmax(0, 1fr); padding-inline: 11px; }
  .lesson-entry { gap: 7px; padding-inline: 7px; }
  .lesson-action { font-size: 0; }
  .lesson-action svg { width: 16px; height: 16px; }
}
</style>
