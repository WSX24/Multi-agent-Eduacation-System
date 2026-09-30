import { createRouter, createWebHistory } from 'vue-router'
import AppShell from '../components/AppShell.vue'
import AuthView from '../views/AuthView.vue'
import CourseDetailView from '../views/CourseDetailView.vue'
import CoursesView from '../views/CoursesView.vue'
import GenerateView from '../views/GenerateView.vue'
import LandingView from '../views/LandingView.vue'
import LessonLearningView from '../views/LessonLearningView.vue'

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    {
      path: '/',
      name: 'landing',
      component: LandingView,
      meta: { bodyClass: 'landing' },
    },
    {
      path: '/auth',
      name: 'auth',
      component: AuthView,
      meta: { bodyClass: 'auth' },
    },
    {
      path: '/app',
      component: AppShell,
      meta: { bodyClass: 'workspace' },
      children: [
        {
          path: '',
          redirect: { name: 'app-generate' },
        },
        {
          path: 'generate',
          name: 'app-generate',
          component: GenerateView,
          meta: { title: '生成课堂' },
        },
        {
          path: 'courses',
          name: 'app-courses',
          component: CoursesView,
          meta: { title: '我的课堂' },
        },
        {
          path: 'courses/:id/units/:unitNumber/lessons/:lessonNumber',
          name: 'app-lesson',
          component: LessonLearningView,
          meta: { title: '课堂学习' },
        },
        {
          path: 'courses/:id',
          name: 'app-course-detail',
          component: CourseDetailView,
          meta: { title: '课堂详情' },
        },
      ],
    },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
  scrollBehavior(to, _from, savedPosition) {
    if (savedPosition) return savedPosition
    if (to.hash) return { el: to.hash, behavior: 'smooth' }
    return { top: 0 }
  },
})

export default router
