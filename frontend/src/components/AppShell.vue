<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { RouterLink, RouterView, useRoute } from 'vue-router'

const route = useRoute()
const isNavOpen = ref(false)
const pageTitle = computed(() => String(route.meta.title ?? '工作区'))
const isCoursesSection = computed(() => route.name === 'app-courses' || route.name === 'app-course-detail' || route.name === 'app-lesson')

const closeOnEscape = (event: KeyboardEvent) => {
  if (event.key === 'Escape') isNavOpen.value = false
}

onMounted(() => window.addEventListener('keydown', closeOnEscape))
onBeforeUnmount(() => window.removeEventListener('keydown', closeOnEscape))
</script>

<template>
  <div class="app" :data-nav="isNavOpen ? 'open' : 'closed'">
    <aside class="sidebar" aria-label="主导航">
      <RouterLink class="brand" :to="{ name: 'app-generate' }" @click="isNavOpen = false">
        <span class="brand-mark" aria-hidden="true">
          <svg class="icon icon-lg" viewBox="0 0 24 24">
            <rect x="5.5" y="3.5" width="13" height="11" rx="4.5" />
            <path d="M9 8.5h.01M15 8.5h.01M9.5 17.5h5M12 14.5v3M4 18.5h16" />
          </svg>
        </span>
        <span class="brand-name">智塾<small>AI TEACHING PLATFORM</small></span>
      </RouterLink>

      <nav class="nav" aria-label="工作区页面">
        <span class="nav-label">工作区</span>
        <RouterLink class="nav-item" :to="{ name: 'app-generate' }" @click="isNavOpen = false">
          <svg class="icon" viewBox="0 0 24 24" aria-hidden="true">
            <circle cx="12" cy="12" r="9" />
            <path d="M12 8v8M8 12h8" />
          </svg>
          <span>生成课堂</span>
        </RouterLink>
        <RouterLink class="nav-item" :to="{ name: 'app-courses' }" :aria-current="isCoursesSection ? 'page' : undefined" @click="isNavOpen = false">
          <svg class="icon" viewBox="0 0 24 24" aria-hidden="true">
            <path d="M4 4h6v16H4zM14 4h6v16h-6z" />
          </svg>
          <span>我的课堂</span>
        </RouterLink>
      </nav>

      <div class="side-foot">
        <div class="side-user">
          <span class="avatar avatar--accent" aria-hidden="true">小T</span>
          <span class="side-user-copy">
            <span>演示账号</span>
            <span class="caption">本地前端预览</span>
          </span>
        </div>
      </div>
    </aside>

    <button
      v-if="isNavOpen"
      class="app-nav-backdrop"
      type="button"
      aria-label="关闭导航菜单"
      @click="isNavOpen = false"
    />

    <div class="main">
      <header class="topbar">
        <button
          class="icon-btn menu-btn"
          type="button"
          :aria-expanded="isNavOpen"
          aria-label="打开导航菜单"
          @click="isNavOpen = !isNavOpen"
        >
          <svg class="icon" viewBox="0 0 24 24" aria-hidden="true">
            <path d="M4 7h16M4 12h16M4 17h16" />
          </svg>
        </button>
        <div class="breadcrumb">
          智塾<span aria-hidden="true">/</span><b>{{ pageTitle }}</b>
        </div>
        <span class="workspace-mark">课程工作区</span>
      </header>

      <main class="page">
        <RouterView />
      </main>
    </div>
  </div>
</template>
