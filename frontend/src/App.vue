<script setup lang="ts">
import { watch } from 'vue'
import { RouterView, useRoute } from 'vue-router'

const route = useRoute()

watch(
  () => route.meta.bodyClass,
  (bodyClass) => {
    document.body.className = typeof bodyClass === 'string' ? bodyClass : ''
  },
  { immediate: true },
)
</script>

<template>
  <RouterView />
</template>
