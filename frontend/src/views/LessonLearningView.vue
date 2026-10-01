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
const unitNumber = computed(() => Number(route.params.unitNumber))
const lessonNumber = computed(() => Number(route.params.lessonNumber))
const unit = computed(() => {
  if (!Number.isInteger(unitNumber.value) || unitNumber.value < 1) return undefined
  return course.value?.units[unitNumber.value - 1]
})
const lessonTitle = computed(() => {
  if (!Number.isInteger(lessonNumber.value) || lessonNumber.value < 1) return undefined
  return unit.value?.lessons[lessonNumber.value - 1]
})
</script>

<template>
  <div class="lesson-page">
    <RouterLink
      v-if="course"
      class="lesson-back"
      :to="{ name: 'app-course-detail', params: { id: course.id } }"
    >
      <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m15 18-6-6 6-6M9 12h11" /></svg>
      返回课程大纲
    </RouterLink>

    <template v-if="course && unit && lessonTitle">
      <header class="lesson-heading">
        <span class="eyebrow">课堂学习</span>
        <h1>{{ lessonTitle }}</h1>
        <p>{{ course.title }}<span aria-hidden="true"> / </span>第 {{ unitNumber }} 单元 · 第 {{ lessonNumber }} 小节</p>
      </header>

      <section class="lesson-content" aria-label="课堂内容">
        <span class="lesson-content-icon" aria-hidden="true">
          <svg viewBox="0 0 24 24"><path d="M4 5.5c3.2-.9 5.9-.2 8 1.5 2.1-1.7 4.8-2.4 8-1.5v13c-3.2-.9-5.9-.2-8 1.5-2.1-1.7-4.8-2.4-8-1.5v-13Z" /><path d="M12 7v13M7 9l2 .4M15 9l2-.4M7 12l2 .4M15 12l2-.4" /></svg>
        </span>
        <span class="lesson-content-label">本节课堂</span>
        <h2>学习内容将在这里呈现</h2>
        <p>「{{ unit.title }} · {{ lessonTitle }}」的讲义、讲解与练习接入后，会显示在这个页面。</p>
        <RouterLink class="lesson-outline-link" :to="{ name: 'app-course-detail', params: { id: course.id } }">
          返回课程大纲
        </RouterLink>
      </section>
    </template>

    <section v-else class="lesson-not-found">
      <span class="not-found-mark" aria-hidden="true">?</span>
      <h1>没有找到这个小节</h1>
      <p>课程或小节地址可能已失效，请回到课程列表重新选择。</p>
      <RouterLink class="lesson-outline-link" :to="{ name: 'app-courses' }">返回我的课堂</RouterLink>
    </section>
  </div>
</template>

<style scoped>
.lesson-page { max-width: 1050px; min-height: 60vh; margin-inline: auto; }
.lesson-back { display: inline-flex; align-items: center; gap: 7px; margin-bottom: 24px; color: var(--muted); font-size: 12px; transition: color 140ms ease; }
.lesson-back:hover { color: var(--accent); }
.lesson-back svg { width: 16px; height: 16px; fill: none; stroke: currentColor; stroke-linecap: round; stroke-linejoin: round; stroke-width: 1.7; }
.lesson-heading { padding: 8px 0 22px; }
.lesson-heading .eyebrow { color: var(--accent); }
.lesson-heading h1 { margin-top: 9px; font-size: clamp(25px, 4vw, 36px); font-weight: 670; letter-spacing: -.035em; line-height: 1.3; }
.lesson-heading p { margin-top: 10px; color: var(--muted); font-size: 12px; }
.lesson-content { display: flex; min-height: 390px; flex-direction: column; align-items: center; justify-content: center; padding: 34px; border: 1px solid var(--border); border-radius: 16px; background: var(--surface); text-align: center; }
.lesson-content-icon { display: grid; width: 58px; height: 58px; place-items: center; border: 1px solid color-mix(in oklch, var(--accent) 20%, var(--border)); border-radius: 18px; background: color-mix(in oklch, var(--accent) 8%, var(--surface)); color: var(--accent); }
.lesson-content-icon svg { width: 27px; height: 27px; fill: none; stroke: currentColor; stroke-linecap: round; stroke-linejoin: round; stroke-width: 1.5; }
.lesson-content-label { margin-top: 17px; color: var(--meta); font-family: var(--font-mono); font-size: 9px; letter-spacing: .1em; text-transform: uppercase; }
.lesson-content h2 { margin-top: 8px; font-size: 20px; font-weight: 640; }
.lesson-content p, .lesson-not-found p { max-width: 50ch; margin-top: 9px; color: var(--muted); font-size: 12px; line-height: 1.75; }
.lesson-outline-link { display: inline-flex; align-items: center; justify-content: center; margin-top: 19px; padding: 9px 13px; border: 1px solid var(--border); border-radius: 9px; background: var(--surface); color: var(--muted); font-size: 11px; transition: border-color 140ms ease, color 140ms ease; }
.lesson-outline-link:hover { border-color: var(--accent); color: var(--accent); }
.lesson-not-found { display: flex; min-height: 50vh; flex-direction: column; align-items: center; justify-content: center; text-align: center; }
.not-found-mark { display: grid; width: 46px; height: 46px; place-items: center; border-radius: 15px; background: color-mix(in oklch, var(--accent) 9%, var(--surface)); color: var(--accent); font-size: 20px; }
.lesson-not-found h1 { margin-top: 15px; font-size: 21px; }
@media (max-width: 600px) { .lesson-content { min-height: 320px; padding: 24px 18px; } }
</style>
