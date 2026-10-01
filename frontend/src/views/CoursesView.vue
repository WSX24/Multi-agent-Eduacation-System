<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { loadDemoCourses, physicsDemoCourse, type DemoCourse } from '../data/demoCourses'

type CycleFilter = 'all' | 'short' | 'long'

const savedCourses = ref<DemoCourse[]>([])
const searchText = ref('')
const cycleFilter = ref<CycleFilter>('all')

onMounted(() => {
  savedCourses.value = loadDemoCourses()
})

const courses = computed(() => [
  physicsDemoCourse,
  ...savedCourses.value.filter((course) => course.id !== physicsDemoCourse.id),
])
const visibleCourses = computed(() => {
  const keyword = searchText.value.trim().toLocaleLowerCase()

  return courses.value.filter((course) => {
    const matchesCycle = cycleFilter.value === 'all' || course.cycle === cycleFilter.value
    const outlineText = course.units
      .map((unit) => `${unit.title} ${unit.lessons.join(' ')}`)
      .join(' ')
    const matchesKeyword = !keyword || `${course.title} ${course.goal} ${outlineText}`.toLocaleLowerCase().includes(keyword)
    return matchesCycle && matchesKeyword
  })
})

</script>

<template>
  <div class="classroom-page">
    <header class="library-heading">
      <div>
        <span class="eyebrow">MY CLASSROOM</span>
        <h1 class="page-title">我的课堂</h1>
        <p class="lead">把生成的课程收在这里，随时回来查看学习大纲。</p>
      </div>
      <RouterLink class="btn btn-primary create-classroom" :to="{ name: 'app-generate' }">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14" /></svg>
        生成新课堂
      </RouterLink>
    </header>

    <template v-if="courses.length">
      <section class="library-overview" aria-label="课堂概览">
        <span class="overview-icon" aria-hidden="true">
          <svg viewBox="0 0 24 24"><path d="M4 5.5c3.2-.9 5.9-.2 8 1.5 2.1-1.7 4.8-2.4 8-1.5v13c-3.2-.9-5.9-.2-8 1.5-2.1-1.7-4.8-2.4-8-1.5v-13Z" /><path d="M12 7v13" /></svg>
        </span>
        <div class="overview-copy">
          <strong>你的学习空间</strong>
          <span>已保存 {{ courses.length }} 门课堂</span>
        </div>
        <span class="overview-mark">LEARNING LIBRARY</span>
      </section>

      <section class="classroom-library" aria-label="已保存的课堂">
        <div class="library-toolbar">
          <div class="library-title-row">
            <div>
              <h2>全部课堂</h2>
              <span>{{ visibleCourses.length }} 门</span>
            </div>
            <label class="course-search">
              <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="10.8" cy="10.8" r="6.8" /><path d="m16 16 4 4" /></svg>
              <input v-model="searchText" type="search" placeholder="搜索课堂或学习内容" aria-label="搜索课堂">
            </label>
          </div>

          <div class="filter-row" role="group" aria-label="按学习周期筛选">
            <button class="filter-chip" type="button" :aria-pressed="cycleFilter === 'all'" @click="cycleFilter = 'all'">全部</button>
            <button class="filter-chip" type="button" :aria-pressed="cycleFilter === 'long'" @click="cycleFilter = 'long'">长期计划</button>
            <button class="filter-chip" type="button" :aria-pressed="cycleFilter === 'short'" @click="cycleFilter = 'short'">短期计划</button>
          </div>
        </div>

        <div v-if="visibleCourses.length" class="classroom-grid">
          <RouterLink
            v-for="(course, index) in visibleCourses"
            :key="course.id"
            class="classroom-card"
            :to="{ name: 'app-course-detail', params: { id: course.id } }"
          >
            <div class="classroom-cover" :class="`classroom-cover--${index % 3}`" aria-hidden="true">
              <span class="cover-kicker">PERSONAL STUDY PLAN</span>
              <svg class="cover-illustration" viewBox="0 0 160 112">
                <path class="cover-book-shadow" d="M80 34c-15-12-34-14-54-8v54c19-6 38-4 54 8 16-12 35-14 54-8V26c-20-6-39-4-54 8Z" />
                <path class="cover-book-page" d="M80 37c-14-11-31-12-48-7v43c17-5 34-3 48 8V37Z" />
                <path class="cover-book-page cover-book-page--right" d="M80 37c14-11 31-12 48-7v43c-17-5-34-3-48 8V37Z" />
                <path class="cover-book-line" d="M43 43c10-2 19-1 27 3M43 53c10-2 19-1 27 3M90 46c8-4 17-5 27-3M90 56c8-4 17-5 27-3" />
                <path class="cover-book-spine" d="M80 37v44" />
              </svg>
              <span class="cover-index">{{ String(index + 1).padStart(2, '0') }}</span>
              <span class="cover-cycle">{{ course.cycle === 'short' ? '短期计划' : '长期计划' }}</span>
            </div>

            <div class="classroom-card-body">
              <div class="course-card-heading">
                <span class="course-kind"><i />个性化课堂</span>
              </div>
              <h3>{{ course.title }}</h3>
              <p class="classroom-goal">{{ course.goal }}</p>
              <footer class="classroom-card-footer">
                <span>创建于 {{ course.createdAt }}</span>
              </footer>
            </div>
          </RouterLink>
        </div>

        <div v-else class="no-results">
          <span class="no-results-mark" aria-hidden="true">
            <svg viewBox="0 0 24 24"><circle cx="10.8" cy="10.8" r="6.8" /><path d="m16 16 4 4" /></svg>
          </span>
          <strong>没有找到匹配的课堂</strong>
          <span>试试其他关键词，或切换学习周期。</span>
          <button class="clear-filters" type="button" @click="searchText = ''; cycleFilter = 'all'">清除筛选</button>
        </div>
      </section>
    </template>

    <section v-else class="empty-library">
      <div class="empty-art" aria-hidden="true">
        <span class="empty-art-orbit empty-art-orbit--one" />
        <span class="empty-art-orbit empty-art-orbit--two" />
        <svg viewBox="0 0 120 100">
          <path class="empty-book-back" d="M60 24C44 12 26 13 10 19v57c17-6 34-5 50 7 16-12 33-13 50-7V19c-16-6-34-7-50 5Z" />
          <path class="empty-book-page" d="M60 28C46 18 30 18 17 23v45c15-5 29-3 43 7V28Z" />
          <path class="empty-book-page empty-book-page--right" d="M60 28c14-10 30-10 43-5v45c-15-5-29-3-43 7V28Z" />
          <path class="empty-book-spine" d="M60 28v47" />
        </svg>
        <span class="empty-art-star empty-art-star--one">✦</span>
        <span class="empty-art-star empty-art-star--two">✦</span>
      </div>
      <h2>你的课堂会收纳在这里</h2>
      <p>从一次对话开始，规划适合自己的学习课程。生成并保存后，就能在这里查看大纲。</p>
      <RouterLink class="btn btn-primary" :to="{ name: 'app-generate' }">生成第一门课堂</RouterLink>
    </section>
  </div>
</template>

<style scoped>
.classroom-page { max-width: 1160px; margin-inline: auto; }
.library-heading { display: flex; align-items: center; justify-content: space-between; gap: 24px; margin-bottom: 26px; }
.library-heading .page-title { margin-top: 7px; }
.library-heading .lead { margin-top: 7px; }
.create-classroom { display: inline-flex; align-items: center; gap: 8px; white-space: nowrap; }
.create-classroom svg { width: 16px; height: 16px; fill: none; stroke: currentColor; stroke-linecap: round; stroke-width: 1.8; }

.library-overview { position: relative; display: flex; align-items: center; gap: 14px; overflow: hidden; min-height: 86px; margin-bottom: 30px; padding: 17px 20px; border: 1px solid color-mix(in oklch, var(--accent) 13%, var(--border)); border-radius: 16px; background: linear-gradient(105deg, color-mix(in oklch, var(--accent) 7%, var(--surface)), var(--surface) 66%); }
.overview-icon { display: grid; width: 46px; height: 46px; flex: none; place-items: center; border: 1px solid color-mix(in oklch, var(--accent) 20%, var(--border)); border-radius: 14px; background: color-mix(in oklch, var(--accent) 10%, var(--surface)); color: var(--accent); }
.overview-icon svg { width: 23px; height: 23px; fill: none; stroke: currentColor; stroke-linecap: round; stroke-linejoin: round; stroke-width: 1.55; }
.overview-copy { display: flex; flex-direction: column; gap: 4px; }
.overview-copy strong { font-size: 13px; font-weight: 650; }
.overview-copy span { color: var(--muted); font-size: 12px; }
.overview-mark { margin-left: auto; color: color-mix(in oklch, var(--accent) 35%, var(--meta)); font-family: var(--font-mono); font-size: 10px; letter-spacing: .09em; }

.classroom-library { margin-top: 4px; }
.library-toolbar { margin-bottom: 18px; }
.library-title-row { display: flex; align-items: center; justify-content: space-between; gap: 20px; }
.library-title-row > div { display: flex; align-items: baseline; gap: 10px; }
.library-title-row h2 { font-size: 18px; font-weight: 650; letter-spacing: -.02em; }
.library-title-row > div > span { color: var(--meta); font-size: 12px; }
.course-search { display: flex; width: min(100%, 310px); height: 39px; align-items: center; gap: 9px; padding: 0 11px; border: 1px solid var(--border); border-radius: 10px; background: var(--surface); color: var(--meta); transition: border-color 140ms ease, box-shadow 140ms ease; }
.course-search:focus-within { border-color: color-mix(in oklch, var(--accent) 45%, var(--border)); box-shadow: 0 0 0 3px color-mix(in oklch, var(--accent) 8%, transparent); }
.course-search svg { width: 16px; height: 16px; flex: none; fill: none; stroke: currentColor; stroke-linecap: round; stroke-width: 1.6; }
.course-search input { width: 100%; min-width: 0; border: 0; outline: 0; background: transparent; color: var(--fg); font: inherit; font-size: 12px; }
.course-search input::placeholder { color: var(--meta); }
.filter-row { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 17px; }
.filter-chip { padding: 7px 12px; border: 1px solid var(--border); border-radius: 999px; background: var(--surface); color: var(--muted); cursor: pointer; font: inherit; font-size: 11px; transition: background 130ms ease, border-color 130ms ease, color 130ms ease; }
.filter-chip:hover { border-color: color-mix(in oklch, var(--accent) 35%, var(--border)); color: var(--fg); }
.filter-chip[aria-pressed="true"] { border-color: var(--fg); background: var(--fg); color: var(--bg); }

.classroom-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 18px; align-items: start; }
.classroom-card { min-width: 0; overflow: hidden; border: 1px solid var(--border); border-radius: 15px; background: var(--surface); color: inherit; text-decoration: none; transition: transform 160ms ease, border-color 160ms ease, box-shadow 160ms ease; }
.classroom-card:hover { transform: translateY(-3px); border-color: color-mix(in oklch, var(--accent) 32%, var(--border)); box-shadow: 0 12px 28px color-mix(in oklch, var(--fg) 7%, transparent); }
.classroom-cover { position: relative; display: grid; aspect-ratio: 16 / 9; min-height: 150px; place-items: center; overflow: hidden; border-bottom: 1px solid color-mix(in oklch, var(--fg) 5%, transparent); background-color: #eee8fb; background-image: linear-gradient(90deg, rgb(103 75 160 / 5%) 1px, transparent 1px), linear-gradient(rgb(103 75 160 / 5%) 1px, transparent 1px), radial-gradient(ellipse at 70% 30%, rgb(255 255 255 / 70%), transparent 45%); background-size: 28px 28px, 28px 28px, auto; }
.classroom-cover::before, .classroom-cover::after { position: absolute; content: ''; border: 1px solid rgb(112 85 166 / 13%); border-radius: 50%; }
.classroom-cover::before { width: 190px; height: 190px; top: 22px; right: -60px; }
.classroom-cover::after { width: 140px; height: 140px; top: 47px; right: -35px; }
.classroom-cover--1 { background-color: #e4eff8; background-image: linear-gradient(90deg, rgb(56 115 151 / 5%) 1px, transparent 1px), linear-gradient(rgb(56 115 151 / 5%) 1px, transparent 1px), radial-gradient(ellipse at 30% 30%, rgb(255 255 255 / 75%), transparent 48%); background-size: 28px 28px, 28px 28px, auto; }
.classroom-cover--1::before, .classroom-cover--1::after { border-color: rgb(56 115 151 / 15%); }
.classroom-cover--2 { background-color: #f3eadf; background-image: linear-gradient(90deg, rgb(162 112 60 / 5%) 1px, transparent 1px), linear-gradient(rgb(162 112 60 / 5%) 1px, transparent 1px), radial-gradient(ellipse at 68% 28%, rgb(255 255 255 / 75%), transparent 48%); background-size: 28px 28px, 28px 28px, auto; }
.classroom-cover--2::before, .classroom-cover--2::after { border-color: rgb(162 112 60 / 14%); }
.cover-illustration { position: relative; z-index: 1; width: 132px; height: 94px; margin-top: 8px; filter: drop-shadow(0 7px 8px rgb(29 25 39 / 10%)); }
.cover-book-shadow { fill: rgb(107 82 151 / 26%); }
.cover-book-page { fill: #fffdfd; stroke: rgb(91 75 120 / 21%); stroke-width: 1.2; }
.cover-book-page--right { fill: #f8f5fc; }
.cover-book-line, .cover-book-spine { fill: none; stroke: rgb(112 91 143 / 37%); stroke-linecap: round; stroke-width: 1.5; }
.classroom-cover--1 .cover-book-shadow { fill: rgb(57 112 147 / 23%); }
.classroom-cover--1 .cover-book-page--right { fill: #f3f9fc; }
.classroom-cover--1 .cover-book-line, .classroom-cover--1 .cover-book-spine { stroke: rgb(73 119 147 / 39%); }
.classroom-cover--2 .cover-book-shadow { fill: rgb(156 111 61 / 22%); }
.classroom-cover--2 .cover-book-page--right { fill: #fcf8f2; }
.classroom-cover--2 .cover-book-line, .classroom-cover--2 .cover-book-spine { stroke: rgb(141 105 66 / 39%); }
.cover-kicker { position: absolute; z-index: 1; top: 14px; left: 15px; color: color-mix(in oklch, var(--fg) 42%, transparent); font-family: var(--font-mono); font-size: 8px; letter-spacing: .12em; }
.cover-index { position: absolute; right: 13px; bottom: 10px; color: rgb(49 40 66 / 19%); font-family: var(--font-mono); font-size: 27px; font-weight: 600; letter-spacing: -.08em; }
.cover-cycle { position: absolute; right: 13px; top: 12px; padding: 5px 8px; border: 1px solid rgb(255 255 255 / 60%); border-radius: 999px; background: rgb(255 255 255 / 60%); color: color-mix(in oklch, var(--fg) 67%, transparent); font-size: 9px; backdrop-filter: blur(8px); }

.classroom-card-body { display: flex; flex-direction: column; padding: 16px 16px 13px; }
.course-card-heading { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.course-kind { display: inline-flex; align-items: center; gap: 6px; color: var(--accent); font-size: 10px; font-weight: 600; }
.course-kind i { width: 6px; height: 6px; border-radius: 50%; background: var(--accent); }
.classroom-card h3 { display: -webkit-box; min-height: 44px; overflow: hidden; margin-top: 10px; font-size: 16px; font-weight: 650; line-height: 1.45; -webkit-box-orient: vertical; -webkit-line-clamp: 2; }
.classroom-goal { display: -webkit-box; min-height: 39px; overflow: hidden; margin-top: 5px; color: var(--muted); font-size: 11px; line-height: 1.65; -webkit-box-orient: vertical; -webkit-line-clamp: 2; }
.classroom-card-footer { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-top: 14px; padding-top: 11px; border-top: 1px solid var(--border-soft); }
.classroom-card-footer > span { color: var(--meta); font-size: 9px; }

.empty-library { display: flex; min-height: min(64vh, 590px); flex-direction: column; align-items: center; justify-content: center; padding: 30px 20px 52px; text-align: center; }
.empty-art { position: relative; display: grid; width: 196px; height: 160px; place-items: center; margin-bottom: 8px; }
.empty-art > svg { position: relative; z-index: 1; width: 132px; height: 110px; overflow: visible; filter: drop-shadow(0 12px 12px rgb(62 47 92 / 10%)); }
.empty-book-back { fill: color-mix(in oklch, var(--accent) 35%, #e9e1f5); }
.empty-book-page { fill: #fff; stroke: color-mix(in oklch, var(--accent) 18%, var(--border)); stroke-width: 1.2; }
.empty-book-page--right { fill: #f9f7fc; }
.empty-book-spine { fill: none; stroke: color-mix(in oklch, var(--accent) 45%, #fff); stroke-linecap: round; stroke-width: 2; }
.empty-art-orbit { position: absolute; border: 1px solid color-mix(in oklch, var(--accent) 12%, transparent); border-radius: 50%; }
.empty-art-orbit--one { width: 150px; height: 150px; }
.empty-art-orbit--two { width: 184px; height: 120px; transform: rotate(-25deg); }
.empty-art-star { position: absolute; z-index: 2; color: color-mix(in oklch, var(--accent) 60%, white); }
.empty-art-star--one { top: 15px; right: 21px; font-size: 17px; }
.empty-art-star--two { bottom: 22px; left: 24px; font-size: 12px; }
.empty-library h2 { margin-top: 7px; font-size: 20px; font-weight: 650; letter-spacing: -.02em; }
.empty-library p { max-width: 400px; margin-top: 8px; color: var(--muted); font-size: 12px; line-height: 1.7; }
.empty-library .btn { margin-top: 19px; }
.no-results { display: flex; min-height: 280px; flex-direction: column; align-items: center; justify-content: center; gap: 8px; border: 1px dashed var(--border); border-radius: 14px; color: var(--muted); }
.no-results-mark { display: grid; width: 42px; height: 42px; margin-bottom: 5px; place-items: center; border-radius: 13px; background: var(--border-soft); color: var(--meta); }
.no-results-mark svg { width: 19px; height: 19px; fill: none; stroke: currentColor; stroke-linecap: round; stroke-width: 1.6; }
.no-results strong { color: var(--fg); font-size: 13px; }
.no-results > span:not(.no-results-mark) { font-size: 11px; }
.clear-filters { margin-top: 5px; padding: 6px 10px; border: 0; background: transparent; color: var(--accent); cursor: pointer; font: inherit; font-size: 11px; }

@media (max-width: 1050px) {
  .classroom-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}

@media (max-width: 760px) {
  .library-heading { align-items: flex-start; }
  .library-heading .lead { max-width: 40ch; }
  .library-overview { margin-bottom: 24px; }
  .overview-mark { display: none; }
  .library-title-row { align-items: flex-start; flex-direction: column; gap: 12px; }
  .course-search { width: 100%; }
  .classroom-grid { grid-template-columns: minmax(0, 1fr); gap: 13px; }
  .classroom-cover { min-height: 125px; }
  .empty-library { min-height: 54vh; }
}

@media (max-width: 480px) {
  .library-heading { flex-direction: column; gap: 16px; }
  .create-classroom { align-self: stretch; justify-content: center; }
  .overview-copy span { max-width: 37ch; line-height: 1.5; }
  .empty-library { padding-inline: 4px; }
}
</style>
