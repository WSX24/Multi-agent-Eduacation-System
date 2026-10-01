<script setup lang="ts">
import { RouterLink } from 'vue-router'
import botanyMarkup from '../assets/landing-botany.html?raw'
import { useLandingMotion } from '../composables/useLandingMotion'

const { film, filmFrame, video, botanyHost, isStatic } = useLandingMotion()

const agents = [
  {
    sigil: 'TEACHER',
    title: '教师',
    description:
      '负责搭建完整学习路径。从学习大纲出发，拆解单元与章节，分阶段编排教学任务；并根据掌握程度动态调整难度。',
    tasks: ['大纲 → 单元 → 章节分层备课', '单元配套随堂练习', '章节阶段性测验'],
  },
  {
    sigil: 'ASSISTANT',
    title: '助教',
    description:
      '自动批阅作答，生成学情报告，定位知识薄弱点，并将反馈交给课程规划环节；同步整理错题。',
    tasks: ['作业自动批改', '分析薄弱知识点', '错题归集并流转答疑'],
  },
  {
    sigil: 'Q&A',
    title: '习题答疑员',
    description:
      '维护个人错题本，提供错题解析和一对一问答，并根据错题生成同类变式练习。',
    tasks: ['错题一键解答', '一对一答疑对话', '生成同类变式题'],
  },
  {
    sigil: 'COACH',
    title: '学习督导师',
    description:
      '关注学习时长、正确率与提问记录，识别学习节奏，并安排复习计划和阶段性简报。',
    tasks: ['学习行为数据采集', '复习计划调度', '学习提示与阶段简报'],
  },
]

const steps = [
  ['01', '生成课堂', '选教师形象、模型与教学周期，先生成一份大纲。'],
  ['02', '按需生成', '讲解内容随学习进度生成，难度可以调整。'],
  ['03', '练习与测验', '每个单元安排练习，每个章节进行测验。'],
  ['04', '批改与学情', '助教批改作答、定位薄弱点并整理错题。'],
  ['05', '答疑与复习', '解答错题，并按复习计划回顾知识。'],
]
</script>

<template>
  <div class="landing">
    <header class="land-top">
      <RouterLink class="mark" to="/">
        <span aria-hidden="true">智塾</span>
        <small>AI TEACHING PLATFORM</small>
      </RouterLink>
      <nav aria-label="页面导航">
        <a href="#agents" data-scroll-to="agents">专业团队</a>
        <a href="#loop" data-scroll-to="loop">学习闭环</a>
      </nav>
      <RouterLink class="btn btn-sm" to="/auth">进入系统</RouterLink>
    </header>

    <main>
      <section id="film" ref="film" class="film" :class="{ 'is-static': isStatic }">
        <div class="film-pin">
          <div ref="filmFrame" class="film-frame">
            <img
              class="film-poster"
              src="/build/media/final/poster.png"
              alt=""
              decoding="async"
            >
            <video
              ref="video"
              class="film-video"
              poster="/build/media/final/poster.png"
              muted
              playsinline
              preload="metadata"
              tabindex="-1"
              aria-hidden="true"
            ></video>
          </div>
          <div class="film-say">
            <div class="say-rotor" aria-hidden="true">
              <h1 class="say-item" data-say="brand">
                <span class="l1">AI</span>
                <span class="l2">Teaching <em>Platform</em></span>
              </h1>
              <p class="say-item say-item--phrase" data-say="p1">课堂定制</p>
              <p class="say-item say-item--phrase" data-say="p2">个性化辅导</p>
              <p class="say-item say-item--phrase" data-say="p3">智教答疑</p>
              <p class="say-item say-item--phrase" data-say="p4">随时学习</p>
            </div>
            <div id="sayFinal" class="say-final">
              <p class="final-kicker" data-stagger>欢迎加入</p>
              <ul class="final-list">
                <li data-stagger>课堂定制</li>
                <li data-stagger>个性化辅导</li>
                <li data-stagger>智教答疑</li>
                <li data-stagger>随时学习</li>
              </ul>
              <div id="filmCta" class="film-cta" data-stagger>
                <RouterLink class="btn btn-sky btn-lg" to="/auth">开始学习</RouterLink>
              </div>
            </div>
          </div>
          <div class="film-cue" aria-hidden="true"><i /></div>
        </div>
        <div class="film-track" aria-hidden="true"><span id="filmBar" /></div>
      </section>

      <section class="land-section" id="agents">
        <div class="wrap">
          <h2>
            <span class="nb">一间数字化教室，</span>
            <span class="nb">一套完整学习流水线</span>
          </h2>
          <div class="agents-row">
            <article v-for="agent in agents" :key="agent.sigil" class="agent-tile">
              <span class="sigil">{{ agent.sigil }}</span>
              <h3>{{ agent.title }}</h3>
              <p>{{ agent.description }}</p>
              <ul>
                <li v-for="task in agent.tasks" :key="task">· {{ task }}</li>
              </ul>
            </article>
          </div>
        </div>
      </section>

      <section class="land-section" id="loop">
        <div class="wrap">
          <span class="kicker">学习闭环 · 从大纲到复习</span>
          <h2>一条从大纲走到复习的闭环</h2>
          <div class="loop-row">
            <article v-for="step in steps" :key="step[0]" class="loop-cell">
              <span class="idx">{{ step[0] }}</span>
              <h4>{{ step[1] }}</h4>
              <p>{{ step[2] }}</p>
            </article>
          </div>
        </div>
      </section>

      <section class="land-cta" id="cta">
        <div class="wrap">
          <h2>
            <span class="nb">人生没有白走的路，</span>
            <span class="nb">每一步都算数。</span>
          </h2>
          <div class="row row--wrap">
            <RouterLink class="btn btn-primary btn-lg" to="/auth">开始学习</RouterLink>
          </div>
        </div>
      </section>
    </main>
    <div ref="botanyHost" v-html="botanyMarkup"></div>
  </div>
</template>
